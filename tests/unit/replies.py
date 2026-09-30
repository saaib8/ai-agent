"""Helpers for tests that check a reply's exact words.

Every reply now ends on a question: when a reply has none, the application adds
its next step's question (CLAUDE.md 10.2). A test about a branch's own wording
compares the words before that closing question; the closing question has its
own tests (tests/unit/test_next_step.py).
"""

from __future__ import annotations

from app.schemas.next_step import ANY_NEXT_STEP, QUESTIONS

_CLOSERS = (*QUESTIONS.values(), ANY_NEXT_STEP)


def worded(message: str) -> str:
    """The reply's own words, without the next-step question added after them."""
    for closer in _CLOSERS:
        if message.endswith(" " + closer):
            return message[: -len(closer) - 1]
    return message
