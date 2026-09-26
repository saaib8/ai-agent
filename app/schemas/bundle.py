"""Whole-room bundle contracts.

**Application-only.** Like `resolution.py` and `design_discovery.py`, these
carry verified product identity, which is exactly what the model-facing design
contracts must never hold.

Three ideas are kept apart here because collapsing any two of them would let
the optimiser assert something nobody established:

* **A lock is preservation, not purchase.** `locked` says the customer will not
  have the piece replaced. Whether its price is money still to spend is a
  different fact, carried by `BundleAcquisition` and never inferred.
* **A physical unit is not a product.** One product bought twice is two units
  and one line; two needs choosing the same product are two lines. Quantity,
  identity and line are three separate things.
* **A total is new spend.** A piece the customer already owns is in the room
  and not in the bill, so the monetary field is named for what it means.

Nothing here claims a bundle fits a room. M12A establishes only that an object
taller than the ceiling does not go in; there is no targeted horizontal
applicability contract, so no spatial assertion is representable (CLAUDE.md
15.1).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.acquisition import BundleAcquisition
from app.schemas.design import DesignPriority
from app.schemas.design_discovery import DesignDiscoveryResult
from app.schemas.discovery import PriceConstraint
from app.schemas.product import ProductCandidate


class LockedBundleProduct(BaseModel):
    """A product the customer will not have replaced, with its facts verified.

    `acquisition` is **required and has no default**, here or in the optimiser.
    A lock proves preservation and nothing about purchase, so a default would
    be this layer inventing a budget fact (CLAUDE.md 3.3). The caller states
    it.

    The product is assumed freshly hydrated and retailer-scoped. Verifying that
    every expected lock still resolves happens before the optimiser is called,
    because a service with no repository cannot tell a missing id from an id
    nobody passed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product: ProductCandidate
    acquisition: BundleAcquisition
    quantity: int = Field(default=1, ge=1)


class BundleOptimizationRequest(BaseModel):
    """Everything the optimiser reasons over, and nothing it could misuse.

    No `RetailerContext`: scope was applied when every candidate and every lock
    was fetched, and carrying it here would suggest this layer could change it.
    No state, no messages, no prompts, no similarity.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    discovery: DesignDiscoveryResult
    budget: PriceConstraint | None = None
    locked: tuple[LockedBundleProduct, ...] = ()


class UnmetReason(StrEnum):
    """Why a need did not get its full quantity. Reasons, never prose."""

    NO_CANDIDATES = "no_candidates"
    """The search ran and the catalog held nothing, or the type is not stocked."""

    NO_USABLE_PRICE = "no_usable_price"
    """Candidates exist and none carries a price commerce can act on."""

    NOT_BUDGET_COMPARABLE = "not_budget_comparable"
    """Candidates exist and none is priced in the budget's currency. Distinct
    from affordability: nothing was too expensive, the figures simply cannot be
    compared without inventing a rate."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    """Affordable candidates exist, but not alongside higher-priority needs."""


class UnmetNeed(BaseModel):
    """One need that did not get what the design asked for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    need_index: int = Field(ge=0)
    priority: DesignPriority
    shortfall: int = Field(ge=1)
    """Units still missing. At least one, or the need is not unmet."""

    reason: UnmetReason

    commerce_category: str | None = None
    commerce_subcategory: str | None = None
    """Which piece is missing, from the verified plan - so the reply can name
    it ("the rug") instead of counting it ("1 needed piece")."""

    cheapest_price: Decimal | None = Field(default=None, ge=0)
    """The lowest total that would fill it, when the budget was what stopped
    it - a real catalog price times the quantity, so the reply can offer a
    concrete next step. None for any other reason."""


class BundleLine(BaseModel):
    """One product in the room, and how many of it.

    Monetary values are **derived**, never stored: `unit_price` and
    `line_total` read through to the hydrated product, so there is no second
    copy of a price to disagree with the catalog.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    need_index: int | None = Field(default=None, ge=0)
    """The design need this satisfies, or None for a lock matching none."""

    product: ProductCandidate
    quantity: int = Field(ge=1)
    locked: bool
    acquisition: BundleAcquisition

    relaxation_depth: int | None = Field(default=None, ge=0)
    """Search provenance for a newly selected product; None for a lock.

    **Never zero for a lock.** Depth zero is a claim that this product
    satisfied the customer's exact request in a search that just ran, and a
    lock came from a previous turn with no current search behind it. Inventing
    the claim would be indistinguishable from having verified it.
    """

    @model_validator(mode="after")
    def _provenance_matches_origin(self) -> Self:
        if self.locked and self.relaxation_depth is not None:
            raise ValueError("a locked line carries no current search provenance")
        if not self.locked:
            if self.acquisition is not BundleAcquisition.TO_BUY:
                raise ValueError("a newly selected product is bought")
            if self.relaxation_depth is None:
                raise ValueError("a selected line carries its search provenance")
        return self

    @property
    def unit_price(self) -> Decimal:
        return self.product.price_amount

    @property
    def price_unit(self) -> str:
        return self.product.price_unit

    @property
    def line_total(self) -> Decimal:
        return self.product.price_amount * self.quantity


class BundleStatus(StrEnum):
    COMPLETE = "complete"
    """Every REQUIRED need satisfied at its full quantity, within any budget.

    Recommended and optional needs do not enter into it: a room works when the
    pieces it cannot do without are there. What was left out is in `unmet`, so
    completeness never has to be read as "nothing is missing".
    """

    PARTIAL = "partial"
    """A compliant bundle exists; at least one REQUIRED need is short."""

    INFEASIBLE = "infeasible"
    """The locks the customer will not give up already exceed the budget.

    Not an error and not an empty answer: the locked lines are returned so the
    condition can be explained. Dropping a lock to produce a prettier bundle is
    the one thing forbidden here (CLAUDE.md 10).
    """


class TotalUnavailableReason(StrEnum):
    MIXED_PRICE_UNITS = "mixed_price_units"
    """Selected products are priced in more than one unit. No converter exists,
    so there is no total - as opposed to a total that happens to be unknown."""

    NO_PRICED_LINES = "no_priced_lines"
    """Nothing is being bought and no budget names a unit, so there is no
    currency to express zero in. Reachable when a room is entirely furnished by
    pieces the customer already owns."""


class RoomBundle(BaseModel):
    """One deterministic whole-room selection.

    The validators below exist so a partial room cannot be described as a
    complete one, whatever a caller passes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: tuple[BundleLine, ...] = ()
    status: BundleStatus
    unmet: tuple[UnmetNeed, ...] = ()

    new_spend_total: Decimal | None = None
    """Money still to spend: the sum of `TO_BUY` lines only.

    Named for what it means. A piece the customer already owns is in the room
    and not in this figure, and a field called `total` would read as the room's
    value while meaning something else.
    """

    currency: str | None = None
    total_unavailable: TotalUnavailableReason | None = None

    @model_validator(mode="after")
    def _the_arithmetic_is_internally_consistent(self) -> Self:
        priced = self.new_spend_total is not None
        if priced != (self.currency is not None):
            raise ValueError("a total and its currency are present together")
        if priced == (self.total_unavailable is not None):
            raise ValueError("a total is either available or explained, never both")
        return self

    @model_validator(mode="after")
    def _completeness_is_earned(self) -> Self:
        required_short = any(
            entry.priority is DesignPriority.REQUIRED for entry in self.unmet
        )
        if self.status is BundleStatus.COMPLETE and required_short:
            raise ValueError("a bundle missing a required need is not complete")
        if self.status is BundleStatus.PARTIAL and not required_short:
            raise ValueError("a partial bundle is short of a required need")
        if self.status is BundleStatus.INFEASIBLE and any(
            not line.locked for line in self.lines
        ):
            raise ValueError("an infeasible bundle selected nothing new")
        return self

    @model_validator(mode="after")
    def _one_line_per_need_unit(self) -> Self:
        """A need is satisfied by one selected line, plus any locks covering it.

        Two *selected* lines for one need would mean the residual was filled
        twice, which the all-or-nothing rule forbids.
        """
        selected = [
            line.need_index
            for line in self.lines
            if not line.locked and line.need_index is not None
        ]
        if len(selected) != len(set(selected)):
            raise ValueError("a need receives at most one newly selected product")
        return self


class BundleUnavailableReason(StrEnum):
    """Why no bundle could be computed at all.

    Every member means the arithmetic itself is impossible, not that the answer
    was disappointing. A room that simply cannot afford much is a `PARTIAL`
    bundle, not one of these.

    `LOCKED_PRODUCT_UNAVAILABLE` is deliberately **absent**: the optimiser
    receives verified products and cannot tell a lock that failed to hydrate
    from one nobody passed. Establishing that is the caller's job, before this
    service is reached.
    """

    UNSUPPORTED_BUDGET_FORM = "unsupported_budget_form"
    """A minimum or a range. Honouring only the ceiling would answer a
    different question, and optimising toward a minimum spend is not a thing
    this service does."""

    BUDGET_NOT_COMPARABLE = "budget_not_comparable"
    """A locked product that must be bought is priced in another currency. It
    cannot be dropped and it cannot be compared, so no total exists."""

    LOCKED_PRICE_UNUSABLE = "locked_price_unusable"
    """A locked product that must be bought has no price commerce can act on.
    Distinct from the above: a different explanation is owed."""


class BundleUnavailable(BaseModel):
    """No bundle could be computed. A reason code, never wording."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: BundleUnavailableReason


BundleOptimizationOutcome = RoomBundle | BundleUnavailable
