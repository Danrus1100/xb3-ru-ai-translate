"""Предпросмотр того, что уйдёт в API: батчи, контекст, глоссарий, payload.

Запуск (из каталога `tools`):
    python -m xlat.preview --json ../work/bdat_json --lang gb --batch 20 --context 4
    python -m xlat.preview ... --show msg_ev01220100 --max-payloads 1

Без `--show` печатает только статистику: сколько запросов, сколько строк,
оценку токенов/стоимости. С `--show` печатает точные system/user сообщения
для запрошенной сцены (то, что реально уйдёт в OpenAI-совместимый эндпоинт).
"""

import argparse
import csv
import json
import math
import os
import re

from . import bdatjson as B

CHARS_PER_TOKEN_IN = 3.3
CHARS_PER_TOKEN_OUT = 2.4


def load_glossary(path):
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("en") and row.get("ru"):
                rows.append(row)
    return rows


def glossary_for(texts, glossary):
    """Подмножество глоссария: совпавшие термины + все `always=1`."""
    blob = "\n".join(texts)
    picked = []
    seen = set()
    for row in glossary:
        en = row["en"]
        if row.get("always") == "1":
            hit = True
        else:
            flags = 0 if row.get("case_sensitive") == "true" else re.IGNORECASE
            hit = re.search(r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])", blob, flags) is not None
        if hit and en not in seen:
            picked.append(row)
            seen.add(en)
    return picked


def render_glossary(rows):
    out = []
    for r in rows:
        mode = r.get("mode", "suggest")
        suffix = " (keep latin)" if mode == "keep" else ""
        out.append(f"{r['en']} => {r['ru']}{suffix}")
    return "\n".join(out) if out else "(нет совпадений)"


def build_batches(records, batch_size):
    """Блоки не пересекают границу таблицы (сцены)."""
    out, cur, key = [], [], None
    for rec in records:
        k = (rec["path"], rec["table"])
        if k != key:
            if cur:
                out.append(cur)
            cur, key = [], k
        cur.append(rec)
        if len(cur) >= batch_size:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def payloads(records, batch_size, context, glossary, prompt_tpl, style):
    """Отдаёт список (batch_records, system_text, user_text)."""
    by_key = {}
    for i, rec in enumerate(records):
        by_key.setdefault((rec["path"], rec["table"]), []).append(i)
    out = []
    for batch in build_batches(records, batch_size):
        idxs = by_key[(batch[0]["path"], batch[0]["table"])]
        # позиция первого элемента батча внутри таблицы
        pos = next(k for k in range(len(idxs)) if records[idxs[k]] is batch[0])
        prev = [records[idxs[k]]["text"] for k in range(max(0, pos - context), pos)]
        last = pos + len(batch) - 1
        nxt = [records[idxs[k]]["text"] for k in range(last + 1, min(len(idxs), last + 1 + context))]
        items = []
        for n, rec in enumerate(batch):
            k = pos + n
            items.append(
                {
                    "i": n,
                    "src": rec["text"],
                    "prev": [records[idxs[j]]["text"] for j in range(max(0, k - context), k)],
                    "next": [records[idxs[j]]["text"] for j in range(k + 1, min(len(idxs), k + 1 + context))],
                }
            )
        texts = [it["src"] for it in items] + prev + nxt
        gl = render_glossary(glossary_for(texts, glossary))
        system = prompt_tpl.replace("{style}", style).replace("{glossary}", gl)
        user = json.dumps(
            {"scene": batch[0]["path"] + "/" + batch[0]["table"], "items": items},
            ensure_ascii=False,
        )
        out.append((batch, system, user, gl))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="xlat.preview")
    ap.add_argument("--json", default="../work/bdat_json")
    ap.add_argument("--lang", default="gb")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--context", type=int, default=4)
    ap.add_argument("--glossary", default="../config/glossary.csv")
    ap.add_argument("--prompt", default="../config/prompt.md")
    ap.add_argument("--style", default="../config/style_ru.md")
    ap.add_argument("--show", default=None, help="подстрока пути bdat для печати payload")
    ap.add_argument("--max-payloads", type=int, default=1)
    ap.add_argument("--price-in", type=float, default=0.0, help="цена за 1k входных токенов")
    ap.add_argument("--price-out", type=float, default=0.0, help="цена за 1k выходных токенов")
    args = ap.parse_args(argv)

    records = B.iter_text_records(args.json, args.lang)
    glossary = load_glossary(args.glossary)
    style = open(args.style, encoding="utf-8").read() if os.path.isfile(args.style) else ""
    prompt_tpl = open(args.prompt, encoding="utf-8").read() if os.path.isfile(args.prompt) else "{style}\n{glossary}"
    prompt_tpl = re.sub(r"^<!--.*?-->\s*", "", prompt_tpl, flags=re.S)

    pls = payloads(records, args.batch, args.context, glossary, prompt_tpl, style)
    uniq = len({r["text"] for r in records})
    chars_in = sum(len(u) + len(s) for _b, s, u, _g in pls)
    chars_out = sum(len(r["text"]) for batch, _s, _u, _g in pls for r in batch) * 1.15
    tok_in = math.ceil(chars_in / CHARS_PER_TOKEN_IN)
    tok_out = math.ceil(chars_out / CHARS_PER_TOKEN_OUT)

    print(f"строк: {len(records)}  уникальных: {uniq}  батчей: {len(pls)} (batch={args.batch}, context={args.context})")
    print(f"оценка токенов: вход ≈ {tok_in:,}  выход ≈ {tok_out:,}".replace(",", " "))
    if args.price_in or args.price_out:
        cost = tok_in / 1000 * args.price_in + tok_out / 1000 * args.price_out
        print(f"оценка стоимости при $ {args.price_in}/{args.price_out} за 1k: $ {cost:.2f}")
    sizes = [len(b) for b, _s, _u, _g in pls]
    print(f"размер батча: min={min(sizes)} max={max(sizes)} avg={sum(sizes)/len(sizes):.1f}")

    if args.show:
        shown = 0
        for batch, system, user, gl in pls:
            if args.show in batch[0]["path"]:
                print("\n" + "=" * 78)
                print(f"SCENE {batch[0]['path']}/{batch[0]['table']}  строк в батче: {len(batch)}")
                print("-" * 78 + "\nSYSTEM:\n")
                print(system)
                print("-" * 78 + "\nUSER:\n")
                print(user)
                shown += 1
                if shown >= args.max_payloads:
                    break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
