"""Сборка корпуса текстов для перевода: `work/corpus/corpus.jsonl` + статистика.

Записи берутся из JSON-таблиц (`bdat-toolset extract -f json`) набора языка `gb`
(и, если есть, `jp` как reference), правила отбора — R1/R2/R3 из плана.

Запуск (из каталога `tools`):
    python -m xlat.corpus --json ../work/bdat_json --lang gb --ref-lang jp \
        --out ../work/corpus --context 4
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re

from . import bdatjson as B

TAG_RX = re.compile(r"\[[^\]]*\]|\{[^}]*\}|%[0-9]*[sd]|\\n")


def sha16(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def reference_texts(json_root: str, ref_lang: str) -> dict:
    """(path, table, row, col) -> текст на другом языке (для ссылки в промпте)."""
    out = {}
    if not ref_lang:
        return out
    for rec in B.iter_text_records(json_root, ref_lang):
        out[(rec["path"], rec["table"], str(rec["row"]), rec["col"])] = rec["text"]
    return out


def build_records(json_root: str, lang: str, ref: dict, context: int):
    """Записи с контекстом: соседи внутри той же таблицы."""
    records = []
    by_table = collections.defaultdict(list)
    for rec in B.iter_text_records(json_root, lang):
        by_table[(rec["path"], rec["table"])].append(rec)

    for (path, table), items in by_table.items():
        for idx, rec in enumerate(items):
            prev = [r["text"] for r in items[max(0, idx - context) : idx]]
            nxt = [r["text"] for r in items[idx + 1 : idx + 1 + context]]
            text = rec["text"]
            records.append(
                {
                    "key": f"{path}|{table}|{rec['row']}|{rec['col']}",
                    "path": path,
                    "lang": lang,
                    "table": table,
                    "row": rec["row"],
                    "label": rec["label"],
                    "col": rec["col"],
                    "src": text,
                    "src_sha": sha16(text),
                    "ref": ref.get((path, table, str(rec["row"]), rec["col"])),
                    "tags": TAG_RX.findall(text),
                    "prev": prev,
                    "next": nxt,
                    "chars": len(text),
                }
            )
    records.sort(key=lambda r: (r["path"], r["table"], r["row"]))
    return records


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xlat.corpus")
    ap.add_argument("--json", default="../work/bdat_json")
    ap.add_argument("--lang", default="gb")
    ap.add_argument("--ref-lang", default="jp", help="язык-подсказка (jp) или пусто")
    ap.add_argument("--out", default="../work/corpus")
    ap.add_argument("--context", type=int, default=4)
    ap.add_argument("--sample", type=int, default=0, help="напечатать N случайных записей")
    args = ap.parse_args(argv)

    ref = reference_texts(args.json, args.ref_lang)
    records = build_records(args.json, args.lang, ref, args.context)

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "corpus.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    uniq = len({r["src_sha"] for r in records})
    chars = sum(r["chars"] for r in records)
    per_file = collections.Counter(r["path"] for r in records)
    per_table = collections.Counter(r["table"] for r in records)
    stats = {
        "lang": args.lang,
        "ref_lang": args.ref_lang,
        "records": len(records),
        "unique_src": uniq,
        "chars_total": chars,
        "chars_unique": sum(r["chars"] for r in {r["src_sha"]: r for r in records}.values()),
        "est_tokens_in": round(chars / 3.3),
        "est_tokens_out": round(chars / 2.4),
        "with_ref": sum(1 for r in records if r["ref"]),
        "with_tags": sum(1 for r in records if r["tags"]),
        "tables": len(per_table),
        "files": len(per_file),
        "top_files": per_file.most_common(10),
        "top_tables": per_table.most_common(10),
    }
    with open(os.path.join(args.out, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    print(
        f"записей {len(records)} (уникальных {uniq}), символов {chars}, "
        f"таблиц {stats['tables']}, файлов {stats['files']}, с японской ссылкой {stats['with_ref']}"
    )
    print(f"оценка токенов: вход ≈ {stats['est_tokens_in']}, выход ≈ {stats['est_tokens_out']}")
    print(f"-> {out_path}")
    if args.sample:
        import random

        for rec in random.sample(records, min(args.sample, len(records))):
            print(f"  [{rec['key']}] {rec['src'][:70]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
