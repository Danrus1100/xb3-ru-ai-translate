"""Выгрузка переводов из кэша в текстовый файл для git.

Кэш (`work/t9n/cache.sqlite`) — рабочий артефакт: бинарный, большой и не годится
для истории в git. Публикуемые переводы лежат в виде отсортированного JSONL
(`translations/ru.jsonl`): читаемые диффы, правка руками, стабильный порядок строк.

Запуск (из каталога `tools`):
    python -m xlat.export --out ../translations/ru.jsonl
    python -m xlat.export --out ../translations/ru.jsonl --corpus ../work/corpus/corpus.jsonl

`--corpus` добавляет отчёт о том, сколько строк корпуса в выгрузку не попало.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter


def top_dir(key: str) -> str:
    """Верхний уровень пути строки: `evt/ev01/msg_x|...` → `evt`."""
    path = key.split("|", 1)[0]
    if not path:
        return "<без пути>"
    return path.split("/", 1)[0]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xlat.export")
    ap.add_argument("--store", default="../work/t9n/cache.sqlite")
    ap.add_argument("--out", default="../translations/ru.jsonl")
    ap.add_argument("--corpus", default="", help="корпус — для отчёта о непереведённом")
    ap.add_argument("--all", action="store_true", help="выгружать и неудачные строки")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.store):
        print(f"нет кэша: {args.store}")
        return 1

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    db = sqlite3.connect(args.store)
    where = "" if args.all else "WHERE status='ok'"
    rows = db.execute(
        f"SELECT key, src, ru, status, model, prompt_ver FROM t {where} ORDER BY key"
    ).fetchall()

    chars = 0
    dirs: Counter[str] = Counter()
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        for key, src, ru, status, model, pver in rows:
            rec = {"key": key, "src": src, "ru": ru, "status": status}
            if model:
                rec["model"] = model
            if pver:
                rec["prompt_ver"] = pver
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            chars += len(ru or "")
            dirs[top_dir(key)] += 1

    size = os.path.getsize(args.out)
    print(f"выгружено строк: {len(rows):,} → {args.out} ({size / 1048576:.1f} МБ)")
    print(f"символов в переводе: {chars:,}")
    print("по разделам: " + ", ".join(f"{k}={v:,}" for k, v in dirs.most_common()))

    counts = dict(db.execute("SELECT status, COUNT(*) FROM t GROUP BY status"))
    print(f"в кэше всего: {counts.get('ok', 0):,} ok, {counts.get('failed', 0):,} failed")

    if args.corpus and os.path.isfile(args.corpus):
        have = {r[0] for r in db.execute("SELECT key FROM t WHERE status='ok'")}
        missing: Counter[str] = Counter()
        total = 0
        for line in open(args.corpus, encoding="utf-8"):
            r = json.loads(line)
            total += 1
            if r["key"] not in have:
                missing[top_dir(r["key"])] += 1
        left = sum(missing.values())
        print(f"корпус: {total:,} строк, переведено {total - left:,}, "
              f"осталось {left:,} ({100 * left / max(total, 1):.1f}%)")
        print("осталось по разделам: " + ", ".join(f"{k}={v:,}" for k, v in missing.most_common(10)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
