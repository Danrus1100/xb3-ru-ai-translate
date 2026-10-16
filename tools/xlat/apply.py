"""Подстановка переводов в JSON-таблицы: `work/bdat_json` → `work/bdat_json_ru`.

Берёт: корпус (`corpus.jsonl` — соответствие ключ → файл/таблица/строка/колонка),
кэш переводов (`cache.sqlite`, status='ok') и ручные правки (`config/manual_overrides.csv`,
формат `key,ru`, приоритет выше кэша).

Запуск (из каталога `tools`):
    python -m xlat.apply --corpus ../work/corpus/corpus.jsonl --store ../work/t9n/cache.sqlite \
        --json ../work/bdat_json --out ../work/bdat_json_ru --overrides ../config/manual_overrides.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3

from . import validate as V


def load_overrides(path: str) -> dict[str, str]:
    if not path or not os.path.isfile(path):
        return {}
    out = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            key, ru = (row.get("key") or "").strip(), row.get("ru")
            if key and ru:
                out[key] = ru
    return out


LANGS = ("gb", "jp", "cn", "tw", "kr", "fr", "ge", "it", "sp")


def table_json_path(dir_path: str, table: str) -> str | None:
    """Путь к JSON таблицы в каталоге.

    У многотабличных файлов JSON называется именем таблицы (`msg_mnu_title.json`),
    а у однотабличных (события, реплики) — хешем (`047B9609.json`), поэтому если
    точного имени нет и в каталоге ровно один JSON, берём его.
    """
    exact = os.path.join(dir_path, f"{table}.json")
    if os.path.isfile(exact):
        return exact
    if os.path.isdir(dir_path):
        jsons = [n for n in os.listdir(dir_path) if n.endswith(".json")]
        if len(jsons) == 1:
            return os.path.join(dir_path, jsons[0])
    return None


def resolve_table_file(json_root: str, path: str, table: str) -> str | None:
    """Ищет JSON таблицы, устойчиво к отсутствию языкового префикса в `path`."""
    dirs = []
    if path:
        dirs.append(os.path.join(json_root, path))
        if not path.startswith(tuple(l + "/" for l in LANGS)):
            for lang in LANGS:
                dirs.append(os.path.join(json_root, lang, path))
    else:
        for lang in LANGS:
            dirs.append(os.path.join(json_root, lang))
    for d in dirs:
        found = table_json_path(d, table)
        if found:
            return found
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xlat.apply")
    ap.add_argument("--corpus", default="../work/corpus/corpus.jsonl")
    ap.add_argument("--store", default="../work/t9n/cache.sqlite")
    ap.add_argument("--json", default="../work/bdat_json")
    ap.add_argument("--out", default="../work/bdat_json_ru")
    ap.add_argument("--overrides", default="../config/manual_overrides.csv")
    ap.add_argument("--only-tables", default="", help="ограничить список таблиц (через запятую)")
    args = ap.parse_args(argv)

    translations: dict[str, str] = {}
    if os.path.isfile(args.store):
        db = sqlite3.connect(args.store)
        for key, ru in db.execute("SELECT key, ru FROM t WHERE status='ok' AND ru IS NOT NULL"):
            translations[key] = ru
    overrides = load_overrides(args.overrides)
    translations.update(overrides)
    print(f"переводов: {len(translations)} (из них ручных правок: {len(overrides)})")

    wanted = {t for t in args.only_tables.split(",") if t}

    # 1) копируем дерево JSON (если ещё не скопировано)
    if not os.path.isdir(args.out):
        print(f"копирую {args.json} -> {args.out}")
        shutil.copytree(args.json, args.out)

    # 2) идём по корпусу и подставляем
    applied = skipped_missing = skipped_scope = 0
    touched: dict[str, set[str]] = {}
    dirty: set[str] = set()
    errors: list[str] = []
    # кэш открытых таблиц: (path, table) -> (file_path, data)
    cache: dict[tuple[str, str], tuple[str, dict]] = {}

    for line in open(args.corpus, encoding="utf-8"):
        rec = json.loads(line)
        if wanted and rec["table"] not in wanted and rec["path"] not in wanted:
            skipped_scope += 1
            continue
        ru = translations.get(rec["key"])
        if not ru:
            skipped_missing += 1
            continue
        key = (rec["path"], rec["table"])
        if key not in cache:
            src_path = resolve_table_file(args.json, rec["path"], rec["table"])
            if src_path is None or not os.path.isfile(src_path):
                errors.append(f"нет файла таблицы: {rec['path']}/{rec['table']}.json")
                continue
            rel = os.path.relpath(src_path, args.json)
            dst_path = os.path.join(args.out, rel)
            data = json.load(open(dst_path, encoding="utf-8"))  # работаем с копией
            cache[key] = (dst_path, data)
        dst_path, data = cache[key]
        # находим строку по $id
        row = next((r for r in data.get("rows", []) if str(r.get("$id")) == str(rec["row"])), None)
        if row is None:
            errors.append(f"{rec['key']}: нет строки $id={rec['row']}")
            continue
        if rec["col"] not in row:
            errors.append(f"{rec['key']}: нет колонки {rec['col']}")
            continue
        # проверяем теги перед подстановкой (ручные правки тоже)
        errs = V.blocking_errors(rec["src"], ru, [])
        if errs:
            errors.append(f"{rec['key']}: {errs[0]}")
            continue
        row[rec["col"]] = ru
        applied += 1
        dirty.add(dst_path)
        # путь для pack: каталог таблиц относительно корня (совпадает с раскладкой архива)
        rel_tables = os.path.relpath(os.path.dirname(dst_path), args.out).replace("\\", "/")
        touched.setdefault(rel_tables, set()).add(rec["table"])

    # 3) сохраняем изменённые таблицы (по факту правки, а не по совпадению ключей)
    saved = set()
    for (dst_path, data) in cache.values():
        if dst_path in dirty and dst_path not in saved:
            with open(dst_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            saved.add(dst_path)

    report = os.path.join(os.path.dirname(args.store), "..", "reports", "apply.tsv")
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, "w", encoding="utf-8") as f:
        f.write("path\ttables\tstatus\n")
        for path in sorted(touched):
            f.write(f"{path}\t{len(touched[path])}\tok\n")
        for e in errors[:200]:
            f.write(f"\t\t{e}\n")

    print(
        f"подставлено строк: {applied}; без перевода: {skipped_missing}; вне области: {skipped_scope}; "
        f"ошибок: {len(errors)}"
    )
    print(f"затронуто bdat-файлов: {len(touched)} (таблиц: {sum(len(v) for v in touched.values())})")
    print(f"отчёт: {report}")
    if errors:
        for e in errors[:5]:
            print("  ! " + e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
