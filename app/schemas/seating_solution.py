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

    lines: tuple[SeatingBundleLine, ...] = Field(min_length=1)
    total_seats: int = Field(ge=1)
    total_price: Decimal = Field(ge=0)
    currency: str = Field(min_length=1)

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

    NO_SEATING = "no_seating"
    """The store stocks no seating whose capacity is known, so nothing can be
    composed here."""


class SeatingSolution(BaseModel):
    """The planner's answer to "seat this many within this budget"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_seats: int = Field(ge=1)
    budget_amount: Decimal | None = Field(default=None, ge=0)
    """The customer's ceiling, when they named one. ``None`` means no budget was
    given - the combination still composes, it simply has nothing to be within."""
    currency: str = Field(min_length=1)

    outcome: SeatingSolutionOutcome
    bundles: tuple[SeatingBundle, ...] = ()

    @model_validator(mode="after")
    def _bundles_match_the_outcome_and_the_request(self) -> Self:
        if (self.outcome is SeatingSolutionOutcome.BUNDLES) != bool(self.bundles):
            raise ValueError("bundles are present exactly when the outcome is BUNDLES")
        for bundle in self.bundles:
            if bundle.total_seats < self.target_seats:
                raise ValueError("a proposed bundle seats fewer than the target")
            if self.budget_amount is not None and bundle.total_price > self.budget_amount:
                raise ValueError("a proposed bundle exceeds the budget")
            if bundle.currency != self.currency:
                raise ValueError("a proposed bundle is priced in another currency")
        return self
