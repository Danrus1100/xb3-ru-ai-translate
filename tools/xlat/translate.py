"""Перевод корпуса через OpenAI/Anthropic-совместимый API.

Один запрос = один блок (по умолчанию 20 строк одной таблицы). В запрос попадают
соседние строки (контекст), подмножество глоссария и стиль. Ответ — JSON
`{"items":[{"i":N,"ru":"..."}]}`. Лестница исправлений: повтор блока со списком ошибок →
деление блока пополам → поштучно → `failed`.

Пример (из каталога `tools`):
    python -m xlat.translate --corpus ../work/corpus --store ../work/t9n/cache.sqlite \
        --batch 20 --concurrency 4 --limit 200
"""

from __future__ import annotations

import argparse
import csv
import random
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import store as store_mod
from . import validate as V
from .api import ChatClient, ApiError, load_config, parse_json_answer


def load_glossary(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("en") and r.get("ru")]
    return rows


def load_scope(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def in_scope(rec: dict, scope: dict) -> bool:
    table = rec["table"]
    for pat in scope.get("exclude_tables") or []:
        if re.fullmatch(pat.replace("*", ".*"), table):
            return False
    # явный список колонок для таблицы = разрешение переводить её (даже если она не msg_*)
    explicit = (scope.get("table_columns") or {}).get(table)
    if explicit is not None:
        return rec["col"] in explicit
    inc = scope.get("include_tables") or []
    if inc and not any(re.fullmatch(p.replace("*", ".*"), table) for p in inc):
        return False
    allowed = scope.get("non_text_cols")
    if allowed and rec["col"] in allowed:
        return False
    return True


def glossary_subset(texts: list[str], glossary: list[dict]) -> list[dict]:
    blob = "\n".join(texts)
    picked, seen = [], set()
    for row in glossary:
        en = row["en"]
        hit = row.get("always") == "1" or V.term_present(
            en, blob, row.get("case_sensitive") == "true"
        )
        if hit and en not in seen:
            picked.append(row)
            seen.add(en)
    return picked


def render_glossary(rows: list[dict]) -> str:
    out = []
    for r in rows:
        tail = " (keep latin)" if r.get("mode") == "keep" else ""
        out.append(f"{r['en']} => {r['ru']}{tail}")
    return "\n".join(out) if out else "(нет совпадений)"


def blocks_of(records: list[dict], batch: int) -> list[list[dict]]:
    """Клеим подряд идущие записи в блоки по `batch`, не разрываясь на границах таблиц.

    Корпус — тысячи мелких таблиц; нарезка по таблицам давала в среднем 2.8 строки на
    запрос (замерено по счёту провайдера: 12 671 запрос на ~35.7 тыс. строк), а каждый
    запрос заново тащит неизменный системный блок и своё подмножество глоссария.
    Корпус уже упорядочен по таблицам, поэтому соседние реплики остаются рядом.
    """
    return [records[i : i + batch] for i in range(0, len(records), batch)]


class Translator:
    def __init__(self, args):
        self.args = args
        self.cfg = load_config(args.api)
        if getattr(args, "temperature", None) is not None:
            self.cfg["temperature"] = args.temperature
        self.client = ChatClient(self.cfg)
        self.ctx_limit = getattr(args, "context_limit", 2) or 0
        self.ref_mode = getattr(args, "ref_mode", "auto")
        # последняя причина брака по ключу: иначе в кэш попадает неинформативное «нет ответа»
        self.last_errors: dict[str, str] = {}
        self.glossary = load_glossary(args.glossary)
        self.style = open(args.style, encoding="utf-8").read()
        self.world = open(args.world, encoding="utf-8").read() if os.path.isfile(args.world) else ""
        prompt = open(args.prompt, encoding="utf-8").read()
        # Системный блок должен быть НЕИЗМЕННЫМ на весь прогон: подмножество глоссария
        # передаётся в каждом пользовательском сообщении (иначе префикс меняется и не кэшируется).
        self.system = re.sub(r"^<!--.*?-->\s*", "", prompt, flags=re.S).replace(
            "{style}", self.style
        ).replace(
            "{world}", self.world
        ).replace(
            "{glossary}",
            "Список терминов с пояснениями для этого батча передан в сообщении "
            "пользователя (поле glossary: en, ru, режим, пояснение).",
        )
        self.pver = store_mod.prompt_version(args.prompt)
        self.gsha = store_mod.glossary_sha(args.glossary, args.style)
        # Правка глоссария не должна обнулять кэш: уже переведённые строки остаются
        # валидными, а расхождения с новыми пометками чинятся точечной перегонкой
        # (--retranslate-filter) по термину.
        if getattr(args, "ignore_glossary_change", False):
            self.gsha = ""
        self.lock = threading.Lock()
        self.stats = {"ok": 0, "cached": 0, "failed": 0, "requests": 0, "skipped": 0}

    # --- один блок с лестницей исправлений --------------------------------
    def do_block(self, block: list[dict]) -> dict:
        """Полный цикл для блока: батч → досылка пропущенных → половины → поштучно."""
        out = self.try_once(block) or {}
        missing = [r for r in block if r["key"] not in out]
        if len(missing) > 2:
            half = len(missing) // 2
            for part in (missing[:half], missing[half:]):
                out.update(self.try_once(part, ["верни все элементы батча"]) or {})
            missing = [r for r in block if r["key"] not in out]
        if len(missing) > 1:
            out.update(self.try_once(missing, ["верни все элементы батча"]) or {})
            missing = [r for r in block if r["key"] not in out]
        if missing:
            out.update(self.line_by_line(missing))
        for rec in block:
            if rec["key"] not in out:
                out[rec["key"]] = (None, "failed", 3, "нет ответа после всех попыток", "")
        return out

    def try_once(self, block: list[dict], extra_errors: list[str] | None = None) -> dict | None:
        def use_ref(rec: dict) -> bool:
            if self.ref_mode == "never" or not rec.get("ref"):
                return False
            if self.ref_mode == "always":
                return True
            if "/evt/" in ("/" + rec["path"]) or rec["table"].startswith("msg_msg_"):
                return True
            # Реплики в поле и системные сообщения лежат в таблицах с хеш-именами
            # (game/field, game/system) — это те же диалоги, японская подсказка нужна.
            return rec["path"] in ("game/field", "game/system")

        items = [
            {
                "i": i,
                "src": rec["src"],
                **({"prev": rec["prev"][-self.ctx_limit:]} if rec.get("prev") and self.ctx_limit else {}),
                **({"next": rec["next"][: self.ctx_limit]} if rec.get("next") and self.ctx_limit else {}),
                **({"ja": rec["ref"]} if use_ref(rec) else {}),
            }
            for i, rec in enumerate(block)
        ]
        subset = glossary_subset([r["src"] for r in block], self.glossary)
        payload = {
            "scene": (
                f"{block[0]['path']}/{block[0]['table']}"
                if len({(r["path"], r["table"]) for r in block}) == 1
                else "несколько таблиц подряд"
            ),
            # глоссарий — в user-сообщении: системный блок остаётся неизменным (кэш префикса)
            "glossary": [
                [r["en"], r["ru"], r.get("mode", "suggest"), (r.get("notes") or "")[:160]]
                for r in subset
            ],
            "items": items,
        }
        user = json.dumps(payload, ensure_ascii=False)
        user += f"\n\nВерни РОВНО {len(items)} элементов, индексы 0..{len(items)-1}, ничего не пропускать."
        if extra_errors:
            user += "\nИсправь ошибки и верни полный JSON заново: " + "; ".join(extra_errors[:10])
        # Шлюз под нагрузкой иногда отдаёт пустой ответ (`items: []`) или обрезанный JSON —
        # это транзиентный сбой, а не ошибка перевода: повторяем с паузой, и только потом
        # идём по лестнице исправлений.
        got: dict[int, str] = {}
        last_error = ""
        for attempt in range(3):
            try:
                raw = self.client.complete(self.system, user)
            except ApiError as e:
                return {rec["key"]: (None, "failed", 1, f"api: {e}"[:300], "") for rec in block}
            with self.lock:
                self.stats["requests"] += 1
            try:
                data = parse_json_answer(raw)
                got = {int(it["i"]): str(it.get("ru", "")) for it in data.get("items", [])}
                if got:
                    break
                last_error = "пустой ответ (items: [])"
            except (ApiError, KeyError, TypeError, ValueError) as e:
                last_error = f"json: {str(e)[:120]}"
            time.sleep(1.5 * (attempt + 1) + random.random())
        if not got:
            if extra_errors is None:
                return self.try_once(block, [f"ответ не пригоден: {last_error}"])
            return {rec["key"]: (None, "failed", 1, last_error[:200], "") for rec in block}

        out, errors, broken = {}, [], []
        for i, rec in enumerate(block):
            ru = got.get(i)
            if ru is None:
                errors.append(f"[{i}] нет ответа")
                self.last_errors[rec["key"]] = "нет ответа (пропущен в ответе)"
                broken.append(rec)
                continue
            errs = V.blocking_errors(rec["src"], ru, self.glossary)
            if errs:
                errors.extend(f"[{i}] {e}" for e in errs)
                self.last_errors[rec["key"]] = errs[0][:200]
                broken.append(rec)
                continue
            flags = V.flags_for(rec["src"], ru, rec["table"], self.glossary)
            out[rec["key"]] = (ru, "ok", 1, None, ",".join(flags))
        # Частичный успех сохраняем: досылаем только пропущенные/забракованные,
        # иначе (как было раньше) терялся весь батч и падал в «Нет ответа».
        if broken and extra_errors is None:
            retry_block = broken if len(broken) < len(block) else block
            retry = self.try_once(retry_block, errors) or {}
            for k, v in retry.items():
                if v[1] == "ok":
                    out[k] = v
        return out

    def line_by_line(self, block: list[dict]) -> dict:
        out = {}
        for rec in block:
            stripped = {k: v for k, v in rec.items() if k not in ("prev", "next", "ref")}
            single = self.try_once([stripped]) or {}
            value = single.get(rec["key"])
            if value and value[1] == "ok":
                out[rec["key"]] = value
            else:
                out[rec["key"]] = (
                    None,
                    "failed",
                    3,
                    (
                        value[3]
                        if value
                        else self.last_errors.get(rec["key"], "нет ответа")
                    ),
                    "",
                )
        return out

    # --- прогон -----------------------------------------------------------
    def run(self) -> int:
        args = self.args
        records = [json.loads(line) for line in open(args.corpus, encoding="utf-8")]
        scope = load_scope(args.scope)
        records = [r for r in records if in_scope(r, scope)]
        if args.only_tables:
            wanted = set(args.only_tables.split(","))
            records = [r for r in records if r["table"] in wanted or r["path"] in wanted]

        st = store_mod.Store(args.store)
        if args.retranslate_filter_file:
            with open(args.retranslate_filter_file, encoding="utf-8") as f:
                args.retranslate_filter = f.read().strip()
        if args.retranslate_filter:
            keys = {
                row[0]
                for row in st.db.execute(f"SELECT key FROM t WHERE {args.retranslate_filter}")
            }
            records = [r for r in records if r["key"] in keys]
            print(f"по фильтру «{args.retranslate_filter}»: {len(records)} записей")

        # дедупликация по src_sha
        rep: dict[str, dict] = {}
        aliases: dict[str, list[dict]] = {}
        for rec in records:
            sha = rec["src_sha"]
            if sha in rep:
                aliases.setdefault(sha, []).append(rec)
            else:
                rep[sha] = rec
        uniq_records = list(rep.values())

        todo, cached = [], 0
        for rec in uniq_records:
            hit = None if args.force else st.cached(
                rec["key"], rec["src_sha"], self.cfg["model"], self.pver, self.gsha,
                ignore_versions=getattr(args, "ignore_glossary_change", False),
            )
            if hit is not None:
                cached += 1
                continue
            todo.append(rec)
        self.stats["cached"] = cached
        if args.limit:
            todo = todo[: args.limit]

        est_chars = sum(len(r["src"]) for r in todo)
        print(
            f"записей в области: {len(records)}, уникальных: {len(uniq_records)}, "
            f"из кэша: {cached}, к переводу: {len(todo)}"
        )
        print(
            f"оценка: символов {est_chars}, токенов входа ≈ {math.ceil(est_chars/3.3)}, "
            f"выхода ≈ {math.ceil(est_chars/2.4)}, запросов ≈ {len(blocks_of(todo, args.batch))}"
        )
        if args.dry_run or not todo:
            return 0

        blocks = blocks_of(todo, args.batch)
        done = 0
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(self.do_block, b): b for b in blocks}
            for fut in as_completed(futures):
                block = futures[fut]
                try:
                    results = fut.result()
                except Exception as e:  # noqa: BLE001
                    results = {rec["key"]: (None, "failed", 0, f"exception: {e}"[:200], "") for rec in block}
                with self.lock:
                    for rec in block:
                        ru, status, attempts, error, flags = results.get(
                            rec["key"], (None, "failed", 0, "нет результата", "")
                        )
                        st.put(
                            rec["key"], rec["src"], rec["src_sha"], ru, status,
                            self.cfg["model"], self.pver, self.gsha,
                            attempts, 0, 0, error, flags,
                        )
                        if status == "ok":
                            self.stats["ok"] += 1
                            for alias in aliases.get(rec["src_sha"], []):
                                st.put(
                                    alias["key"], alias["src"], alias["src_sha"], ru, "ok",
                                    self.cfg["model"], self.pver, self.gsha, 1, 0, 0, None, flags,
                                )
                        else:
                            self.stats["failed"] += 1
                    st.commit()
                    done += len(block)
                    if done % 200 < len(block):
                        print(
                            f"  {done}/{len(todo)}  ok={self.stats['ok']} failed={self.stats['failed']} "
                            f"запросов={self.stats['requests']} токенов={self.client.tokens_in}→{self.client.tokens_out}",
                            flush=True,
                        )
                    if args.max_tokens_total and self.client.tokens_in + self.client.tokens_out > args.max_tokens_total:
                        print("достигнут --max-tokens-total, останавливаюсь", flush=True)
                        for f2 in futures:
                            f2.cancel()
                        break
        st.commit()
        print(
            f"готово: ok={self.stats['ok']} failed={self.stats['failed']} из кэша={self.stats['cached']} "
            f"| {self.client.summary()}"
        )
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xlat.translate")
    ap.add_argument("--corpus", default="../work/corpus/corpus.jsonl")
    ap.add_argument("--scope", default="../config/scope.json")
    ap.add_argument("--api", default="../config/api.json")
    ap.add_argument("--glossary", default="../config/glossary.csv")
    ap.add_argument("--style", default="../config/style_ru.md")
    ap.add_argument("--world", default="../config/world.md",
                    help="блок контекста мира, подставляется в системный промпт")
    ap.add_argument("--prompt", default="../config/prompt.md")
    ap.add_argument("--store", default="../work/t9n/cache.sqlite")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-tables", default="")
    ap.add_argument("--retranslate-filter", default="")
    ap.add_argument("--retranslate-filter-file", default="",
                    help="то же, но SQL-условие читается из файла (длинные списки ключей)")
    ap.add_argument("--max-tokens-total", type=int, default=0)
    ap.add_argument("--temperature", type=float, default=None, help="переопределить температуру из конфига")
    ap.add_argument("--context-limit", type=int, default=2,
                    help="сколько соседних строк с каждой стороны класть в промпт (в корпусе до 4)")
    ap.add_argument("--ref-mode", choices=["auto", "always", "never"], default="auto",
                    help="японская подсказка: auto — только для диалогов (evt/*), always/never")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--ack-scope", action="store_true")
    ap.add_argument("--ignore-glossary-change", action="store_true",
                    help="не считать изменение промпта или глоссария причиной переводить строку заново")
    args = ap.parse_args(argv)
    return Translator(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
