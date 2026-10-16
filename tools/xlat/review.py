"""Ревью-проход: модель-редактор проверяет готовый перевод по оригиналу.

Зачем: первый проход ловит механику (теги, глоссарий, падежи), но не смысл. Калька
идиомы («do or die» → «теперь или никогда»), выброшенное имя, не тот регистр — это
видит только второй проход с японским оригиналом на руках.

Как: строки берутся из кэша (`status='ok'`), оригинал и `ja` — из корпуса. Пакет из
`--batch` строк уходит редактору (промпт `config/review.md`), ответ — JSON вида
`{"items":[{"i":0,"ok":true},{"i":1,"ok":false,"issue":"…","fix":"…"}]}`. Вердикт
пишется в колонку `review` той же таблицы, поэтому проход продолжается с места
остановки: повторный запуск берёт только непроверенные строки.

Запуск (из каталога `tools`):
    python -m xlat.review --limit 40                       # проба
    python -m xlat.review --batch 20 --concurrency 12      # весь корпус
    python -m xlat.review --apply                          # вкатить предложенные правки
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import validate as V
from .api import ChatClient, ApiError, load_config, parse_json_answer
from .translate import glossary_subset, load_glossary


def load_corpus_index(path: str) -> dict[str, dict]:
    """key → {src, path, table, ref} — только то, что нужно редактору."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            out[r["key"]] = {
                "src": r["src"],
                "path": r.get("path") or "",
                "table": r["table"],
                "ref": r.get("ref") or "",
            }
    return out


def render_glossary(rows: list[dict]) -> list[list[str]]:
    return [
        [r["en"], r["ru"], r.get("mode", "suggest"), (r.get("notes") or "")[:160]] for r in rows
    ]


class Reviewer:
    def __init__(self, args):
        self.args = args
        self.cfg = load_config(args.api)
        if args.temperature is not None:
            self.cfg["temperature"] = args.temperature
        self.client = ChatClient(self.cfg)
        self.system = open(args.prompt, encoding="utf-8").read()
        if os.path.isfile(args.world):
            self.system += "\n\nКонтекст мира и персонажей:\n" + open(
                args.world, encoding="utf-8"
            ).read()
        self.glossary = load_glossary(args.glossary)
        self.lock = threading.Lock()
        self.stats = collections.Counter()
        self.rows: list[dict] = []

    def review_block(self, block: list[dict]) -> None:
        items = []
        for i, rec in enumerate(block):
            item = {"i": i, "src": rec["src"], "ru": rec["ru"]}
            if rec.get("ref"):
                item["ja"] = rec["ref"]
            items.append(item)
        sub = glossary_subset([r["src"] for r in block], self.glossary)
        payload = {
            "scene": f"{block[0]['path']}/{block[0]['table']}",
            "glossary": render_glossary(sub),
            "items": items,
        }
        user = json.dumps(payload, ensure_ascii=False)
        user += f"\n\nПроверь РОВНО {len(items)} элементов, индексы 0..{len(items)-1}, ничего не пропускай."
        got: dict[int, dict] = {}
        last = ""
        for attempt in range(3):
            try:
                raw = self.client.complete(self.system, user)
            except ApiError as e:
                with self.lock:
                    self.stats["api_errors"] += 1
                last = f"api: {e}"
                continue
            try:
                data = parse_json_answer(raw)
                got = {int(it["i"]): it for it in data.get("items", [])}
                if got:
                    break
                last = "пустой ответ"
            except (ApiError, KeyError, TypeError, ValueError) as e:
                last = f"json: {str(e)[:100]}"
            time.sleep(1.5 * (attempt + 1))
        if not got:
            with self.lock:
                self.stats["failed"] += len(block)
            return
        for i, rec in enumerate(block):
            verdict = got.get(i)
            if verdict is None:
                with self.lock:
                    self.stats["missing"] += 1
                continue
            if verdict.get("ok"):
                rec["review"] = ""
                with self.lock:
                    self.stats["ok"] += 1
            else:
                issue = str(verdict.get("issue") or "")[:300]
                fix = str(verdict.get("fix") or "").strip()
                rec["review"] = json.dumps({"issue": issue, "fix": fix}, ensure_ascii=False)
                with self.lock:
                    self.stats["issue"] += 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xlat.review")
    ap.add_argument("--corpus", default="../work/corpus/corpus.jsonl")
    ap.add_argument("--store", default="../work/t9n/cache.sqlite")
    ap.add_argument("--api", default="../config/api.json")
    ap.add_argument("--glossary", default="../config/glossary.csv")
    ap.add_argument("--prompt", default="../config/review.md")
    ap.add_argument("--world", default="../config/world.md")
    ap.add_argument("--report", default="../work/reports/review.tsv")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0, help="ограничить число строк (проба)")
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--filter", default="", help="SQL-условие отбора строк кэша")
    ap.add_argument("--force", action="store_true", help="перепроверить уже проверенные")
    ap.add_argument("--apply", action="store_true",
                    help="вкатить предложенные правки, прошедшие валидатор")
    args = ap.parse_args(argv)

    db = sqlite3.connect(args.store, timeout=120)
    db.execute("PRAGMA journal_mode=WAL")
    review_col = [d[1] for d in db.execute("PRAGMA table_info(t)")]
    if "review" not in review_col:
        raise SystemExit("в кэше нет колонки review")
    print("читаю корпус…")
    corpus = load_corpus_index(args.corpus)

    where = "status='ok'"
    if args.filter:
        where += f" AND ({args.filter})"
    if not args.force:
        where += " AND review IS NULL"  # '' = проверено и хорошо, NULL = ещё не проверяли
    rows = db.execute(f"SELECT key, ru FROM t WHERE {where}").fetchall()
    todo = []
    for key, ru in rows:
        rec = corpus.get(key)
        if not rec or not ru:
            continue
        todo.append({"key": key, "ru": ru, **rec})
    if args.limit:
        todo = todo[: args.limit]
    blocks = [todo[i : i + args.batch] for i in range(0, len(todo), args.batch)]
    print(f"к проверке: {len(todo)} строк, {len(blocks)} запросов")
    if todo:
        est_in = sum(len(r["src"]) + len(r["ru"]) + len(r.get("ref") or "") for r in todo) // 3
        print(f"оценка: токенов входа ≈ {est_in:,}, выхода ≈ {est_in // 4:,}")

        rev = Reviewer(args)
        t0 = time.time()
        done_blocks = 0
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(rev.review_block, b): b for b in blocks}
            for fut in as_completed(futures):
                block = futures[fut]
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001
                    print(f"  ошибка блока: {e}")
                done_blocks += 1
                # пишем вердикты сразу: проход можно прервать и продолжить
                with rev.lock:
                    for rec in block:
                        if "review" in rec:
                            db.execute("UPDATE t SET review=? WHERE key=?", (rec["review"], rec["key"]))
                    db.commit()
                if done_blocks % 25 == 0:
                    el = time.time() - t0
                    print(
                        f"  {done_blocks}/{len(blocks)} блоков | ok={rev.stats['ok']} "
                        f"issue={rev.stats['issue']} пропущено={rev.stats['missing']} "
                        f"| {el:.0f} c | токенов {rev.client.tokens_in:,}→{rev.client.tokens_out:,}",
                        flush=True,
                    )
    else:
        rev = Reviewer(args)  # нужен только для валидатора и глоссария
        print("нечего проверять — перехожу к вкату правок")

    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    issues = db.execute("SELECT key, review FROM t WHERE review IS NOT NULL AND review != ''").fetchall()
    with open(args.report, "w", encoding="utf-8") as f:
        f.write("key\tissue\tfix\tsrc\tru\n")
        for key, payload in issues:
            try:
                d = json.loads(payload)
            except ValueError:
                continue
            rec = corpus.get(key, {})
            f.write(
                f"{key}\t{d.get('issue','')}\t{d.get('fix','')}\t"
                f"{(rec.get('src') or '').replace(chr(9),' ')}\t"
                f"{rec.get('table','')}\n"
            )
    print(
        f"готово: ok={rev.stats['ok']} issue={rev.stats['issue']} "
        f"пропущено={rev.stats['missing']} ошибок={rev.stats['failed']} | "
        f"запросов {rev.client.tokens_in and rev.stats or ''} токенов {rev.client.tokens_in:,}→{rev.client.tokens_out:,}"
    )
    print(f"проблемных строк всего: {len(issues)} -> {args.report}")

    if args.apply:
        applied = rejected = 0
        for key, payload in issues:
            try:
                d = json.loads(payload)
            except ValueError:
                continue
            fix = (d.get("fix") or "").strip()
            if not fix:
                continue
            src = (corpus.get(key) or {}).get("src") or ""
            if V.blocking_errors(src, fix, rev.glossary):
                rejected += 1
                continue
            db.execute("UPDATE t SET ru=?, flags='REVIEWED' WHERE key=?", (fix, key))
            applied += 1
        db.commit()
        print(f"вкатано правок: {applied}, отклонено валидатором: {rejected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
