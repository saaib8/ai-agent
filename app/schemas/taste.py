"""Taste learned after products are on screen (docs/designer-led-shopping-plan.md,
phase 5).

What the customer likes is caught quietly first - from ♡, picks and More like
this - and only what nothing has told us yet is asked, softly, one question a
reply, each once a session. An answer is a key read back through the question
the session remembers, exactly like the card's (CLAUDE.md 10.4): a client can
name an answer, never invent one.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

MAX_TASTE_OPTIONS = 8


class TasteQuestionKind(StrEnum):
    WHICH = "which"
    """"Which of these two feels more like you?" - two cards on screen that
    differ most in colour or style. The answer teaches taste; it is never a
    pick."""
    STYLE = "style"
    """"Which style feels right?" - the styles the store has for this kind."""
    AVOID = "avoid"
    """"Anything you'd rather avoid?" - pushed down, never hidden."""


class TasteOption(BaseModel):
    """One answer a taste question offers, and what it means."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    position: int | None = Field(default=None, ge=1)
    """For "which feels more like you": the card's place on screen."""
    colour: str | None = None
    styles: tuple[str, ...] = ()


class PendingTaste(BaseModel):
    """The taste question on screen, until it is answered or another replaces it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: int = Field(ge=1)
    """Numbered like a card, so an answer to an older question names nothing."""
    kind: TasteQuestionKind
    commerce_subcategory: str | None = None
    """The kind of piece it was asked about - a colour answer belongs to it."""
    list_revision: int | None = None
    """The list it was asked beside: once another list replaces it, the
    question has gone from the screen and nothing answers it."""
    options: tuple[TasteOption, ...] = Field(min_length=1, max_length=MAX_TASTE_OPTIONS)


class TasteState(BaseModel):
    """Which taste questions this session has asked, and the one on screen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    asked: tuple[TasteQuestionKind, ...] = ()
    pending: PendingTaste | None = None
    asked_count: int = Field(default=0, ge=0)
