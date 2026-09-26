"""A seating requirement met by a combination, when no single piece can.

When a customer needs more seats than any one product in the store provides -
"a sofa for eight" where the largest sofa seats five - a good salesperson does
not say "we don't have that". They compose: a large sofa and a few chairs that
together seat eight, within the budget. This is that answer, made structural.

Two invariants make it honest, and both are enforced here rather than trusted
from anywhere:

* **the seats add up** - ``total_seats`` is the sum of the lines, computed by
  code, and it meets or exceeds what was asked. The requirement is *met*, never
  quietly relaxed (CLAUDE.md 13.4).
* **the budget holds** - ``total_price`` is the sum of real product prices and
  never exceeds the budget. The one thing recovery may not break.

Every line is a real catalogue product. A seat count is marked confirmed or not,
because a chair's single seat is reviewed domain data rather than a figure the
catalogue recorded, and a reply must not present the second as the first
(CLAUDE.md 6.2).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.discovery import DimensionConstraint
from app.taxonomy.attributes import AttributeFamily


class SeatingShape(StrEnum):
    """How a combination reaches the seat count - the choice worth asking about.

    Named by arrangement, not by product: which pieces fill it is decided by
    the catalog and the customer's taste, never by the shape."""

    SEPARATE_SOFAS = "separate_sofas"
    """Two or three multi-seat pieces - sofas, sets, sectionals - arranged
    together, with no single chairs."""

    SOFA_WITH_EXTRA_SEATS = "sofa_with_extra_seats"
    """One or two multi-seat pieces, with armchairs or sofa chairs making up
    the rest."""


class SeatingShapeOption(BaseModel):
    """One shape the store can actually build for this request, and from what
    price - so a question offers only real choices, each with a real figure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shape: SeatingShape
    from_price: Decimal = Field(ge=0)
    combination_count: int = Field(ge=1)


class SeatingAnswer(StrEnum):
    """What the customer said to the shape question, as the decision reads it."""

    SEPARATE_SOFAS = "separate_sofas"
    SOFA_WITH_EXTRA_SEATS = "sofa_with_extra_seats"
    ANY = "any"
    """No preference, "just show me" - the best of every shape."""

    @property
    def shape(self) -> SeatingShape | None:
        return None if self is SeatingAnswer.ANY else SeatingShape(self.value)


class SeatingRequirements(BaseModel):
    """What the customer asked of the seating beyond the seat count and budget.

    Every piece of a combination is held to these, exactly as a single-product
    search would be (CLAUDE.md 12.4, 13): a combination is a way to reach the
    seat count, never a way around the rest of the request.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    colors_any_of: tuple[str, ...] = ()
    """Strict colours - every piece must be one of them."""
    styles_all_of: tuple[str, ...] = ()
    """Strict styles - every piece must carry all of them."""
    unmatchable_strict: tuple[AttributeFamily, ...] = ()
    """Families with a strict value no approved value expresses ("only red"):
    no piece can meet them, so they are lifted at once and disclosed."""

    wished_colors: tuple[str, ...] = ()
    wished_styles: tuple[str, ...] = ()
    """Preferences: they order the candidates for a piece, never filter them."""

    asked_type: str | None = None
    """The seating type the customer searched for. A multi-seat type that is
    not a usual main piece - a sofa bed - is used only when it is this."""

    sized_type: str | None = None
    """The product type the customer gave their sizes for. A size belongs to
    that type (CLAUDE.md 13.5), so only a piece of it is measured."""
    dimensions: tuple[DimensionConstraint, ...] = ()

    @property
    def strict_families(self) -> tuple[AttributeFamily, ...]:
        families = [
            family
            for family, values in (
                (AttributeFamily.COLOR, self.colors_any_of),
                (AttributeFamily.STYLE, self.styles_all_of),
            )
            if values
        ]
        return tuple(dict.fromkeys((*families, *self.unmatchable_strict)))


class SeatingBundleLine(BaseModel):
    """One piece of a seating combination: a real product, and how many of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: int = Field(gt=0)
    """Internal, for the presentation layer to render from - never shown to a
    model, which points at pieces by position like everywhere else (CLAUDE.md 6)."""

    name: str = Field(min_length=1)
    commerce_subcategory: str = Field(min_length=1)

    unit_price: Decimal = Field(ge=0)
    quantity: int = Field(ge=1)

    seats_each: int = Field(ge=1)
    seats_are_confirmed: bool
    """Whether the seat count came from the catalogue (a sofa's recorded
    capacity) or from reviewed domain data (a chair seats one). Carried so a
    reply can say "each seats one" for the reviewed case without claiming the
    catalogue confirmed it (CLAUDE.md 6.2, 31)."""

    image_url: str
    product_url: str

    matches_wish: bool = False
    """Whether this piece's stored colour or style is one the customer wished
    for. A fact from the row, for the reply to count - never a description."""

    @property
    def line_seats(self) -> int:
        return self.seats_each * self.quantity

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * self.quantity


class SeatingBundle(BaseModel):
    """A validated combination: real pieces that together meet the seat target
    within budget, with every total computed by code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shape: SeatingShape
    lines: tuple[SeatingBundleLine, ...] = Field(min_length=1)
    total_seats: int = Field(ge=1)
    total_price: Decimal = Field(ge=0)
    currency: str = Field(min_length=1)

    lifted: tuple[AttributeFamily, ...] = ()
    """Strict colour or style families no combination could meet, set aside as
    the last resort (CLAUDE.md 12.4). The reply must say so; empty means every
    piece meets every strict requirement."""

    sizes_applied: bool | None = None
    """Whether the customer's sizes limited this combination: ``None`` when
    they gave none, ``False`` when no piece is of the type they sized."""

    @model_validator(mode="after")
    def _totals_are_the_sum_of_the_lines(self) -> Self:
        if self.total_seats != sum(line.line_seats for line in self.lines):
            raise ValueError("total_seats must be the sum of the lines")
        if self.total_price != sum((line.line_total for line in self.lines), Decimal(0)):
            raise ValueError("total_price must be the sum of the lines")
        return self


class SeatingSolutionOutcome(StrEnum):
    """What the planner found, so a caller handles each case without inspecting
    the bundle list to infer it."""

    BUNDLES = "bundles"
    """At least one combination meets the target within budget."""

    SINGLE_PIECE_SUFFICES = "single_piece_suffices"
    """A single product can already seat this many - not a combination case, and
    ordinary search should answer it."""

    NONE_WITHIN_BUDGET = "none_within_budget"
    """Combinations exist, but none fits the budget. An honest outcome, not an
    error: the reply says the closest it could do rather than a dead end."""

    CHOOSE_SHAPE = "choose_shape"
    """Combinations exist, and the customer is asked which shape they would
    like first - only shapes that really exist, each with its lowest total.
    Nothing is shown yet; `options` carries the choice."""

    NO_MORE = "no_more"
    """Every combination they asked to see more of has been shown or turned
    down. Nothing new is shown; `options` carries the shapes that still have
    unseen combinations, if any, so the reply can offer them honestly."""

    NO_SEATING = "no_seating"
    """The store stocks no seating whose capacity is known, so nothing can be
    composed here."""


class SeatingArrangementLine(BaseModel):
    """One product of a room's seating, and how many of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: int = Field(ge=1)
    commerce_subcategory: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    seats_each: int = Field(ge=1)


class SeatingArrangement(BaseModel):
    """One way to seat a room's head count: identity and arithmetic only.

    Candidates for a room, not something shown on its own - the room's pieces
    are read fresh and priced together by the bundle optimiser (CLAUDE.md 27).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: tuple[SeatingArrangementLine, ...] = Field(min_length=1)
    total_price: Decimal = Field(ge=0)
    lifted: tuple[AttributeFamily, ...] = ()

    @property
    def total_seats(self) -> int:
        return sum(line.seats_each * line.quantity for line in self.lines)


class SeatingSolution(BaseModel):
    """The planner's answer to "seat this many within this budget"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    closest_total: Decimal | None = Field(default=None, ge=0)
    """When nothing fits the budget: the lowest real total that seats them,
    the budget set aside and everything else kept - so the reply can offer it
    ("the closest is about 3,700 - shall I show it?")."""

    target_seats: int = Field(ge=1)
    budget_amount: Decimal | None = Field(default=None, ge=0)
    """The customer's ceiling, when they named one. ``None`` means no budget was
    given - the combination still composes, it simply has nothing to be within."""
    currency: str = Field(min_length=1)

    outcome: SeatingSolutionOutcome
    bundles: tuple[SeatingBundle, ...] = ()

    options: tuple[SeatingShapeOption, ...] = ()
    """Every shape the store can build for this request, cheapest first -
    including shapes not shown among `bundles`, for a question to offer."""

    lifted: tuple[AttributeFamily, ...] = ()
    """A strict colour or style no combination could meet, set aside as the
    last resort - true of every combination and option here, shown or asked."""

    wishes_given: bool = False
    """Whether they wished for a colour or style - so a count of pieces that
    match means something."""

    already_seen: int = Field(default=0, ge=0)
    """How many combinations were already shown or turned down and are left
    out here - above zero, everything in `bundles` is new to the customer."""

    requested_shape: SeatingShape | None = None
    """The shape these were built for, when one was chosen - so a reply can say
    which shape has run out."""

    ask_colour: bool = False
    """A shape question that also asks which colour they like, because
    nothing they said or have on record names one."""

    @model_validator(mode="after")
    def _bundles_match_the_outcome_and_the_request(self) -> Self:
        if (self.outcome is SeatingSolutionOutcome.BUNDLES) != bool(self.bundles):
            raise ValueError("bundles are present exactly when the outcome is BUNDLES")
        if self.outcome is SeatingSolutionOutcome.CHOOSE_SHAPE and not self.options:
            raise ValueError("a shape question offers at least one real shape")
        shapes = [option.shape for option in self.options]
        if len(shapes) != len(set(shapes)):
            raise ValueError("each shape is offered once")
        if shapes and any(bundle.shape not in shapes for bundle in self.bundles):
            raise ValueError("a shown bundle's shape must be among the options")
        for bundle in self.bundles:
            if bundle.total_seats < self.target_seats:
                raise ValueError("a proposed bundle seats fewer than the target")
            if self.budget_amount is not None and bundle.total_price > self.budget_amount:
                raise ValueError("a proposed bundle exceeds the budget")
            if bundle.currency != self.currency:
                raise ValueError("a proposed bundle is priced in another currency")
        return self
