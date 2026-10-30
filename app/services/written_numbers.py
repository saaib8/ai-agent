"""Figures a shop's document writes in words or as "k" amounts.

A shop writes "returns within fourteen days", "free above one thousand two
hundred riyals" or "bank transfer for orders over 10k". The reply says those
figures in digits, as every reply does, so the numeric guard must know the
document stated them - otherwise the shop's own policy is refused as invented
and the shopper is told nothing (CLAUDE.md 21.1).

Read from the shop's passages only - the facts of the `store_policy` tool
(`app/agents/reply.py`) - never from a product name or a reply: the guard still
parses digits alone in what the model writes. Lexical and exact - English number
words and a digit run followed by "k" - so nothing is derived: no sum, no range,
no rounding. Arabic number words are not read yet.
"""

from __future__ import annotations

import re
from decimal import Decimal

_UNITS = {
    "zero": 0,
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
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_JOINERS = {"and", "a"}
_WORD = re.compile(r"[a-z]+")
_K_AMOUNT = re.compile(r"(?<![\w.,])(\d+(?:\.\d+)?)\s?k\b", re.IGNORECASE)


def written_figures(text: str) -> tuple[Decimal, ...]:
    """Every figure the text states in English words or as a "k" amount."""
    found = [Decimal(match.group(1)) * 1000 for match in _K_AMOUNT.finditer(text)]
    words = _WORD.findall(text.lower().replace("-", " "))
    return tuple(found + [Decimal(value) for value in _word_numbers(words)])


def _word_numbers(words: list[str]) -> list[int]:
    """Each run of number words as one value: "one thousand two hundred" is
    1200, "two to four" is 2 and 4. A joiner ("and", "a") only continues a run
    already started, so "a week" states no figure."""
    values: list[int] = []
    total = current = 0
    started = False
    for word in words:
        if word in _UNITS or word in _TENS:
            if started and not _continues(current, word):
                values.append(total + current)
                total = current = 0
            current += _UNITS.get(word, 0) + _TENS.get(word, 0)
            started = True
        elif word in _SCALES and started:
            scale = _SCALES[word]
            if scale == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * scale
                current = 0
        elif word in _JOINERS and started:
            continue
        else:
            if started:
                values.append(total + current)
            total = current = 0
            started = False
    if started:
        values.append(total + current)
    return values


def _continues(current: int, word: str) -> bool:
    """Whether a number word carries on the figure being read: after a whole
    hundred or thousand anything may follow ("two hundred fifty"), after a
    ten only a single digit ("twenty five"); otherwise it starts a new one."""
    last = current % 100
    if last == 0:
        return True
    return word in _UNITS and _UNITS[word] < 10 and last in _TENS.values()
