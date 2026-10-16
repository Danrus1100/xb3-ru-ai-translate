"""Чтение JSON-таблиц, полученных `bdat-toolset extract -f json`.

Формат одного JSON-файла (одна таблица):
    {"schema": [{"name": "<col>", "type": <int>}, ...],
     "rows":   [{"$id": <int>, "<col>": <value>, ...}, ...]}

Типы ячеек (docs/MODERN.md bdat-rs): 7 = String, 11 = Debug String,
остальные — числа/хеши/ссылки. Имена таблиц и колонок хранятся в файлах
хешированными и подставляются через `--hashes <namehashes.txt>`.
"""

import json
import os

STRING_TYPES = {7, 11}

# Колонки таблиц сообщений, которые не являются текстом для перевода
NON_TEXT_COLS = {
    "label",
    "ID",
    "style",
    "talker",
    "talkattr",
    "talkattr2",
    "talkattr3",
}

# Колонки таблиц данных, в которых встречается литеральный текст
COL_TEXT_RX_NAMES = (
    "Name",
    "QuestTitle",
    "Summary",
    "ResultA",
    "ResultB",
    "Description",
    "Caption",
)

_HASH_RX = None


def _hash_like(name: str) -> bool:
    """`<8C7DD24D>` — неразрешённый хеш."""
    return name.startswith("<") and name.endswith(">") and len(name) == 10


def load_table(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def table_id(path):
    return os.path.splitext(os.path.basename(path))[0]


def string_columns(schema):
    return [c["name"] for c in schema if c["type"] in STRING_TYPES]


def is_natural_language(value: str) -> bool:
    """Грубая эвристика «это текст, а не идентификатор/число/тег»."""
    v = value.strip()
    if len(v) < 3:
        return False
    if v.isdigit():
        return False
    if v.startswith("<") and v.endswith(">"):
        return False
    if v.startswith("[") and v.endswith("]"):
        return False
    # идентификаторы и КОНСТАНТЫ: DOF_DEFAULT, EFFECT_02, STORY
    if "_" in v and " " not in v:
        return False
    if v.isupper() and " " not in v:
        return False
    if not any(ch.islower() for ch in v) and " " not in v:
        return False
    words = v.split()
    if len(words) >= 2 and any(len(w) >= 3 and w.isalpha() for w in words):
        return True
    return any(ch.islower() for ch in v)


def text_columns_for_table(table_name, schema):
    """Колонки, из которых берём текст: правила R1/R2 из плана."""
    cols = []
    for c in schema:
        name = c["name"]
        if c["type"] not in STRING_TYPES:
            continue
        if table_name.startswith("msg_"):
            if name in NON_TEXT_COLS:
                continue
            cols.append(name)
            continue
        if name in NON_TEXT_COLS or "debug" in name.lower() or name.startswith("field_"):
            continue
        if name in COL_TEXT_RX_NAMES or any(
            k in name for k in ("Text", "Title", "Message", "Desc", "name")
        ):
            cols.append(name)
    return cols


def rows_in_order(table):
    return sorted(table.get("rows", []), key=lambda r: r.get("$id", 0))


def label_of(row):
    for name in ("label", "ID"):
        if name in row:
            return str(row[name])
    return ""


def iter_bdat(root):
    """Обходит дерево `work/bdat_json`, отдавая (rel_path, table_json_path)."""
    for dirpath, _dirs, files in os.walk(root):
        for f in sorted(files):
            if f.endswith(".json"):
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root).replace("\\", "/")
                yield rel, full


DATA_STEMS = ("btl", "des", "evt", "fld", "map", "mnu", "prg", "qst", "sys")


def looks_like_hash(name: str) -> bool:
    return len(name) == 8 and all(c in "0123456789ABCDEFabcdef<>" for c in name)


def iter_text_records(json_root, lang):
    """Отдаёт текстовые записи набора `lang` в порядке чтения.

    Каждая запись — dict: {path, table, row, label, col, text}.
    У однотабличных файлов (события, реплики) имя таблицы не разрешается
    из хеша, поэтому берётся имя bdat-файла (так же поступает xb3tool).
    """
    raw = []
    roots = [(os.path.join(json_root, lang), lang)]
    roots += [(os.path.join(json_root, s), s) for s in DATA_STEMS]
    for root, prefix in roots:
        if not os.path.isdir(root):
            continue
        for rel, full in iter_bdat(root):
            sub = rel.rsplit("/", 1)[0] if "/" in rel else ""
            # путь должен повторять раскладку архива: gb/evt/ev01/msg_..., qst, ...
            bdat_path = f"{prefix}/{sub}" if sub else prefix
            table_name = table_id(full)
            try:
                table = load_table(full)
            except (OSError, ValueError):
                continue
            schema = table.get("schema") or []
            cols = text_columns_for_table(table_name, schema)
            if not cols:
                continue
            raw.append((bdat_path, table_name, table, cols))

    counts = {}
    for path, _table, _t, _c in raw:
        counts[path] = counts.get(path, 0) + 1

    records = []
    for bdat_path, table_name, table, cols in raw:
        if counts[bdat_path] == 1 and looks_like_hash(table_name):
            table_name = os.path.basename(bdat_path)
        is_msg = table_name.startswith("msg_")
        for row in rows_in_order(table):
            rid = row.get("$id", "")
            label = label_of(row)
            for col in cols:
                value = row.get(col)
                if not isinstance(value, str) or not value.strip():
                    continue
                value = value.replace("\r", " ")
                # в таблицах данных колонка с «текстовым» именем может содержать
                # идентификаторы (DOF_DEFAULT, EFFECT_02) — их отсекаем эвристикой
                if not is_msg and not is_natural_language(value):
                    continue
                records.append(
                    {
                        "path": bdat_path,
                        "table": table_name,
                        "row": rid,
                        "label": label,
                        "col": col,
                        "text": value,
                    }
                )
    records.sort(key=lambda r: (r["path"], r["table"], r["row"]))
    return records
