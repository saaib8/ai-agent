"""The warranty length a store wrote in a product's description.

Most stores leave the warranty field empty and write "Warranty: Two years" or
"a five-year warranty" in the description instead. The description is free
text and never a source of figures (CLAUDE.md 37), so the length is read here,
in code, only where it stands right beside the word warranty - and given to an
agent as its own fact, said as the store's words. A description that states two
different lengths states none we can repeat: nothing is given rather than one
guessed.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

MAX_WARRANTY_MONTHS = 360
"""Thirty years: a longer figure beside "warranty" is not a warranty length."""

_EN_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "eighteen": 18,
    "twenty": 20,
    "twenty-four": 24,
    "thirty": 30,
    "thirty-six": 36,
}
_AR_WORDS = {
    "واحد": 1,
    "واحدة": 1,
    "ثلاث": 3,
    "ثلاثة": 3,
    "أربع": 4,
    "اربع": 4,
    "أربعة": 4,
    "اربعة": 4,
    "خمس": 5,
    "خمسة": 5,
    "ست": 6,
    "ستة": 6,
    "سبع": 7,
    "سبعة": 7,
    "ثمان": 8,
    "ثماني": 8,
    "ثمانية": 8,
    "تسع": 9,
    "تسعة": 9,
    "عشر": 10,
    "عشرة": 10,
}
_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

_EN_NUM = r"(?P<num>\d{1,3}|" + "|".join(sorted(_EN_WORDS, key=len, reverse=True)) + r")"
_EN_UNIT = r"(?P<unit>years?|months?)"
_EN_WARRANTY = r"(?:warranty|guarantee)"
_EN_PATTERNS = (
    # "Warranty: Two years", "warranty of 5 years", "guarantee for 18 months"
    re.compile(
        rf"\b{_EN_WARRANTY}\b[^.;!?\n\-\u2013]{{0,25}}?\b{_EN_NUM}[\s-]*{_EN_UNIT}\b", re.IGNORECASE
    ),
    # "a five-year warranty", "2 years warranty", "10 years of guarantee"
    re.compile(
        rf"\b{_EN_NUM}[\s-]*{_EN_UNIT}[\s-]*(?:of\s+|full\s+)?{_EN_WARRANTY}\b", re.IGNORECASE
    ),
)

_AR_NUM = (
    r"(?P<num>[0-9\u0660-\u0669]{1,3}|" + "|".join(sorted(_AR_WORDS, key=len, reverse=True)) + r")"
)
_AR_UNIT = r"(?P<unit>سنوات|سنة|سنه|سنين|أعوام|اعوام|عام|أشهر|اشهر|شهور|شهرا|شهراً|شهر)"
_AR_WARRANTY = r"(?:(?<!\w)|(?<=(?<!\w)ال))ضمان"
"""The word warranty ("ضمان", "الضمان"), never inside "لضمان" - "to ensure"."""
_AR_GAP = r"[^.؛!؟\n\-\u2013]{0,15}?"
_AR_PATTERNS = (
    # "ضمان 5 سنوات", "ضمان لمدة خمس سنوات"
    re.compile(rf"{_AR_WARRANTY}{_AR_GAP}{_AR_NUM}\s*{_AR_UNIT}"),
)
_AR_WORD_GAP = r"[^.؛!؟\n\-\u20130-9\u0660-\u0669]{0,15}?"
"""Before a length said in one word, no figure: "ضمان 2 سنة" is two years, not one."""
_AR_PAIR = re.compile(rf"{_AR_WARRANTY}{_AR_WORD_GAP}(?:سنتين|سنتان|عامين|عامان)")
"""Arabic says "two years" in one word."""
_AR_ONE = re.compile(
    rf"{_AR_WARRANTY}{_AR_WORD_GAP}(?:لمدة\s+)?(?:سنة|سنه|عام)(?!\s*[0-9\u0660-\u0669])\b"
)
"""...and "one year" by the unit alone: "ضمان سنة"."""


class ListedWarranty(BaseModel):
    """One warranty length, in whole years where it is whole years."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    years: int | None = Field(default=None, ge=1)
    months: int | None = Field(default=None, ge=1)


def warranty_in(english: str | None, stored: str | None) -> ListedWarranty | None:
    """The one warranty length the store's description states - its English
    text first, else the stored text - or None when it states none, or more
    than one."""
    for text in (english, stored):
        if not text:
            continue
        lengths = _lengths(text)
        if lengths:
            return _as_listed(lengths.pop()) if len(lengths) == 1 else None
    return None


def _lengths(text: str) -> set[int]:
    """Every warranty length the text states, in months."""
    found: set[int] = set()
    for pattern in _EN_PATTERNS:
        for match in pattern.finditer(text):
            found.add(_months(_en_number(match.group("num")), match.group("unit").lower()))
    for pattern in _AR_PATTERNS:
        for match in pattern.finditer(text):
            found.add(_months(_ar_number(match.group("num")), _ar_unit(match.group("unit"))))
    found.update(24 for _ in _AR_PAIR.finditer(text))
    found.update(12 for _ in _AR_ONE.finditer(text))
    return {m for m in found if 0 < m <= MAX_WARRANTY_MONTHS}


def _en_number(raw: str) -> int:
    word = raw.lower()
    return _EN_WORDS[word] if word in _EN_WORDS else int(word)


def _ar_number(raw: str) -> int:
    return _AR_WORDS[raw] if raw in _AR_WORDS else int(raw.translate(_INDIC))


def _ar_unit(raw: str) -> str:
    return "month" if raw.startswith(("ش", "أش", "اش")) else "year"


def _months(number: int, unit: str) -> int:
    return number if unit.startswith("month") else number * 12


def _as_listed(months: int) -> ListedWarranty:
    if months % 12 == 0:
        return ListedWarranty(years=months // 12)
    return ListedWarranty(months=months)


def in_arabic(warranty: ListedWarranty) -> str:
    """The length as Arabic says it after "لمدة": two is one word ("سنتين"),
    three to ten take the plural, so an agent never writes "2 سنتين"."""
    if warranty.years is not None:
        return _arabic_count(warranty.years, "سنة واحدة", "سنتين", "سنوات", "سنة")
    assert warranty.months is not None
    return _arabic_count(warranty.months, "شهر واحد", "شهرين", "أشهر", "شهرًا")


def _arabic_count(count: int, one: str, two: str, few: str, many: str) -> str:
    if count == 1:
        return one
    if count == 2:
        return two
    return f"{count} {few if count <= 10 else many}"
