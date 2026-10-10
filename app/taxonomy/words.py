"""Taxonomy keys as a customer would say them.

The registry's keys are internal identifiers - `lounge-chair`,
`single-seater-sofa`, `tv-table`. A model shown one writes it back verbatim,
which is how a customer who had asked about sofas came to be told there were no
matching "lounge-chair options" (CLAUDE.md 50).

The conversion is mechanical and total: hyphens become spaces, and nothing is
renamed. That is what keeps this from being a second vocabulary beside the
registry - there is no table to maintain, no value mapped onto another, and no
way for a display name to drift from the key it displays (CLAUDE.md 14.1, 14.2).
"""

from __future__ import annotations

_KEY_SEPARATOR = "-"


def customer_words(value: str) -> str:
    """One taxonomy key, spelled the way it is spoken."""
    return value.replace(_KEY_SEPARATOR, " ")


def customer_words_or_none(value: str | None) -> str | None:
    """The same, for an optional subcategory."""
    return None if value is None else customer_words(value)


def plural_words(words: str) -> str:
    """Customer words for several of a type: "sofas", "lounge chairs". Some
    keys are plural already ("mattresses", "tables") and stay as they are;
    "canvas" and "glass" take "es"."""
    if words.endswith("es") or (words.endswith("s") and words[-2:-1] not in set("aeiousy")):
        return words
    if words.endswith(("s", "x", "ch", "sh")):
        return f"{words}es"
    return f"{words}s"


_ACRONYMS = frozenset({"tv"})


def label_words(words: str) -> str:
    """Customer words as a chip label: the first letter capitalised, and an
    acronym in capitals - "TV table", not "Tv table"."""
    parts = [part.upper() if part in _ACRONYMS else part for part in words.split(" ")]
    text = " ".join(parts)
    return text[:1].upper() + text[1:]
