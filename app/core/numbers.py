"""Reading a figure a model wrote down.

Prices, percentages and measurements arrive from model output as strings, so
money never passes through a binary float. `Decimal` accepts far more than a
customer could have meant, though: "NaN", "Infinity", a signalling "sNaN", and
exponents like "1e999999". None of those is a figure anyone said, and the
last two do not fail when parsed - they fail later, inside arithmetic such as
a unit conversion, as `decimal` exceptions no caller expects.

So there is one reader, and every place that turns model output into a number
goes through it. Each caller still decides what an unreadable figure means for
it: a composition defect, or an unusable model answer.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

MAX_STATED_MAGNITUDE = Decimal("1e12")
"""A ceiling no real price or measurement comes near.

Not a business limit - a guard against numbers that only a malfunction
produces. A trillion riyals, or a trillion millimetres, is still comfortably
inside what `Decimal` arithmetic and a PostgreSQL NUMERIC comparison handle.
"""

MIN_STATED_MAGNITUDE = Decimal("1e-6")
"""The floor for a non-zero figure, for the same reason as the ceiling.

"1e-999999" is finite and parses, but no customer means a millionth of a
centimetre, and an exponent that small overflows a NUMERIC bind parameter.
Zero itself is allowed through: whether zero is meaningful is the caller's
contract.
"""


_GROUPED = re.compile(r"[+-]?[1-9]\d{0,2}(,\d{3})+(\.\d+)?")
"""Money grouped in thousands by commas: "5,000", "1,250,000.50".

Deliberately strict, and money only. "2,5" and "0,500" are left alone rather
than read as thousands, because a comma that is not certainly a thousands
separator may be a decimal comma - and a misread figure is worse than a
refused one. Lengths never accept grouping: "1,500 m" is not a real room.
"""

_DOT_GROUPED = re.compile(r"[+-]?\d{1,3}\.\d{3}")
"""A money figure like "5.000": five, or five thousand in dot-grouping locales.

Refused rather than guessed. Prices carry at most two decimals, so three digits
after the dot is exactly the case where the two readings disagree.
"""

_THOUSANDS_SUFFIX = re.compile(r"(?P<figure>.+?)\s*[kK]")


def parse_stated_decimal(raw: str) -> Decimal:
    """A finite figure of sane magnitude, or `ValueError`.

    Sign is deliberately not checked here: whether a negative is meaningful is
    the caller's contract, and the domain models already refuse the ones that
    are not.
    """
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ValueError("not a decimal figure") from exc
    if not value.is_finite():
        raise ValueError("not a finite figure")
    if not value:
        # "0E-999999" is zero with an exponent no NUMERIC column accepts.
        return Decimal(0)
    if abs(value) > MAX_STATED_MAGNITUDE or (value and abs(value) < MIN_STATED_MAGNITUDE):
        raise ValueError("figure out of range")
    return value


def parse_stated_amount(raw: str) -> Decimal:
    """A money figure, as people write money: "5,000", "5k", "12.5K".

    Money only. Grouping and "k" mean nothing in a length, so measurements go
    through :func:`parse_stated_decimal` and refuse both.
    """
    text = raw.strip()
    shorthand = _THOUSANDS_SUFFIX.fullmatch(text)
    if shorthand is not None:
        return parse_stated_decimal(str(_plain_amount(shorthand["figure"]) * 1000))
    return _plain_amount(text)


def _plain_amount(text: str) -> Decimal:
    text = text.strip()
    if _DOT_GROUPED.fullmatch(text):
        raise ValueError("ambiguous figure")
    if _GROUPED.fullmatch(text):
        text = text.replace(",", "")
    return parse_stated_decimal(text)


def parse_stated_percent(raw: str) -> Decimal:
    """A percentage, with or without its sign: "20", "20%", "12.5 %"."""
    return parse_stated_decimal(raw.strip().removesuffix("%"))
