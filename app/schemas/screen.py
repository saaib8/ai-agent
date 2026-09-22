"""What the customer can actually see, as the reasoning models may read it.

A salesperson standing beside five sofas knows which sofas the customer is
looking at. Until this existed the models knew only that five products were
displayed, which is why every search came back as "here's what I found" - true,
and no use to anyone.

**This is not the frontend.** No screenshot, no DOM, no component tree, no
client state. It is a typed projection of the *same verified data* the
application used to build this turn's presentation payload, so a fact stated
here and a fact drawn on the card are the same fact by construction
(CLAUDE.md 3).

Three rules hold everywhere in this module.

**No backend identity.** No `product_id`, `uuid`, `store_id`, `line_id`,
`need_id`, revision or score. The authority design rests on a model never
emitting an identifier, and the surest way to keep that true is to never show
it one. Position is the only handle, and `PresentedOrdinal` is how a model
points at something (CLAUDE.md 6, 7).

**No internal vocabulary.** Categories arrive as customer words, never as
registry keys, because a model shown `lounge-chair` writes `lounge-chair`
(CLAUDE.md 50).

**Merchant text is data.** Names and style tokens are merchant-controlled and
arbitrary. They travel inside the serialised user turn, never inside
instructions, and nothing here interprets them (CLAUDE.md 13, 20.1).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.acquisition import BundleAcquisition
from app.schemas.bundle import BundleStatus
from app.schemas.comparison import ComparisonField, ComparisonStatus


class ScreenCardDimensions(BaseModel):
    """The measurements printed on a card, in centimetres.

    Column names, not physical roles: `length_cm` is whatever the catalog
    stores in `length`, which is a sofa's along-wall span and a table's longer
    side. Turning a column into a customer-facing role is
    `dimension_semantics_v1`'s job and depends on the subcategory, so the
    projection does not guess at one (CLAUDE.md 15.1).

    Present only when the catalog normalised them. An unusable unit yields no
    dimensions at all rather than a number in an unknown scale.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    length_cm: Decimal | None = None
    width_cm: Decimal | None = None
    height_cm: Decimal | None = None

    @model_validator(mode="after")
    def _at_least_one_measurement(self) -> Self:
        if self.length_cm is None and self.width_cm is None and self.height_cm is None:
            raise ValueError("a dimensions block with nothing in it should be absent")
        return self


class PresentedCardView(BaseModel):
    """One product card, as the customer sees it.

    An explicit allowlist rather than a filtered `GroundedProduct`: adding a
    field to grounding can never leak it here, and every field present is one
    a customer is already looking at (CLAUDE.md 20.4).

    `image_url` and `product_url` are deliberately absent. They are on the card
    and a model has no conversational use for either - it cannot describe a
    picture it was not shown, and a link it repeated in prose would duplicate
    the button beside it.

    `relaxation_depth` is absent for a different reason: it is search
    provenance rather than something on screen, and it already reaches the
    response layer as a count.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    presented_ordinal: int = Field(ge=1)
    """Which card this is, counting from 1.

    The one handle a model gets, and the same number a `PresentedOrdinal`
    reference resolves against. Positions are never renumbered to close a gap
    left by a product the catalog stopped returning: card 3 is the third thing
    the customer was shown, whatever happened to card 2 (CLAUDE.md 7).
    """

    name: str = Field(min_length=1)
    """Merchant-controlled text. Data, never instruction (CLAUDE.md 13)."""

    commerce_category: str | None = Field(default=None, min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)
    """In customer words, never registry keys (CLAUDE.md 50).

    Optional because the catalog's is. A product whose commerce fields were
    never reviewed has no category, and the card says so rather than borrowing
    the visual classification or guessing from the name - the agent reports
    what the catalog contains (CLAUDE.md 6.1).
    """

    price_amount: Decimal | None = None
    price_unit: str | None = None

    seating_capacity: int | None = Field(default=None, ge=1)
    """Reviewed capacity, or None when nobody confirmed one.

    None means *unverified*, never *any*. A single-seater sofa with a NULL
    capacity stays None here, and a reply may not call it a one-seater
    (CLAUDE.md 6.2, 31).
    """

    dimensions: ScreenCardDimensions | None = None
    main_color: str | None = None
    styles: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _a_price_carries_its_unit(self) -> Self:
        if (self.price_amount is None) != (self.price_unit is None):
            raise ValueError("a price is an amount and a unit, or neither")
        return self


class ScreenComparisonCellView(BaseModel):
    """One cell of the visible comparison table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    known: bool
    value: str | None = None


class ScreenComparisonRowView(BaseModel):
    """One field, across the compared products.

    `cells` is positional: cell *i* belongs to compared product *i*, in the
    same order as :attr:`ScreenComparisonView.ordinals`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: ComparisonField
    status: ComparisonStatus
    cells: tuple[ScreenComparisonCellView, ...]


class ScreenComparisonView(BaseModel):
    """The comparison table currently on screen.

    Carried so "which one is cheaper?" is answerable from what the customer is
    looking at, rather than answered with "the table beside this message says".
    The application computed every cell, so restating one is reporting, not
    claiming (CLAUDE.md 12).

    It states which values differ. It never states which product is better:
    that judgement is the customer's, and nothing here establishes it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ordinals: tuple[int, ...] = ()
    """The card positions being compared, in table column order."""

    rows: tuple[ScreenComparisonRowView, ...] = ()

    @model_validator(mode="after")
    def _every_row_covers_every_column(self) -> Self:
        for row in self.rows:
            if len(row.cells) != len(self.ordinals):
                raise ValueError("a comparison row has one cell per compared product")
        if len(set(self.ordinals)) != len(self.ordinals):
            raise ValueError("a product is compared with itself")
        return self


class ScreenRoomCardView(BaseModel):
    """One piece of the room package, as the customer sees it listed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    presented_ordinal: int = Field(ge=1)
    name: str = Field(min_length=1)
    commerce_category: str | None = Field(default=None, min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)

    quantity: int = Field(ge=1)
    acquisition: BundleAcquisition
    locked: bool
    """Customer-visible states. Both are things they told us, so reading them
    back is how the agent avoids offering to sell someone a piece they said
    they already own."""

    unit_price: Decimal | None = None
    price_unit: str | None = None
    new_spend_line_total: Decimal | None = None
    """What this line adds to the spend, when it adds anything.

    None for a piece they already own - and that absence is the point, because
    a line total on an owned piece would say they are buying it again.
    """


class ScreenRoomView(BaseModel):
    """The room package currently on screen.

    Every figure was computed by the application. The model may repeat them and
    may not add them up: a second summation could disagree with the first, and
    the first is the one the customer can see (CLAUDE.md 14).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: BundleStatus
    cards: tuple[ScreenRoomCardView, ...] = ()

    new_spend_total: Decimal | None = None
    currency: str | None = None
    budget_max_amount: Decimal | None = None
    within_budget: bool | None = None


class CustomerVisibleScreenView(BaseModel):
    """Everything the customer is currently looking at.

    One shape for three presentations, because a turn may show products, a
    comparison or a room, and a reader should not have to know which to ask.
    Empty means nothing is on screen, which is an ordinary state: a design
    question produces words and no cards.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    products: tuple[PresentedCardView, ...] = ()
    comparison: ScreenComparisonView | None = None
    room: ScreenRoomView | None = None

    @model_validator(mode="after")
    def _ordinals_are_distinct_positions(self) -> Self:
        positions = [card.presented_ordinal for card in self.products]
        if len(set(positions)) != len(positions):
            raise ValueError("two cards cannot occupy the same position")
        if positions != sorted(positions):
            # Order is the order the customer sees. Sorting or shuffling here
            # would break the one invariant every ordinal reference rests on.
            raise ValueError("cards must be in the order they are presented")
        return self

    def is_empty(self) -> bool:
        return not self.products and self.comparison is None and self.room is None
