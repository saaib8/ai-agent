"""Whether a number in generated prose came from somewhere it was allowed to.

One narrow question, and deliberately only one: *did this figure come from an
approved non-product source?* It is not a fact checker, not a unit or currency
converter, not a semantic validator and not a second model. It compares
canonical numeric tokens, and nothing else.

The reason it can be this small is that the response model never receives a
product fact. Prices, dimensions and capacities are rendered by the application
from verified grounding, so a figure the model produced has no legitimate
catalog origin - the only numbers it may echo are ones the customer just said,
or counts the application supplied.

**Provenance, not arithmetic.** A customer who said "20% cheaper" licenses
"20%", not "80%" - even though one follows from the other. Deriving a figure is
commerce arithmetic, which belongs to services that read real prices
(CLAUDE.md 3.3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

ResponseNumericAllowance = frozenset[str]
"""Canonical numeric tokens generated prose may contain.

A plain immutable set rather than a schema: it carries no structure worth
validating, and it is application-only - nothing builds it into a prompt.
"""

_NUMERIC_TOKEN = re.compile(r"\d[\d,.]*")
"""A run beginning with a digit, allowing separators inside it.

Deliberately loose at the edges: trailing punctuation is trimmed afterwards, so
a sentence-final "3 options." yields 3 rather than being missed.
"""

_ORDINAL_SUFFIX = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)

_THOUSANDS = str.maketrans({",": ""})
"""The group separator. Only the ASCII comma: M11 V1 is English, and
admitting other separators would mean deciding which of them group and
which divide. Unicode *digits* still normalise: `Decimal` reads them."""


def canonical_number(raw: str) -> str | None:
    """One numeric literal as a canonical decimal string, or None.

    Equal values normalise equal: `3,000`, `3000.0` and `3000.00` all become
    `3000`, and Unicode decimal digits normalise to ASCII. A sign is dropped,
    because a hyphen in
    prose is far more often a range dash than a minus, and treating "3,000-4,000"
    as a single negative figure would hide a real number from the check.

    A percent sign, a currency code or a unit is not part of the number, so
    `20%`, `SAR 3,000` and `220cm` carry the same values as `20`, `3000`, `220`.
    """
    text = raw.strip().lstrip("+-")
    text = _ORDINAL_SUFFIX.sub("", text)
    text = text.translate(_THOUSANDS)
    text = text.strip(".")
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    value = value.normalize()
    if value == value.to_integral_value():
        value = value.to_integral_value()
    return format(value, "f")


def numbers_in(text: str) -> frozenset[str]:
    """Every canonical numeric value the text contains.

    Ordinal suffixes are stripped first so "the 2nd option" yields 2, matching
    how the allowance counts positions.
    """
    without_ordinals = _ORDINAL_SUFFIX.sub("", text)
    found = {
        canonical
        for token in _NUMERIC_TOKEN.findall(without_ordinals)
        if (canonical := canonical_number(token)) is not None
    }
    return frozenset(found)


def build_allowance(
    message: str, *, presented_count: int = 0, compared_count: int = 0
) -> ResponseNumericAllowance:
    """What this turn's prose is permitted to say in figures.

    Three sources, and no others:

    * numbers in the **current** customer message, read lexically. Not from
      `ResolvedSearch`, because most branches never run query understanding,
      and not from history, because a figure mentioned three turns ago is not
      what they are asking about now. The guard needs provenance, not search
      semantics.
    * the counts the application itself established.
    * ordinal positions within whatever is on screen, so "the second option"
      is sayable and "the fourth" of three is not.

    No product price, dimension, capacity, comparison cell, relaxation
    threshold or id is ever admitted here.
    """
    allowed = set(numbers_in(message))
    for count in (presented_count, compared_count):
        if count > 0:
            allowed.add(canonical_number(str(count)) or str(count))
            allowed.update(str(position) for position in range(1, count + 1))
    return frozenset(allowed)


@dataclass(frozen=True, slots=True)
class NumericPolicyViolation:
    """A figure in generated prose with no approved source."""

    field: str
    value: str


def check_numeric_policy(
    *, message: str, follow_up_question: str | None, allowance: ResponseNumericAllowance
) -> NumericPolicyViolation | None:
    """The first unsupported figure, or None.

    Scans exactly the two model-generated fields. A pass-through clarification
    is not scanned: no response call produced it, and the decision model that
    wrote it was validated in its own phase.
    """
    fields: list[tuple[str, str]] = [("message", message)]
    if follow_up_question is not None:
        fields.append(("follow_up_question", follow_up_question))

    for name, text in fields:
        for value in sorted(numbers_in(text)):
            if value not in allowance:
                return NumericPolicyViolation(field=name, value=value)
    return None
