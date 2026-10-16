"""Валидация переводов: блокирующие правила 1–5 и флаги (см. план, S8).

Правила (блокирующие, по ним идёт repair-повтор):
  1. непустой ответ, без `\\r`, без изменения ведущего/замыкающего пробела;
  2. мультимножество тегов (`[...]`, `{...}`, `%s`) совпадает с исходником;
  3. количество переводов строк совпадает;
  4. `mode=force` из глоссария: если термин есть в src — его перевод обязан быть в ru;
  5. `mode=keep`: латинское написание обязано сохраниться.

Флаги (не блокируют): LONG, LATIN, STYLE.
"""

from __future__ import annotations

import collections
import re

# `\[` — экранированная скобка (литеральный текст, а не тег), поэтому такие пары
# в разметку не попадают: иначе строки вида `\[Haha, no.\]` требуют «сохранить тег»,
# то есть перевести содержимое, оставаясь с тем же текстом.
TAG_RX = re.compile(r"(?<!\\)\[[^\]]*\]|\{[^}]*\}|%[0-9]*[sd]")
WORD_RX = re.compile(r"[A-Za-z]{2,}")
UI_TABLE_RX = re.compile(r"^(msg_mnu_|msg_sys_|MNU_|SYS_)")
LATIN_OK_RX = re.compile(r"^(A|B|X|Y|L|R|ZL|ZR|N|M|X|Z|HP|TP|EXP|DLC|Ouroboros)")


def tags(text: str) -> collections.Counter:
    return collections.Counter(TAG_RX.findall(text))


def term_present(term: str, text: str, case_sensitive: bool) -> bool:
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.search(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", text, flags) is not None


def ru_contains(needle: str, haystack: str) -> bool:
    """Сравнение без учёта регистра первой буквы (регистр слова в русском бывает разным)."""
    if not needle:
        return True
    return needle.lower() in haystack.lower()


WORD_SPLIT_RX = re.compile(r"[^\w]+", re.UNICODE)


def _stem(word: str) -> str:
    """Основа слова: отсекаем падежное окончание, чтобы «колонии» совпадало с «Колония»."""
    if len(word) >= 5:
        return word[:-2]
    if len(word) == 4:
        return word[:-1]
    return word


def ru_matches_term(term: str, ru: str, allow_inflection: bool = True) -> bool:
    """Есть ли перевод термина в тексте.

    `allow_inflection` нужен для режима force: в русском терминология склоняется
    («Колония» → «в колонию», «9-й колонии»), и требование точной формы гонит
    корректные переводы в брак. Сравниваем по основам слов.
    """
    if not term:
        return True
    if ru_contains(term, ru):
        return True
    if not allow_inflection:
        return False
    low = ru.lower()
    for word in [w for w in WORD_SPLIT_RX.split(term.lower()) if w]:
        if _stem(word) not in low:
            return False
    return True


def blocking_errors(src: str, ru: str, glossary: list[dict]) -> list[str]:
    errors = []
    if not ru or not ru.strip():
        errors.append("пустой перевод")
        return errors
    if "\r" in ru:
        errors.append("есть \\r")
    # Значимы краевые ПРОБЕЛЫ (склейка строк в интерфейсе), а переводы строк — нет:
    # они отдельно проверяются ниже по количеству.
    if (src.startswith(" ") or src.endswith(" ")) and not (
        ru.startswith((" ", "\u00a0")) or ru.endswith((" ", "\u00a0"))
    ):
        errors.append("потеряны краевые пробелы")
    src_tags, ru_tags = tags(src), tags(ru)
    if src_tags != ru_tags:
        missing = (src_tags - ru_tags).most_common(5)
        extra = (ru_tags - src_tags).most_common(5)
        errors.append(f"теги не совпадают (нет {missing}, лишние {extra})")
    if src.count("\n") != ru.count("\n"):
        # Переупаковка строки сама по себе не порча текста: движок раскладывает текст по
        # рамке сам, а переводы на другие языки всё равно другой длины. Опасно сочетание
        # «переносов меньше И текст длиннее» — вот его и блокируем, остальное помечаем NL.
        if ru.count("\n") < src.count("\n") and len(ru) > len(src) + 8:
            errors.append(
                f"переводов строк {ru.count(chr(10))} вместо {src.count(chr(10))} "
                f"при переводе длиннее исходного"
            )
    for row in glossary:
        en, target, mode = row["en"], row["ru"], row.get("mode", "suggest")
        if mode not in ("force", "keep"):
            continue
        if not term_present(en, src, row.get("case_sensitive") == "true"):
            continue
        if mode == "force" and not ru_matches_term(target, ru):
            errors.append(f"глоссарий force: нет «{target}» для {en!r}")
        if mode == "keep":
            # Однобуквенные записи (N, M, X, Z — «буквы-имена») не должны ловиться
            # внутри заиканий: «M-my body feels…» — это не имя, а обрыв слова.
            if len(en) == 1:
                # буква-имя: она должна стоять отдельно и не быть сокращением («N. Gate»)
                if re.search(re.escape(en) + r"\s*[.:]", src):
                    continue
                if not re.search(
                    r"(?<![A-Za-z\-'\u2019])" + re.escape(en) + r"(?![A-Za-z\-'\u2019])", src
                ):
                    continue
            if not ru_contains(en, ru):
                errors.append(f"глоссарий keep: потеряно латинское {en!r}")
    return errors


def flags_for(src: str, ru: str, table: str, glossary: list[dict]) -> list[str]:
    out = []
    ui = bool(UI_TABLE_RX.match(table))
    limit = 1.2 * len(src) + 4 if ui else 1.6 * len(src) + 10
    if len(ru) > limit:
        out.append("LONG")
    if src.count("\n") != ru.count("\n"):
        out.append("NL")
    latin_words = [w for w in WORD_RX.findall(ru)]
    if len(latin_words) >= 3:
        covered = any(term_present(r["en"], ru, False) for r in glossary) or LATIN_OK_RX.search(ru)
        if not covered:
            out.append("LATIN")
    if '"' in ru or "..." in ru or (ui is False and " - " in ru):
        out.append("STYLE")
    return out
