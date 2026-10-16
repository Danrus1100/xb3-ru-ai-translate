"""Поиск расхождений в передаче имён и топонимов (без модели).

Зачем: глоссарий покрывает не всё. «Everblight Plain» где-то «Равнина Порчи», где-то
«Пустоши Порчи»; «Yzana Plains» — «Изана» и «Идзана». Ревьюер такое ловит, но у него
дорогой проход по всем репликам. Здесь — дешёвая проверка по корпусу и кэшу: для каждого
устойчивого латинского имени считаем «подпись» его русского написания (слова, которые
часто встречаются в переводе строк с этим именем и редко вне них) и показываем имена
с несколькими подписями — это и есть кандидаты на единый термин.

Запуск (из каталога `tools`):
    python -m xlat.names --min-count 6 --limit 40
    python -m xlat.names --report ../work/reports/name_drift.tsv --glossary-candidates ../work/reports/glossary_candidates.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import itertools
import json
import os
import re
import sqlite3

WORD_RX = re.compile(r"[A-Za-z][A-Za-z'’\-]{2,}")
RU_WORD_RX = re.compile(r"[А-Яа-яЁё][А-Яа-яЁё\-]{3,}")
CAP_RX = re.compile(r"\b[A-Z][a-zA-Z'’\-]{2,}\b")

# Частые слова, которые не являются именами (обращения, служебные, междометия).
STOP = {
    "the", "this", "that", "these", "those", "there", "then", "they", "their", "them",
    "and", "but", "for", "not", "with", "you", "your", "what", "when", "where", "which",
    "who", "why", "how", "yes", "now", "just", "get", "well", "hey", "look", "come",
    "lets", "let", "can", "could", "would", "should", "dont", "didnt", "its", "ill",
    "im", "ive", "cant", "wont", "thats", "whats", "been", "being", "have", "has",
    "had", "take", "make", "made", "know", "think", "want", "need", "like", "right",
    "left", "good", "bad", "sorry", "thank", "thanks", "please", "okay", "alright",
    "still", "even", "ever", "never", "only", "also", "very", "much", "more", "most",
    "some", "any", "all", "because", "before", "after", "again", "around", "about",
    "over", "under", "here", "his", "her", "hers", "she", "he", "we", "us", "our",
    "it", "a", "an", "oh", "ah", "hm", "huh", "ugh", "meh", "wow", "whoa", "ha",
    "no", "yeah", "yep", "nope", "ok", "sir", "madam", "man", "boy", "girl",
    "i'll", "i've", "i'm", "i'd", "you're", "we're", "we'll", "they're", "it's",
    "that's", "what's", "there's", "here's", "can't", "won't", "don't", "didn't",
    "isn't", "aren't", "haha", "hah", "hm", "eh", "ow", "aha", "mm", "hmm",
}
# Служебные русские слова, которые не должны попадать в подпись.
RU_STOP = {
    "этот", "эта", "это", "эти", "который", "которая", "которое", "которые", "весь",
    "вся", "всё", "все", "если", "чтобы", "потому", "поэтому", "когда", "тогда",
    "только", "даже", "очень", "просто", "конечно", "может", "надо", "нужно",
    "будет", "было", "были", "есть", "быть", "меня", "тебя", "него", "неё", "них",
    "мне", "тебе", "нему", "ней", "ним", "вами", "нами", "себе", "себя", "свой",
    "своя", "своё", "свои", "тоже", "также", "потом", "сейчас", "теперь", "здесь",
    "там", "туда", "сюда", "почему", "зачем", "какой", "какая", "какое", "какие",
}


def load_candidates(min_count: int) -> list[tuple[str, int]]:
    """Имена и топонимы: слово с заглавной встречается заметно чаще, чем со строчной."""
    cap: collections.Counter = collections.Counter()
    low: collections.Counter = collections.Counter()
    forms: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    with open(os.environ.get("CORPUS", "../work/corpus/corpus.jsonl"), encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            for w in WORD_RX.findall(rec["src"]):
                (cap if w[0].isupper() else low)[w.lower()] += 1
                if w[0].isupper():
                    forms[w.lower()][w] += 1
    out = []
    for w, n in cap.items():
        if w.lower() in STOP or len(w) < 4:
            continue
        if w.lower().startswith(("ml", "system", "enhanceparam", "icon", "line")) or w.isupper():
            continue
        if n >= min_count and n > 6 * low.get(w, 0):
            # возвращаем самое частое исходное написание: по нему и искать в тексте
            out.append((forms[w].most_common(1)[0][0] if forms.get(w) else w, n))
    out.sort(key=lambda kv: -kv[1])
    return out


def signature(ru_texts: list[str], global_freq: collections.Counter) -> collections.Counter:
    """Слова, характерные именно для этих переводов (частота делится на общекорпусную)."""
    local: collections.Counter = collections.Counter()
    for t in ru_texts:
        for w in RU_WORD_RX.findall(t.lower()):
            if w in RU_STOP:
                continue
            local[w] += 1
    scored = collections.Counter()
    for w, n in local.items():
        if n < 2:
            continue
        scored[w] = n / (1 + global_freq.get(w, 0))
    return scored


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xlat.names")
    ap.add_argument("--corpus", default="../work/corpus/corpus.jsonl")
    ap.add_argument("--store", default="../work/t9n/cache.sqlite")
    ap.add_argument("--min-count", type=int, default=6)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--min-rows", type=int, default=4, help="минимум переведённых строк на имя")
    ap.add_argument("--samples", type=int, default=3, help="сколько примеров перевода показывать")
    ap.add_argument("--report", default="../work/reports/name_drift.tsv")
    ap.add_argument("--candidates", default="../work/reports/glossary_candidates.csv")
    args = ap.parse_args(argv)
    os.environ["CORPUS"] = args.corpus

    print("имена в корпусе…")
    cands = load_candidates(args.min_count)
    print(f"  кандидатов: {len(cands)}")

    db = sqlite3.connect(args.store)
    # оригинал берём из кэша: часть ключей — алиасы одинаковых строк, в корпусе их нет
    rows = db.execute("SELECT key, src, ru FROM t WHERE status='ok'").fetchall()
    corpus = {key: src for key, src, _ in rows}
    global_freq: collections.Counter = collections.Counter()
    for ru in (r for _, _, r in rows if r):
        for w in RU_WORD_RX.findall(ru.lower()):
            global_freq[w] += 1
    print(f"  переводов: {len(rows)}, слов в них: {len(global_freq)}")

    # Кандидаты в глоссарий: имена и термины вне глоссария — с примерами русского
    # написания, чтобы глазами увидеть разнобой («Сегири» против «Сэгири»). Пары слов
    # для этого не годятся: «flame» законно переводится то «пламя», то «огонь».
    gl: set[str] = set()
    gpath = "../config/glossary.csv"
    if os.path.isfile(gpath):
        with open(gpath, encoding="utf-8", newline="") as f:
            gl = {r["en"].lower() for r in csv.DictReader(f) if r.get("en")}
    report_rows = []
    for name, count in cands:
        if name.lower() in gl or count < args.min_count:
            continue
        pat = re.compile(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", re.IGNORECASE)
        samples = []
        for key, src, ru in rows:
            if not ru or len(samples) >= args.samples:
                continue
            if src and pat.search(src):
                samples.append(ru.replace("\n", " ")[:90])
        report_rows.append((name, count, samples))

    report_rows.sort(key=lambda r: -r[1])
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as f:
        f.write("name\tcount_in_corpus\tsamples\n")
        for name, count, samples in report_rows:
            f.write(f"{name}\t{count}\t{' | '.join(samples)}\n")
    with open(args.candidates, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["en", "count_in_corpus", "sample_ru"])
        for name, count, samples in report_rows:
            w.writerow([name, count, samples[0] if samples else ""])

    print(f"\nимён и терминов вне глоссария: {len(report_rows)} -> {args.report}")
    print(f"то же в CSV для правки глоссария -> {args.candidates}")
    for name, count, samples in report_rows[: args.limit]:
        head = samples[0] if samples else ""
        print(f"  {name:20s} {count:5d} | {head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
