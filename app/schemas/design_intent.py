"""The rule for a design need's qualitative wording, in one place.

Two contracts carry the same phrase and must treat it identically: the
specialist's `DesignCategoryNeed.semantic_intent`, which is what the model
proposes, and `RoomDesignNeedState.semantic_intent`, which is the plan the
application went on to act on. If the two validated differently, a value could
be accepted by one and refused by the other, and the plan would stop being a
faithful record of the need.

A leaf module with no imports of its own, so state and design contracts can
both reach it without either importing the other.
"""

from __future__ import annotations

import re

MAX_DESIGN_INTENT_CHARS = 200
"""A phrase, not a transcript."""

_DIGITS = re.compile(r"\d")


def normalise_design_intent(value: str | None) -> str | None:
    """Trimmed wording, nothing, or a refusal.

    Blank means absent: a need with nothing particular to say is ordinary, and
    an empty string pretending otherwise would reach the query embedding.

    A figure is **refused rather than stripped**. Every prohibited structured
    fact - a price, a measurement, a seat count, a quantity, an identifier - is
    a number with a typed home elsewhere. Removing the digits would leave
    wording the design never asked for; keeping them would put an unprovenanced
    figure into text a later layer might quote.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if _DIGITS.search(trimmed):
        raise ValueError("a design intent carries no figures")
    return trimmed
