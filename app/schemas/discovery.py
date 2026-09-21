"""Structured product discovery contracts.

This is the minimum contract deterministic PostgreSQL retrieval needs - not a
general search language. Style, colour, material, dimensional filtering and
semantic text are deliberately absent; they arrive with the milestones that
actually use them (CLAUDE.md 12.1).

The request carries no retailer identity. Catalog scope comes only from an
immutable :class:`~app.schemas.retailer.RetailerContext` resolved by
application code, so a caller - eventually a model - cannot choose a store
(CLAUDE.md 8, 20.2).

On ``SearchConstraint``: CLAUDE.md keeps the name as an approved architectural
concept. M6 deliberately does NOT introduce it as a base class. Price and
seating capacity have different value types, different validation and different
SQL, and a shared abstraction over two members would hide that without removing
any duplication. The concrete constraints below are the implementation of that
concept for now; a common type can be extracted when a third constraint shows
what it should actually share.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.product import ProductCandidate
from app.taxonomy.dimensions import DimensionRole, SourceAxis

MAX_EXCLUDED_PRODUCT_IDS = 100
"""Upper bound on one request's exclusion list.

A conversation refers to a handful of products, not a page of them, and an
unbounded `NOT IN` would be a way to push a large payload into SQL.
"""


class ProductSort(StrEnum):
    """Deterministic orderings. No relevance ranking exists yet, and these must
    not be presented as one."""

    DEFAULT = "default"
    """Stable catalog order (product id ascending)."""

    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"


class PriceConstraint(BaseModel):
    """Inclusive price bounds in one explicit currency.

    The currency is required and is matched exactly against ``price_unit``.
    Nothing here converts between currencies, and SAR 5000 is never treated as
    comparable to USD 5000. The value is not upper-cased or otherwise
    normalised: normalising would be a guess about a column the upstream import
    does not validate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: str = Field(min_length=1, max_length=255)
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, ge=0)

    min_exclusive: bool = False
    """Compare the floor with `>` rather than `>=`."""

    max_exclusive: bool = False
    """Compare the ceiling with `<` rather than `<=`.

    Both default to False, so a constraint written before these existed keeps
    exactly the eligibility it had. They exist because "cheaper than this one"
    is a strict relation: an inclusive ceiling at the reference price returns
    products costing the same, and those are not cheaper.
    """

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if self.min_amount is None and self.max_amount is None:
            raise ValueError("a price constraint needs at least one bound")
        if (
            self.min_amount is not None
            and self.max_amount is not None
            and self.min_amount > self.max_amount
        ):
            raise ValueError("price min_amount cannot exceed max_amount")
        if not self.currency.strip():
            raise ValueError("a price constraint needs an explicit currency")
        if self.min_exclusive and self.min_amount is None:
            raise ValueError("min_exclusive needs a min_amount to exclude")
        if self.max_exclusive and self.max_amount is None:
            raise ValueError("max_exclusive needs a max_amount to exclude")
        if (
            self.min_amount is not None
            and self.min_amount == self.max_amount
            and (self.min_exclusive or self.max_exclusive)
        ):
            # An exclusive bound on a single point admits nothing at all: a
            # contradiction, not a very narrow search.
            raise ValueError(
                "an exclusive bound on equal min and max can never be satisfied"
            )
        return self

    @classmethod
    def below(cls, amount: Decimal, currency: str) -> PriceConstraint:
        """Strictly cheaper than `amount`."""
        return cls(currency=currency, max_amount=amount, max_exclusive=True)

    @classmethod
    def above(cls, amount: Decimal, currency: str) -> PriceConstraint:
        """Strictly more expensive than `amount`."""
        return cls(currency=currency, min_amount=amount, min_exclusive=True)

    @classmethod
    def at_most(cls, amount: Decimal, currency: str) -> PriceConstraint:
        return cls(currency=currency, max_amount=amount)

    @classmethod
    def between(cls, low: Decimal, high: Decimal, currency: str) -> PriceConstraint:
        return cls(currency=currency, min_amount=low, max_amount=high)


class SeatingCapacityConstraint(BaseModel):
    """Inclusive bounds on the reviewed ``seating_capacity`` column.

    Matched against the stored value only. Capacity is never inferred from the
    subcategory or the product name, so a `single-seater-sofa` whose reviewed
    capacity is NULL does not satisfy any capacity constraint (CLAUDE.md 31).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_capacity: int | None = Field(default=None, gt=0)
    max_capacity: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if self.min_capacity is None and self.max_capacity is None:
            raise ValueError("a seating capacity constraint needs at least one bound")
        if (
            self.min_capacity is not None
            and self.max_capacity is not None
            and self.min_capacity > self.max_capacity
        ):
            raise ValueError("seating min_capacity cannot exceed max_capacity")
        return self

    @classmethod
    def exactly(cls, capacity: int) -> SeatingCapacityConstraint:
        return cls(min_capacity=capacity, max_capacity=capacity)

    @classmethod
    def at_least(cls, capacity: int) -> SeatingCapacityConstraint:
        return cls(min_capacity=capacity)

    @classmethod
    def at_most(cls, capacity: int) -> SeatingCapacityConstraint:
        return cls(max_capacity=capacity)


class DimensionConstraintKind(StrEnum):
    MIN = "min"
    MAX = "max"
    RANGE = "range"
    TARGET = "target"
    """A figure to sit near, not a bound. "around 220 cm" is a target."""


class DimensionConstraint(BaseModel):
    """One numeric requirement, in the customer's own physical terms.

    Carries a customer-facing :class:`DimensionRole`, never a database column:
    resolving a role to a stored axis is the dimension registry's job, below
    this boundary (CLAUDE.md 15.1).

    Values are already centimetres. `source_value` and `source_unit` keep what
    the customer actually said, so a result can be explained in their units.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: DimensionRole
    kind: DimensionConstraintKind
    min_cm: Decimal | None = Field(default=None, gt=0)
    max_cm: Decimal | None = Field(default=None, gt=0)
    target_cm: Decimal | None = Field(default=None, gt=0)
    source_value: str | None = None
    source_unit: str | None = None

    @model_validator(mode="after")
    def _check_kind(self) -> Self:
        """Each kind carries exactly the values it means, and no others."""
        required = {
            DimensionConstraintKind.MIN: ("min_cm",),
            DimensionConstraintKind.MAX: ("max_cm",),
            DimensionConstraintKind.RANGE: ("min_cm", "max_cm"),
            DimensionConstraintKind.TARGET: ("target_cm",),
        }[self.kind]
        for field in ("min_cm", "max_cm", "target_cm"):
            value = getattr(self, field)
            if field in required and value is None:
                raise ValueError(f"a {self.kind} dimension constraint needs {field}")
            if field not in required and value is not None:
                raise ValueError(f"a {self.kind} dimension constraint must not set {field}")
        if (
            self.kind is DimensionConstraintKind.RANGE
            and self.min_cm is not None
            and self.max_cm is not None
            and self.min_cm > self.max_cm
        ):
            raise ValueError("dimension min_cm cannot exceed max_cm")
        return self


class PlanarDimensionConstraint(BaseModel):
    """Two sides of a flat product, matched as a set.

    A rug described as 200 x 300 is the same rug as 300 x 200; the stored order
    is an artefact of data entry, not a fact about the product. Only
    subcategories the registry marks as planar may carry one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    first_cm: Decimal = Field(gt=0)
    second_cm: Decimal = Field(gt=0)
    source_unit: str | None = None

    @property
    def sides(self) -> tuple[Decimal, Decimal]:
        """The pair in ascending order, so comparison ignores how it was said."""
        return (
            (self.first_cm, self.second_cm)
            if self.first_cm <= self.second_cm
            else (self.second_cm, self.first_cm)
        )


class AxisConstraint(BaseModel):
    """A dimension constraint with its role already resolved to a stored axis.

    Built only by discovery, from the registry. The axis is an enum of the three
    real columns, so nothing a caller or a model supplies can name a column.
    """

    model_config = ConfigDict(frozen=True)

    axis: SourceAxis
    kind: DimensionConstraintKind
    min_cm: Decimal | None = None
    max_cm: Decimal | None = None
    target_cm: Decimal | None = None


class ProductSearchRequest(BaseModel):
    """A validated structured request. Product Discovery never sees language.

    ``commerce_category`` is required. Query understanding is expected to
    resolve at least a category before discovery runs ("show me a table" gives
    `tables` with no subcategory, which is enough), and requiring it also keeps
    a request from sweeping the tens of thousands of products that carry no
    reviewed commerce classification at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)
    price: PriceConstraint | None = None
    seating_capacity: SeatingCapacityConstraint | None = None
    # Exact, strictly-required catalog attributes only. Ordinary colour and
    # style wording is a semantic preference and does not belong here - it
    # would silently exclude products the customer never ruled out
    # (CLAUDE.md 12.4). Values are approved vocabulary, validated before SQL.
    dimensions: tuple[DimensionConstraint, ...] = ()
    """Numeric requirements, in customer-facing roles. Combined with AND."""

    planar_dimensions: PlanarDimensionConstraint | None = None
    """An unordered pair, for the subcategories whose sides have no fixed order."""

    colors_any_of: tuple[str, ...] = ()
    """Any one of these colours satisfies the requirement (OR)."""

    styles_all_of: tuple[str, ...] = ()
    """Every one of these styles must be present on the product (AND)."""

    exclude_product_ids: tuple[int, ...] = ()
    """Products this search must not return, whatever else matches.

    An eligibility predicate, not a post-filter: applied in the query before
    ordering and limiting, so a bounded page is never silently short
    (CLAUDE.md 15.1). Set only by application code from an already-resolved
    reference - "something similar to this one" must not return that one.
    """

    # None means "use the configured default"; the service resolves it and
    # rejects anything above the configured hard maximum.
    limit: int | None = Field(default=None, gt=0)
    sort: ProductSort = ProductSort.DEFAULT

    @field_validator("exclude_product_ids")
    @classmethod
    def _bounded_exclusions(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(value) > MAX_EXCLUDED_PRODUCT_IDS:
            raise ValueError(
                f"a request may exclude at most {MAX_EXCLUDED_PRODUCT_IDS} products"
            )
        if len(value) != len(set(value)):
            raise ValueError("exclude_product_ids must not repeat a product id")
        return value

    @model_validator(mode="after")
    def _check_one_constraint_per_role(self) -> Self:
        """At most one constraint per measurement.

        The kinds already cover every shape a single measurement can take: a
        RANGE carries both bounds, a TARGET one figure, MIN and MAX one
        direction each. Two constraints on the same role would therefore be
        either redundant or contradictory, and merging them would mean deciding
        which of the customer's two statements to discard.
        """
        roles = [constraint.role for constraint in self.dimensions]
        if len(roles) != len(set(roles)):
            raise ValueError("a request carries at most one constraint per dimension role")
        return self

    def applied_filters(self) -> tuple[str, ...]:
        """Which structured filter types this request carries. For logging."""
        filters = ["commerce_category"]
        if self.commerce_subcategory is not None:
            filters.append("commerce_subcategory")
        if self.price is not None:
            filters.append("price")
        if self.seating_capacity is not None:
            filters.append("seating_capacity")
        if self.dimensions:
            filters.append("dimensions")
        if self.planar_dimensions is not None:
            filters.append("planar_dimensions")
        if self.colors_any_of:
            filters.append("colors_any_of")
        if self.styles_all_of:
            filters.append("styles_all_of")
        if self.exclude_product_ids:
            filters.append("exclude_product_ids")
        return tuple(filters)


class ProductSearchResult(BaseModel):
    """An eligible candidate pool, in a deterministic order.

    Candidates are eligible, not ranked: PostgreSQL decided eligibility and
    nothing has scored them. There is deliberately no relevance score.
    """

    model_config = ConfigDict(frozen=True)

    candidates: tuple[ProductCandidate, ...]
    limit: int
    """The bound actually applied, after defaults and the configured maximum."""

    truncated: bool
    """True when more eligible products existed than the limit allowed."""
