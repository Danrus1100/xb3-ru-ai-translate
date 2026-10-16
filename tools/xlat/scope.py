"""Область перевода: черновик (`draft`) и проверка (`check`).

`draft` сканирует JSON-таблицы набора языка и выводит кандидатов — текстовые колонки
таблиц данных (не `msg_*`), прошедшие эвристику текста: файл-обзор для ревью и черновик
`config/scope.json`. `check` считает, сколько записей корпуса попадает в зафиксированную
область.

Правило рантайма: таблицы `msg_*` переводим целиком (кроме служебных колонок), а таблицы
данных — только если для них в `scope.json` есть явный список колонок (`table_columns`).
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import re

from . import bdatjson as B

SERVICE_COLS = {"label", "ID", "style", "talker", "talkattr", "talkattr2", "talkattr3"}
COL_RX = re.compile(
    r"^(Name|.*_name|Name.*|QuestTitle|Summary|Result[AB]|Description|.*_desc|.*Desc|"
    r".*[Tt]ext.*|.*[Tt]itle.*|.*[Mm]essage.*|Caption)$"
)


def scan(json_root: str, lang: str) -> dict:
    """(table, col) -> {"count": N, "samples": [...], "paths": set()}"""
    out: dict[tuple[str, str], dict] = collections.defaultdict(
        lambda: {"count": 0, "samples": [], "paths": set()}
    )
    for rec in B.iter_text_records(json_root, lang):
        table = rec["table"]
        if table.startswith("msg_"):
            continue
        col = rec["col"]
        if col in SERVICE_COLS or not COL_RX.match(col):
            continue
        key = (table, col)
        entry = out[key]
        entry["count"] += 1
        entry["paths"].add(rec["path"])
        if len(entry["samples"]) < 3:
            entry["samples"].append(rec["text"][:80])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xlat.scope")
    ap.add_argument("cmd", choices=["draft", "check"])
    ap.add_argument("--json", default="../work/bdat_json")
    ap.add_argument("--lang", default="gb")
    ap.add_argument("--scope", default="../config/scope.json")
    ap.add_argument("--review", default="../work/reports/scope_review.tsv")
    ap.add_argument("--corpus", default="../work/corpus/corpus.jsonl")
    ap.add_argument("--min-count", type=int, default=1)
    args = ap.parse_args(argv)

    if args.cmd == "check":
        from .translate import in_scope, load_scope

        scope = load_scope(args.scope)
        total = inside = 0
        per_table = collections.Counter()
        for line in open(args.corpus, encoding="utf-8"):
            rec = json.loads(line)
            total += 1
            if in_scope(rec, scope):
                inside += 1
                per_table[rec["table"]] += 1
        print(f"записей в корпусе: {total}, в области: {inside}, вне: {total - inside}")
        print("топ таблиц в области:", per_table.most_common(10))
        return 0

    found = scan(args.json, args.lang)
    rows = []
    for (table, col), info in sorted(found.items(), key=lambda kv: -kv[1]["count"]):
        if info["count"] < args.min_count:
            continue
        rows.append(
            {
                "table": table,
                "col": col,
                "count": info["count"],
                "paths": ";".join(sorted(info["paths"])[:3]),
                "sample1": info["samples"][0] if info["samples"] else "",
                "sample2": info["samples"][1] if len(info["samples"]) > 1 else "",
            }
        )
    os.makedirs(os.path.dirname(args.review), exist_ok=True)
    with open(args.review, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["table", "col", "count", "paths", "sample1", "sample2"], delimiter="\t")
        w.writeheader()
        w.writerows(rows)
    print(f"кандидатов (таблица, колонка): {len(rows)} -> {args.review}")
    for r in rows[:25]:
        print(f"  {r['count']:6d}  {r['table']:34s} {r['col']:18s} {r['sample1'][:52]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
