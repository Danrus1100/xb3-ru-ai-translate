"""Загрузка переводов из `translations/ru.jsonl` в рабочий кэш.

Нужна, чтобы собрать мод из чистого клона репозитория, не прогоняя модель:
шаг `apply` читает кэш, и импортированные строки для него ничем не отличаются
от машинных.

Запуск (из каталога `tools`):
    python -m xlat.import --jsonl ../translations/ru.jsonl --store ../work/t9n/cache.sqlite
"""

from __future__ import annotations

import argparse
import json
import os

from .corpus import sha16
from .store import Store


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xlat.import")
    ap.add_argument("--jsonl", default="../translations/ru.jsonl")
    ap.add_argument("--store", default="../work/t9n/cache.sqlite")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.jsonl):
        print(f"нет файла переводов: {args.jsonl}")
        return 1

    store = Store(args.store)
    n = 0
    chars = 0
    for line in open(args.jsonl, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        src = rec.get("src") or ""
        ru = rec.get("ru")
        if not src or not ru:
            continue
        store.put(
            rec["key"],
            src,
            rec.get("src_sha") or sha16(src),
            ru,
            rec.get("status") or "ok",
            rec.get("model") or "imported",
            int(rec.get("prompt_ver") or 0),
            rec.get("glossary_sha") or "",
        )
        n += 1
        chars += len(ru)
    store.commit()
    print(f"загружено строк: {n:,} ({chars:,} символов) → {args.store}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
