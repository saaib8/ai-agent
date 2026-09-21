"""The whole-room package as the customer sees it.

**Application-only, and the other half of a room turn.** The response model
frames - "here's a package covering what the room needs" - and everything with
a value in it is rendered here, from the same verified products the optimiser
chose. Nothing in this module ever enters `ResponseInput`.

That split is why the numeric guard can stay as strict as it is: the model is
never given a price, so a figure in its prose has no legitimate source, and the
figures the customer reads never passed through it (CLAUDE.md 20.4).

One rule is worth stating because getting it wrong would be a lie rather than a
bug: a piece the customer already owns has **no** new-spend line total. Not
zero - zero is a price, and this product does not cost nothing.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.acquisition import BundleAcquisition
from app.schemas.bundle import BundleStatus, TotalUnavailableReason
from app.schemas.product import CommerceClassification


class GroundedBundleItem(BaseModel):
    """One rendered piece of the room.

    An explicit allowlist, like every other customer-facing shape: adding a
    column to `core_product` can never surface here. No product id, no line id,
    no store, no rank, no similarity, no need index - `grounding_ref` is the
    only handle, and it means a position in this turn's rendering rather than
    anything the catalog would recognise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    grounding_ref: int = Field(ge=1)

    name_english: str
    image_url: str
    product_url: str
    commerce: CommerceClassification

    quantity: int = Field(ge=1)
    acquisition: BundleAcquisition
    locked: bool

    unit_price: Decimal
    price_unit: str
    """What one costs today, as the catalog has it. Present whatever the
    acquisition, because "you already own one of these, they're SAR 1,200"
    is a true and useful thing to show."""

    new_spend_line_total: Decimal | None = None
    """What this line adds to the bill, or None when it adds nothing.

    None rather than zero for an already-owned piece. Zero would read as a
    price, and the product does not cost nothing - the customer simply is not
    buying it again.
    """

    @model_validator(mode="after")
    def _only_a_purchase_has_a_line_total(self) -> Self:
        if self.acquisition is BundleAcquisition.TO_BUY:
            if self.new_spend_line_total != self.unit_price * self.quantity:
                raise ValueError("a purchased line totals its unit price by quantity")
        elif self.new_spend_line_total is not None:
            raise ValueError("an already-owned piece adds nothing to new spend")
        return self


class GroundedBundleTotals(BaseModel):
    """What the room comes to, and how that sits against the budget.

    Every figure is copied from the outcome the optimiser produced. Nothing is
    recomputed here: a second summation could disagree with the first, and the
    one that decided the package is the one that is true.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    new_spend_total: Decimal | None = None
    currency: str | None = None
    total_unavailable: TotalUnavailableReason | None = None

    budget_max_amount: Decimal | None = None
    budget_currency: str | None = None
    budget_max_exclusive: bool = False
    """The customer's ceiling, in their own terms. A minimum is never rendered
    as a maximum: M12D supports no budget or a maximum, and nothing else
    reaches here."""

    within_budget: bool | None = None

    @model_validator(mode="after")
    def _figures_travel_with_their_units(self) -> Self:
        if (self.new_spend_total is not None) != (self.currency is not None):
            raise ValueError("a total and its currency are present together")
        if (self.budget_max_amount is not None) != (self.budget_currency is not None):
            raise ValueError("a budget and its currency are present together")
        if self.budget_max_amount is None and self.within_budget is not None:
            raise ValueError("no budget was given, so nothing can be within it")
        return self


class GroundedBundlePresentation(BaseModel):
    """The rendered room: its pieces, its arithmetic, and its status.

    A sibling of the assistant's message rather than part of it. A later
    transport layer pairs the two; neither is embedded in the other, which is
    what keeps model-authored prose and application-verified facts separable
    all the way out.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: BundleStatus
    items: tuple[GroundedBundleItem, ...] = ()
    totals: GroundedBundleTotals

    @model_validator(mode="after")
    def _refs_are_this_turns_positions(self) -> Self:
        """1..N with no holes: a ref means a place in this rendering."""
        expected = tuple(range(1, len(self.items) + 1))
        if tuple(item.grounding_ref for item in self.items) != expected:
            raise ValueError("bundle grounding refs run 1..N in rendered order")
        return self
