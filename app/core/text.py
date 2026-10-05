"""Small spelling-only text helpers (CLAUDE.md 14.2)."""

from __future__ import annotations


def same_words(text: str) -> str:
    """The text with its spacing normalised, for "is this the same message?".

    One definition, because the decision log's restatement rate and the reuse
    of a speculative reading must agree on what "the same" means."""
    return " ".join(text.split())
