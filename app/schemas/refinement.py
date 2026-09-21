"""A typed delta over the active search.

"Make them beige" cannot go through M7: it is contextless by contract and
would read that message as a search for nothing in particular. So a refinement
arrives as typed operations over the search already in progress.

Three rules carry the design.

* **Omission preserves.** A field absent from the delta provably cannot change,
  which is what lets "show me cheaper ones" leave a locked budget and a colour
  requirement from three turns ago exactly where they were.
* **Clearing is something someone asked for.** A bare `None` cannot say whether
  it means "leave it alone" or "empty it", so every axis takes an explicit
  operation. Without it, "any style is fine" would be indistinguishable from
  saying nothing about style.
* **No operation ordering.** At most one operation per axis, per measurement
  and per attribute family, so nothing depends on which came first in a tuple.

Nothing here executes. Composing a delta onto an active search - including
unit inheritance, currency inheritance and default precedence - is the
deterministic composer's work, not this contract's.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.discovery import DimensionConstraintKind, ProductSort
from app.schemas.product_reference import ProductReferenceSelector
from app.schemas.query import ConstraintStrength
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.dimensions import DimensionRole


class RefinementOp(StrEnum):
    SET = "set"
    CLEAR = "clear"
    """Remove the restriction entirely. Not the same as never mentioning it."""


class AttributeRefinementOp(StrEnum):
    """Colour and style carry a third case that price and capacity do not.

    Ordinary wanting is a preference; only explicitly strict wording is a
    filter (CLAUDE.md 12.4). The operation records which of the two the
    customer expressed, so the composer never has to re-read their words.
    """

    SET_REQUIREMENT = "set_requirement"
    SET_PREFERENCE = "set_preference"
    CLEAR = "clear"


class SemanticIntentOp(StrEnum):
    SET = "set"
    CLEAR = "clear"


def _forbid_payload(model: BaseModel, fields: tuple[str, ...], operation: str) -> None:
    for name in fields:
        if getattr(model, name) is not None:
            raise ValueError(f"a {operation} operation must not carry {name}")


class SemanticIntentRefinement(BaseModel):
    """Durable fuzzy wording - cosy, sleek - that no structured field holds.

    Search-affecting, so it lives only on search-specific contracts and is
    staged with the candidate search. It has exactly one other home, the
    new-search proposal, and none at all on customer state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: SemanticIntentOp
    value: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _value_matches_the_operation(self) -> Self:
        if self.op is SemanticIntentOp.SET:
            if not (self.value or "").strip():
                raise ValueError("a set operation needs a semantic intent")
        elif self.value is not None:
            raise ValueError("a clear operation must not carry a value")
        return self



MAX_PRICE_PERCENT = Decimal("1000")
"""A sanity ceiling on a stated percentage, not a product rule."""


class PriceRelation(StrEnum):
    """A price expressed against another product rather than as a figure.

    "Cheaper than this one" names no amount. Turning it into one needs the
    reference product's current price, which is a catalog fact - so the
    relation is all the model states, and the application resolves the
    product, re-reads its price and computes the bound (CLAUDE.md 3.3).

    The strictness distinction is the customer's own. A bare comparative is a
    strict relation: a product costing the same is not cheaper. An explicit
    percentage names a threshold they will accept, so it includes it. "More
    than X%" is the strict form of that threshold.
    """

    CHEAPER_THAN = "cheaper_than"
    MORE_EXPENSIVE_THAN = "more_expensive_than"
    PERCENT_CHEAPER = "percent_cheaper"
    MORE_THAN_PERCENT_CHEAPER = "more_than_percent_cheaper"
    PERCENT_MORE_EXPENSIVE = "percent_more_expensive"
    MORE_THAN_PERCENT_MORE_EXPENSIVE = "more_than_percent_more_expensive"

    @property
    def needs_percent(self) -> bool:
        return self in _PERCENT_RELATIONS

    @property
    def is_cheaper(self) -> bool:
        return self in _CHEAPER_RELATIONS


_PERCENT_RELATIONS = frozenset(
    {
        PriceRelation.PERCENT_CHEAPER,
        PriceRelation.MORE_THAN_PERCENT_CHEAPER,
        PriceRelation.PERCENT_MORE_EXPENSIVE,
        PriceRelation.MORE_THAN_PERCENT_MORE_EXPENSIVE,
    }
)
_CHEAPER_RELATIONS = frozenset(
    {
        PriceRelation.CHEAPER_THAN,
        PriceRelation.PERCENT_CHEAPER,
        PriceRelation.MORE_THAN_PERCENT_CHEAPER,
    }
)


class RelativePriceRefinement(BaseModel):
    """A price bound the application will compute, stated as a relation.

    Carries the reference itself rather than leaning on a second field
    elsewhere on the decision: one owner, so nothing can disagree about which
    product the comparison is against.

    `percent` is a decimal string for the same reason every other amount here
    is - a percentage read from JSON as a float would reach `Decimal` already
    rounded.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: PriceRelation
    reference: ProductReferenceSelector
    percent: str | None = None
    """The figure the customer actually said. Never one inferred for them:
    "cheaper" is not 20% cheaper (CLAUDE.md 31)."""

    @model_validator(mode="after")
    def _percent_matches_the_relation(self) -> Self:
        if not self.relation.needs_percent:
            if self.percent is not None:
                raise ValueError(f"{self.relation} takes no percentage")
            return self
        if self.percent is None:
            raise ValueError(f"{self.relation} needs the percentage they stated")
        value = self.percent_value
        if value is None:
            raise ValueError("a percentage must be a decimal figure")
        if value <= 0:
            raise ValueError("a percentage must be greater than zero")
        if value > MAX_PRICE_PERCENT:
            raise ValueError(f"a percentage may not exceed {MAX_PRICE_PERCENT}")
        if self.relation.is_cheaper and value >= 100:
            # 100% cheaper is free, and more than that is a negative price.
            raise ValueError("a product cannot be 100% or more cheaper")
        return self

    @property
    def percent_value(self) -> Decimal | None:
        """The stated percentage as a Decimal, or None when unparseable."""
        if self.percent is None:
            return None
        try:
            return Decimal(self.percent.strip())
        except (InvalidOperation, ValueError):
            return None


class PriceRefinementOp(StrEnum):
    """Price carries a third case that capacity and sort do not.

    A relative bound is neither an absolute figure nor an absence, and folding
    it into `SET` would mean a set operation that sometimes carries amounts
    and sometimes carries a product reference.
    """

    SET = "set"
    SET_RELATIVE = "set_relative"
    CLEAR = "clear"


class PriceRefinement(BaseModel):
    """Amounts as decimal strings, so money never passes through a float."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: PriceRefinementOp
    relative: RelativePriceRefinement | None = None
    """Set exactly when the operation is relative. The bound itself is
    computed by the application from a freshly read reference price."""

    min_amount: str | None = None
    max_amount: str | None = None
    currency: str | None = None
    """None means "inherit" - the composer resolves it, never invents it."""

    min_exclusive: bool = False
    max_exclusive: bool = False
    min_strength: ConstraintStrength | None = None
    max_strength: ConstraintStrength | None = None

    @model_validator(mode="after")
    def _payload_matches_the_operation(self) -> Self:
        absolute = ("min_amount", "max_amount", "min_strength", "max_strength")
        if self.op is PriceRefinementOp.SET:
            if self.relative is not None:
                raise ValueError("an absolute price operation carries no relation")
            if self.min_amount is None and self.max_amount is None:
                raise ValueError("a price set operation needs at least one bound")
            if self.min_exclusive and self.min_amount is None:
                raise ValueError("min_exclusive needs a min_amount")
            if self.max_exclusive and self.max_amount is None:
                raise ValueError("max_exclusive needs a max_amount")
            return self
        if self.op is PriceRefinementOp.SET_RELATIVE:
            if self.relative is None:
                raise ValueError("a relative price operation needs a relation")
            # The bound is the application's to compute, so stating one here
            # would be the model doing the arithmetic it must not do.
            _forbid_payload(self, (*absolute, "currency"), "relative price")
            if self.min_exclusive or self.max_exclusive:
                raise ValueError(
                    "strictness comes from the relation, not from a flag"
                )
            return self
        if self.relative is not None:
            raise ValueError("a price clear operation carries no relation")
        _forbid_payload(self, (*absolute, "currency"), "price clear")
        if self.min_exclusive or self.max_exclusive:
            raise ValueError("a price clear operation must not carry exclusivity")
        return self


class CapacityRefinement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    op: RefinementOp
    min_capacity: int | None = Field(default=None, gt=0)
    max_capacity: int | None = Field(default=None, gt=0)
    min_strength: ConstraintStrength | None = None
    max_strength: ConstraintStrength | None = None

    @model_validator(mode="after")
    def _payload_matches_the_operation(self) -> Self:
        if self.op is RefinementOp.SET:
            if self.min_capacity is None and self.max_capacity is None:
                raise ValueError("a capacity set operation needs at least one bound")
            return self
        _forbid_payload(
            self,
            ("min_capacity", "max_capacity", "min_strength", "max_strength"),
            "capacity clear",
        )
        return self


class DimensionRefinement(BaseModel):
    """One measurement, addressed by the role the customer meant.

    `role` is required on a clear as well as a set: clearing "the width" has to
    say which measurement, and a clear that named nothing would have to mean
    "all of them", which no customer has asked for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: RefinementOp
    role: DimensionRole
    kind: DimensionConstraintKind | None = None
    min_value: str | None = None
    max_value: str | None = None
    target_value: str | None = None
    unit: str | None = None
    """None means "inherit from this same role" - never a default of cm."""

    strength: ConstraintStrength | None = None

    @model_validator(mode="after")
    def _payload_matches_the_operation(self) -> Self:
        values = ("min_value", "max_value", "target_value")
        if self.op is RefinementOp.SET:
            if self.kind is None:
                raise ValueError("a dimension set operation needs a kind")
            required = {
                DimensionConstraintKind.MIN: ("min_value",),
                DimensionConstraintKind.MAX: ("max_value",),
                DimensionConstraintKind.RANGE: ("min_value", "max_value"),
                DimensionConstraintKind.TARGET: ("target_value",),
            }[self.kind]
            for name in values:
                present = getattr(self, name) is not None
                if name in required and not present:
                    raise ValueError(f"a {self.kind} dimension needs {name}")
                if name not in required and present:
                    raise ValueError(f"a {self.kind} dimension must not set {name}")
            return self
        _forbid_payload(self, (*values, "kind", "unit", "strength"), "dimension clear")
        return self


class PlanarRefinement(BaseModel):
    """Two sides given together, as in a rug 200 x 300."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: RefinementOp
    first_value: str | None = None
    second_value: str | None = None
    unit: str | None = None
    strength: ConstraintStrength | None = None

    @model_validator(mode="after")
    def _payload_matches_the_operation(self) -> Self:
        if self.op is RefinementOp.SET:
            if self.first_value is None or self.second_value is None:
                raise ValueError("a planar set operation needs both sides")
            return self
        _forbid_payload(
            self,
            ("first_value", "second_value", "unit", "strength"),
            "planar clear",
        )
        return self


class ProposedAttributeValue(BaseModel):
    """A colour or style the customer named, in their words.

    `canonical_value` is set only when the wording names an approved value
    outright. It stays None for anything else - "warm neutral" is not Beige -
    and the application validates whatever is claimed against the registry
    before it can reach SQL (CLAUDE.md 14.3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_value: str = Field(min_length=1)
    canonical_value: str | None = None


class AttributeRefinement(BaseModel):
    """One family's whole position, replaced or cleared.

    Replace rather than add: `styles_all_of` is a conjunction, so adding
    Japandi to Modern would silently narrow the search to products carrying
    both, and the customer would see nothing. A genuine conjunction arrives as
    one operation carrying both values.

    A clear removes the requirement **and** the preferences for that family.
    "I don't care about style" must not leave a stale leaning steering the
    ranking.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: AttributeRefinementOp
    family: AttributeFamily
    values: tuple[ProposedAttributeValue, ...] = ()

    @model_validator(mode="after")
    def _payload_matches_the_operation(self) -> Self:
        if self.op is AttributeRefinementOp.CLEAR:
            if self.values:
                raise ValueError("an attribute clear operation must not carry values")
            return self
        if not self.values:
            raise ValueError("an attribute set operation needs at least one value")
        seen = [v.raw_value.casefold() for v in self.values]
        if len(set(seen)) != len(seen):
            raise ValueError("an attribute operation must not repeat a value")
        return self


class SortRefinement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    op: RefinementOp
    value: ProductSort | None = None

    @model_validator(mode="after")
    def _payload_matches_the_operation(self) -> Self:
        if self.op is RefinementOp.SET:
            if self.value is None:
                raise ValueError("a sort set operation needs a value")
        elif self.value is not None:
            raise ValueError("a sort clear operation must not carry a value")
        return self


class SearchRefinementDelta(BaseModel):
    """One turn's changes to the active search. Absent means untouched.

    There is deliberately no `commerce_category` or `commerce_subcategory`:
    the taxonomy is M7's and the registry's, and a delta that could name a
    product type would be a way for a model to invent one (CLAUDE.md 14.3).
    Changing the product type is a separate, registry-validated path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    price: PriceRefinement | None = None
    seating_capacity: CapacityRefinement | None = None
    dimensions: tuple[DimensionRefinement, ...] = ()
    planar_dimensions: PlanarRefinement | None = None
    attributes: tuple[AttributeRefinement, ...] = ()
    semantic_intent: SemanticIntentRefinement | None = None
    sort: SortRefinement | None = None

    @model_validator(mode="after")
    def _one_operation_per_axis(self) -> Self:
        """No axis may carry two operations, so ordering decides nothing.

        `SET width` beside `CLEAR width` has no correct reading, and picking
        the later one would make a tuple's order load-bearing.
        """
        roles = [d.role for d in self.dimensions]
        if len(set(roles)) != len(roles):
            raise ValueError("at most one operation per dimension role")
        families = [a.family for a in self.attributes]
        if len(set(families)) != len(families):
            raise ValueError("at most one operation per attribute family")
        return self

    def is_empty(self) -> bool:
        """True when the delta would change nothing."""
        return not any(
            (
                self.price,
                self.seating_capacity,
                self.dimensions,
                self.planar_dimensions,
                self.attributes,
                self.semantic_intent,
                self.sort,
            )
        )
