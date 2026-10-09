"""The card of questions asked before a search, and what it remembers.

"I need a sofa" is answered with one card - the kind, the budget, colours, the
feel, the style - rather than the first few sofas the catalog returns
(CLAUDE.md 10.4). The card is built by the application from reviewed data and
the live catalog; a model never writes its questions or its choices.

Two halves, kept apart on purpose:

* :class:`ProductBrief` is what the client draws - labels and opaque keys.
* :class:`PendingBrief` is what the session remembers - what each key *means*:
  a subcategory and a seat count, a price band, the words a feel ranks by.

A tapped answer sends keys only, and they are read back through the pending
card, so a client can name a choice but never invent one (CLAUDE.md 20.2).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.query import ResolvedSearch
from app.taxonomy.briefs import BriefQuestionKind

MAX_BRIEF_COLOURS: Final[int] = 3
"""Colours they may tick together. Alternatives, so a few is plenty."""

MAX_BRIEF_STYLES: Final[int] = 2
"""Styles they may tick together. Past two a look stops being one."""

MAX_BRIEFS_SHOWN: Final[int] = 20
"""A bound on the session's record of cards shown - one per product family,
and the families are few."""

MAX_OPENING_ASKED: Final[int] = 80
"""A bound on the session's record of opening questions asked - a few per
product family."""


class BriefMode(StrEnum):
    """Why the card is on screen."""

    ASK = "ask"
    """They stated a need. The card comes first; nothing is searched yet."""

    NARROW = "narrow"
    """They asked to see things and have been shown them. The card sits
    beside the results, folded, for narrowing them down."""


# ── what the client draws ───────────────────────────────────────────────────


class BriefChoice(BaseModel):
    """One chip: a label, and the opaque key tapping it sends back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=40)


class BriefQuestionView(BaseModel):
    """One question on the card, with only choices that lead to products."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: BriefQuestionKind
    label: str = Field(min_length=1, max_length=40)
    choices: tuple[BriefChoice, ...] = Field(min_length=2)
    max_choices: int = Field(default=1, ge=1)
    """One for a single answer; more where several may be ticked together."""
    selected: tuple[str, ...] = ()
    """The keys already true of the search on screen, so Narrow down opens
    showing what it is using rather than blank."""

    @model_validator(mode="after")
    def _keys_unique(self) -> Self:
        keys = [choice.key for choice in self.choices]
        if len(keys) != len(set(keys)):
            raise ValueError("each choice on a question needs its own key")
        return self


class ProductBrief(BaseModel):
    """The card, as a client draws it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    card: int = Field(ge=1)
    """Which card this is in the session. An answer names it, so a stale card
    cannot answer a newer one."""

    mode: BriefMode
    noun: str = Field(min_length=1, max_length=40)
    """What is being looked for, in customer words ("sofas")."""

    questions: tuple[BriefQuestionView, ...] = Field(min_length=1)
    submit_label: str = Field(min_length=1, max_length=40)
    skip_label: str = Field(min_length=1, max_length=48)
    """The same button with nothing tapped, in the reply's language."""


class BriefChip(BaseModel):
    """One thing the search on screen is using, which they can take away."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    facet: str = Field(min_length=1, max_length=80)
    """What tapping ✕ removes - sent back as a `drop` action."""
    label: str = Field(min_length=1, max_length=60)


# ── what the session remembers ──────────────────────────────────────────────


class BriefKindOption(BaseModel):
    """What a kind key means: a type, and a seat count where it names one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str = Field(min_length=1)
    seats: int | None = Field(default=None, ge=1)
    min_seats: int | None = Field(default=None, ge=1)


class BriefBudgetOption(BaseModel):
    """What a budget key means: a price band in the store's currency."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    currency: str = Field(min_length=1)
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _a_band(self) -> Self:
        if self.min_amount is None and self.max_amount is None:
            raise ValueError("a budget band needs at least one bound")
        if (
            self.min_amount is not None
            and self.max_amount is not None
            and self.min_amount >= self.max_amount
        ):
            raise ValueError("a budget band's floor must be below its ceiling")
        return self


class BriefSpaceOption(BaseModel):
    """What a space key means: the along-wall width a piece may be, in cm.

    A ceiling the customer taps ("my spot is up to 220 cm wide"), so a chosen
    band becomes a max-width filter. `max_cm` is None for the "any width"
    escape, which filters nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    max_cm: Decimal | None = Field(default=None, gt=0)


class BriefRoomOption(BaseModel):
    """What a room key means: the room it remembers, or nothing for "Other"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    room: str | None = Field(default=None, min_length=1, max_length=40)


class BriefPeopleOption(BaseModel):
    """What a head-count key means: how many usually sit there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    people: int = Field(ge=1)
    or_more: bool = False
    combine: bool = False
    """No single piece in the store seats this many, so the answer is a seat
    requirement that leads to a combination (CLAUDE.md 27.1), not an order."""


class BriefFeelOption(BaseModel):
    """What a feel key means: the words it ranks by."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    words: str = Field(min_length=1, max_length=60)


class PendingBrief(BaseModel):
    """A card on screen, waiting for its answers.

    `base` is the search as they stated it - everything the card does not ask
    about is kept exactly as they said it. The options record what each key on
    the card means, fixed when the card was drawn: a budget band stays the band
    they saw even if prices move before they answer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    card: int = Field(ge=1)
    name: str = Field(min_length=1)
    base: ResolvedSearch
    kinds: tuple[BriefKindOption, ...] = ()
    budgets: tuple[BriefBudgetOption, ...] = ()
    spaces: tuple[BriefSpaceOption, ...] = ()
    colours: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    feels: tuple[BriefFeelOption, ...] = ()
    rooms: tuple[BriefRoomOption, ...] = Field(default=(), exclude_if=lambda v: not v)
    people: tuple[BriefPeopleOption, ...] = Field(default=(), exclude_if=lambda v: not v)
    opening: bool = Field(default=False, exclude_if=lambda v: not v)
    """The card opens a new search: the reply writer chose which of its
    questions to show, and only those were asked."""
    replaces: tuple[BriefQuestionKind, ...] = Field(default=(), exclude_if=lambda v: not v)
    """The questions Narrow down opened with a value ticked: the answers it
    sends replace those values, so a chip they untick is taken away."""
    narrowing: bool = Field(default=False, exclude_if=lambda v: not v)
    """Narrow down beside results: answered by tapping, never by typing."""
    drop_saved_sizes: bool = False
    """They let go of the sizes saved for this kind in the message the card
    answers - "back to sofas, any size is fine". Kept with the card, so the
    search its answers run does not bring those sizes back."""


class ProductBriefState(BaseModel):
    """The cards of this session.

    A search for a kind of product gets its card every time; `shown` is what
    keeps the folded card beside results to once per product family
    (CLAUDE.md 10.4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    shown: tuple[str, ...] = Field(default=(), max_length=MAX_BRIEFS_SHOWN)
    """The families whose card has been shown, by name. Read only for the
    folded card beside results, which is offered once per family."""

    cards: int = Field(default=0, ge=0)
    """How many cards have been drawn - the next card's number less one."""

    pending: PendingBrief | None = None
    """The card on screen, until it is answered or another replaces it."""

    asked: tuple[str, ...] = Field(
        default=(), max_length=MAX_OPENING_ASKED, exclude_if=lambda v: not v
    )
    """Opening questions already asked, as "family:kind" - never asked twice,
    whether or not they were answered."""

    @model_validator(mode="after")
    def _pending_was_counted(self) -> Self:
        if self.pending is not None and self.pending.card > self.cards:
            raise ValueError("a pending card must be one that was drawn")
        return self
