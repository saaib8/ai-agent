"""Screen-driven, deterministic follow-ups on a product search.

**Transport, and application-only.** Like `bundle_action`, these are things the
customer did on the screen — tapped "show me different options", or "not this
one" — not language a model interpreted. They carry no product id: an exclusion
names a product only by its position on screen, and the server resolves that to
a real id against verified state (CLAUDE.md 6, 20.2).

They exist because retrieval on its own is a pure function of the filters: run
it twice, get the same shelf. These give it a **memory** — re-run the current
search while excluding what the customer has already seen (or turned down) — so
"show me alternatives" returns genuinely different products. No model is
consulted, so it can never be mis-routed.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.product_brief import MAX_BRIEF_COLOURS, MAX_BRIEF_STYLES


class MoreOptionsAction(BaseModel):
    """Re-run the current search, excluding everything already shown.

    The whole presented set becomes an exclusion for this run, so the customer
    sees a different page of products for the same request. Repeats accumulate:
    each round excludes what the last one showed, so "keep going" keeps moving.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["more_options"] = "more_options"


class ExcludeProductAction(BaseModel):
    """Drop one product the customer turned down, then re-run the search.

    `ordinal` is the product's position on screen. It is resolved to a real id
    server-side and added to the search's exclusions, so it does not come back —
    not this turn, and not on a later "show me more" either.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["exclude"] = "exclude"
    ordinal: int = Field(ge=1)


class BriefAnswerAction(BaseModel):
    """The answers tapped on a card of questions, and a search to run on them.

    Keys only, each one of the choices the card offered: they are read back
    through the card the session remembers, so a tapped answer can name a
    choice but never invent one - a type, a price or a colour the card did not
    show is refused rather than searched (CLAUDE.md 10.4, 20.2). Anything left
    blank is simply not asked about; a card sent with nothing ticked searches
    for what they first said.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["brief"] = "brief"
    card: int = Field(ge=1)
    """The card answered, as it was numbered when drawn."""

    piece: str | None = Field(default=None, min_length=1, max_length=64)
    """The kind of piece: a sofa's seat count, an L-shape, a set."""

    room: str | None = Field(default=None, min_length=1, max_length=64)
    """Which room it is for."""
    people: str | None = Field(default=None, min_length=1, max_length=64)
    """How many usually sit there."""

    budget: str | None = Field(default=None, min_length=1, max_length=64)
    space: str | None = Field(default=None, min_length=1, max_length=64)
    """A chosen width ceiling: how wide a space the piece must fit."""
    colours: tuple[str, ...] = Field(default=(), max_length=MAX_BRIEF_COLOURS)
    styles: tuple[str, ...] = Field(default=(), max_length=MAX_BRIEF_STYLES)
    feel: str | None = Field(default=None, min_length=1, max_length=64)


class DropFacetAction(BaseModel):
    """✕ on one thing the search on screen is using - their budget, a colour,
    the seats - and the search run again without it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["drop"] = "drop"
    facet: str = Field(min_length=1, max_length=80)


class CombinationAction(BaseModel):
    """A tap on the seating combinations on screen: choose one, turn one
    down, or see more - exactly what "I'll take the second option", "not the
    second option" and "show me more" do typed (CLAUDE.md 17.1, 27.1).

    `position` is the combination's place on screen, resolved against the
    combinations the session remembers showing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["combination"] = "combination"
    op: Literal["choose", "dismiss", "more"]
    position: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _names_one_when_it_must(self) -> Self:
        if (self.op == "more") != (self.position is None):
            raise ValueError("choose and dismiss name a position; more names none")
        return self


class TasteAnswerAction(BaseModel):
    """A tapped answer to the taste question on screen - which of two feels
    more like them, a style, something to avoid (phase 5).

    A key the question offered, read back through the question the session
    remembers: a client can name an answer, never invent one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["taste"] = "taste"
    question: int = Field(ge=1)
    answer: str = Field(min_length=1, max_length=64)


SearchActionRequest = Annotated[
    MoreOptionsAction
    | ExcludeProductAction
    | BriefAnswerAction
    | DropFacetAction
    | CombinationAction
    | TasteAnswerAction,
    Field(discriminator="kind"),
]
"""A follow-up on the search, or the answers to its card, told apart by `kind`."""
