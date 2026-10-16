"""Ручные правки перевода — `config/manual_overrides.csv`.

Правки подставляются поверх кэша на шаге `apply`, поэтому переживают любой
повторный прогон модели: машина их не перезаписывает.

    python -m xlat.manual find "Хвост ловкого"                  # найти строки
    python -m xlat.manual set --src "Dextrous Bunnit Tail" --ru "Хвост баннита"
    python -m xlat.manual set --key "game/system|msg_item_collection|419|name" --ru "..."
    python -m xlat.manual list                                  # все правки
    python -m xlat.manual unset --src "Dextrous Bunnit Tail"

После правок пересобрать: `apply` → `pack` → `dist` (см. `docs/EDITING.md`).
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import tempfile


def load_rows(path: str) -> list[list[str]]:
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        out = []
        for row in csv.reader(f):
            if not row or (row[0] == "key" and len(row) > 1 and row[1] == "ru"):
                continue
            if len(row) >= 2 and row[0].strip():
                out.append([row[0].strip(), row[1]])
        return out


def save_rows(path: str, rows: list[list[str]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["key", "ru"])
        w.writerows(rows)
    os.replace(tmp, path)


def cache_lookup(store_path: str, key: str) -> tuple[str, str] | None:
    if not os.path.isfile(store_path):
        return None
    db = sqlite3.connect(store_path)
    row = db.execute("SELECT src, ru FROM t WHERE key=?", (key,)).fetchone()
    return (row[0], row[1]) if row else None


def resolve_key(store_path: str, args) -> str | None:
    """Ключ из `--key` или по точному/частичному совпадению исходного текста."""
    if args.key:
        return args.key
    if not args.src:
        print("нужен --key или --src")
        return None
    if not os.path.isfile(store_path):
        print(f"нет кэша: {store_path}")
        return None
    db = sqlite3.connect(store_path)
    rows = db.execute("SELECT key, src, ru FROM t WHERE src=?", (args.src,)).fetchall()
    if not rows:
        rows = db.execute(
            "SELECT key, src, ru FROM t WHERE src LIKE ? LIMIT 15", (f"%{args.src}%",)
        ).fetchall()
    if not rows:
        print(f"ничего не найдено по {args.src!r}")
        return None
    exact = [r for r in rows if r[1] == args.src]
    if len(exact) == 1:
        return exact[0][0]
    if len(rows) == 1:
        return rows[0][0]
    print(f"совпадений {len(rows)} — уточните --key:")
    for key, src, ru in rows:
        print(f"  {key}\n     en: {src[:70]!r}\n     ru: {(ru or '')[:70]!r}")
    return None


def cmd_find(args) -> int:
    if not os.path.isfile(args.store):
        print(f"нет кэша: {args.store}")
        return 1
    db = sqlite3.connect(args.store)
    like = f"%{args.text}%"
    rows = db.execute(
        "SELECT key, src, ru FROM t WHERE src LIKE ? OR ru LIKE ? LIMIT ?",
        (like, like, args.limit),
    ).fetchall()
    overrides = {k: ru for k, ru in load_rows(args.overrides)}
    print(f"найдено: {len(rows)}")
    for key, src, ru in rows:
        mark = "  [ручная правка]" if key in overrides else ""
        print(f"  {key}{mark}")
        print(f"     en: {src[:78]!r}")
        print(f"     ru: {(ru or '')[:78]!r}" + (f"  →  {overrides[key]!r}" if key in overrides else ""))
    return 0


def cmd_set(args) -> int:
    key = resolve_key(args.store, args)
    if not key or args.ru is None:
        return 2
    rows = load_rows(args.overrides)
    for row in rows:
        if row[0] == key:
            row[1] = args.ru
            break
    else:
        rows.append([key, args.ru])
    save_rows(args.overrides, rows)
    known = cache_lookup(args.store, key)
    if known:
        print(f"правка записана: {key}")
        print(f"  было: {(known[1] or '')[:70]!r}")
        print(f"  стало: {args.ru[:70]!r}")
    else:
        print(f"правка записана: {key} → {args.ru[:70]!r}")
    print("пересобрать: apply → pack → dist")
    return 0


def cmd_unset(args) -> int:
    key = resolve_key(args.store, args)
    if not key:
        return 2
    rows = load_rows(args.overrides)
    left = [r for r in rows if r[0] != key]
    if len(left) == len(rows):
        print(f"правки для {key} не было")
        return 0
    save_rows(args.overrides, left)
    print(f"правка удалена: {key}")
    return 0


def cmd_list(args) -> int:
    rows = load_rows(args.overrides)
    print(f"ручных правок: {len(rows)}")
    for key, ru in rows:
        known = cache_lookup(args.store, key)
        if not known:
            note = "нет в кэше"
        elif known[1] == ru:
            note = "совпадает с кэшем (правка не нужна)"
        else:
            note = "отличается от кэша"
        print(f"  {key}  [{note}]")
        print(f"     было: {(known[1] if known else '')!r}")
        print(f"     стало: {ru!r}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xlat.manual", description=__doc__.splitlines()[0])
    ap.add_argument("--store", default="../work/t9n/cache.sqlite")
    ap.add_argument("--overrides", default="../config/manual_overrides.csv")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("find", help="искать строки по тексту оригинала или перевода")
    p.add_argument("text")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("set", help="добавить или заменить правку")
    p.add_argument("--key")
    p.add_argument("--src", help="точный текст оригинала")
    p.add_argument("--ru", required=True)
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("unset", help="удалить правку")
    p.add_argument("--key")
    p.add_argument("--src")
    p.set_defaults(func=cmd_unset)

    p = sub.add_parser("list", help="показать все правки")
    p.set_defaults(func=cmd_list)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
