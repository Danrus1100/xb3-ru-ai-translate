"""Читаемый дамп английских строк игры: `work/read/en/**`.

Запуск (из каталога `tools`):
    python -m xlat.dump_en --json work/bdat_json --out work/read/en --lang gb

Что делает: обходит JSON-таблицы (результат `bdat-toolset extract -f json`),
выбирает текстовые колонки по правилам R1/R2 (см. xc3-ru-ai-translation-plan.md)
и пишет:
    <out>/ALL.txt          — сплошной читаемый дамп (сцена за сценой)
    <out>/INDEX.tsv        — индекс: path, table, id, label, column, text
    <out>/files/<path>.txt — отдельный файл на каждый bdat-файл
    <out>/STATS.json       — счётчики (файлы, таблицы, строки, символы)
"""

import argparse
import collections
import json
import os
import re

from . import bdatjson as B

DATA_STEMS = ("btl", "des", "evt", "fld", "map", "mnu", "prg", "qst", "sys")

# Служебные теги Monolith — в читаемом дампе не нужны (в корпусе для перевода
# они сохраняются, см. план S8): [ML:undisp ], [System:Ruby], [XENO:...] и т.п.
TAG_RX = re.compile(r"\[[A-Za-z]+:[^\]]*\]\s*")


def strip_tags(text: str) -> str:
    return TAG_RX.sub("", text)


def _looks_like_hash(name: str) -> bool:
    return len(name) == 8 and all(c in "0123456789ABCDEFabcdef<>" for c in name)


def collect(json_root, lang):
    """Отдаёт список (bdat_path, table_name, rows_in_reading_order, text_columns)."""
    raw = []
    roots = [os.path.join(json_root, lang)]
    roots += [os.path.join(json_root, s) for s in DATA_STEMS]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for rel, full in B.iter_bdat(root):
            bdat_path = rel.rsplit("/", 1)[0]           # без имени JSON-файла
            table_name = B.table_id(full)
            try:
                table = B.load_table(full)
            except (OSError, ValueError):
                continue
            schema = table.get("schema") or []
            cols = B.text_columns_for_table(table_name, schema)
            if not cols:
                continue
            raw.append((bdat_path, table_name, table, cols))
    counts = collections.Counter(p for p, _n, _t, _c in raw)
    out = []
    for bdat_path, table_name, table, cols in raw:
        # У однотабличных файлов (события, реплики) имя таблицы в файле —
        # неразрешённый хеш; игра и сообщество используют имя bdat-файла.
        if counts[bdat_path] == 1 and _looks_like_hash(table_name):
            table_name = os.path.basename(bdat_path)
        out.append((bdat_path, table_name, table, cols))
    out.sort(key=lambda e: (e[0], e[1]))
    return out


def render(entries, out_dir, index_rows, stats):
    os.makedirs(out_dir, exist_ok=True)
    files_dir = os.path.join(out_dir, "files")
    all_path = os.path.join(out_dir, "ALL.txt")
    current = None
    buf = []
    with open(all_path, "w", encoding="utf-8") as allf:
        for bdat_path, table_name, table, cols in entries:
            if current != bdat_path:
                current = bdat_path
                allf.write(f"\n\n===== {bdat_path} =====\n")
                if buf:
                    _flush(files_dir, bdat_path_prev, buf)
                buf = []
            bdat_path_prev = bdat_path
            allf.write(f"\n--- {table_name} ---\n")
            buf.append(f"\n--- {table_name} ---")
            for row in B.rows_in_order(table):
                rid = row.get("$id", "")
                label = B.label_of(row)
                for col in cols:
                    value = row.get(col)
                    if not isinstance(value, str):
                        continue
                    value = value.replace("\r", " ")
                    if not value.strip():
                        continue
                    clean = strip_tags(value).strip() or value.strip()
                    if table_name.startswith("msg_"):
                        line = clean
                    else:
                        line = f"[{label or rid}] {col}: {clean}"
                    allf.write(line + "\n")
                    buf.append(line)
                    index_rows.append((bdat_path, table_name, rid, label, col, value))
                    stats["lines"] += 1
                    stats["chars"] += len(value)
            stats["tables"] += 1
        if buf:
            _flush(files_dir, current, buf)
    return all_path


def _flush(files_dir, bdat_path, lines):
    if not bdat_path:
        return
    dst = os.path.join(files_dir, bdat_path + ".txt")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="xlat.dump_en")
    ap.add_argument("--json", default="work/bdat_json")
    ap.add_argument("--out", default="work/read/en")
    ap.add_argument("--lang", default="gb")
    args = ap.parse_args(argv)

    entries = collect(args.json, args.lang)
    stats = {"files": len({e[0] for e in entries}), "tables": 0, "lines": 0, "chars": 0}
    index_rows = []
    all_path = render(entries, args.out, index_rows, stats)
    with open(os.path.join(args.out, "INDEX.tsv"), "w", encoding="utf-8") as f:
        f.write("path\ttable\tid\tlabel\tcolumn\ttext\n")
        for row in index_rows:
            f.write("\t".join(str(x).replace("\t", " ").replace("\n", " ") for x in row) + "\n")
    with open(os.path.join(args.out, "STATS.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(
        f"files={stats['files']} tables={stats['tables']} lines={stats['lines']} "
        f"chars={stats['chars']} -> {all_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
