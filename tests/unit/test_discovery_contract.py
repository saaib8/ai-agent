"""The structured search contract: what a valid request may and may not say."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.discovery import (
    PriceConstraint,
    ProductSearchRequest,
    ProductSort,
    SeatingCapacityConstraint,
)
from pydantic import ValidationError

SAR = "SAR"


# ── shape ───────────────────────────────────────────────────────────────────


def test_a_category_alone_is_a_valid_request() -> None:
    """"show me a table" resolves a category and no subcategory."""
    request = ProductSearchRequest(commerce_category="tables")

    assert request.commerce_subcategory is None
    assert request.sort is ProductSort.DEFAULT
    assert request.limit is None
    assert request.applied_filters() == ("commerce_category",)


def test_a_category_and_subcategory_is_a_valid_request() -> None:
    request = ProductSearchRequest(
        commerce_category="tables", commerce_subcategory="nightstand"
    )

    assert request.applied_filters() == ("commerce_category", "commerce_subcategory")


def test_commerce_category_is_required() -> None:
    """Without it a request would sweep the unclassified catalog."""
    with pytest.raises(ValidationError):
        ProductSearchRequest()  # type: ignore[call-arg]


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_category_is_rejected(blank: str) -> None:
    with pytest.raises(ValidationError):
        ProductSearchRequest(commerce_category=blank.strip() or "")


def test_a_request_cannot_carry_a_store_id() -> None:
    """Retailer identity comes only from RetailerContext (CLAUDE.md 8)."""
    with pytest.raises(ValidationError):
        ProductSearchRequest(commerce_category="seating", store_id=50)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "field", ["is_active", "commerce_category_sql", "order_by", "material", "style"]
)
def test_a_request_cannot_carry_arbitrary_fields(field: str) -> None:
    """No caller may disable active filtering or name a database column."""
    with pytest.raises(ValidationError):
        ProductSearchRequest(**{"commerce_category": "seating", field: "x"})


def test_a_request_is_immutable() -> None:
    request = ProductSearchRequest(commerce_category="seating")
    with pytest.raises(ValidationError):
        request.commerce_category = "lighting"  # type: ignore[misc]


# ── price ───────────────────────────────────────────────────────────────────


def test_price_max_only() -> None:
    price = PriceConstraint.at_most(Decimal("5000"), SAR)

    assert price.max_amount == Decimal("5000")
    assert price.min_amount is None
    assert price.currency == SAR


def test_price_min_only() -> None:
    assert PriceConstraint(currency=SAR, min_amount=Decimal("2500")).max_amount is None


def test_price_range() -> None:
    price = PriceConstraint.between(Decimal("2500"), Decimal("6000"), SAR)

    assert (price.min_amount, price.max_amount) == (Decimal("2500"), Decimal("6000"))


def test_price_requires_an_explicit_currency() -> None:
    """SAR 5000 and USD 5000 must never be treated as the same constraint."""
    with pytest.raises(ValidationError):
        PriceConstraint(min_amount=Decimal("100"))  # type: ignore[call-arg]


@pytest.mark.parametrize("currency", ["", "   "])
def test_a_blank_currency_is_rejected(currency: str) -> None:
    with pytest.raises(ValidationError):
        PriceConstraint(currency=currency, max_amount=Decimal("100"))


def test_a_price_constraint_needs_at_least_one_bound() -> None:
    with pytest.raises(ValidationError, match="at least one bound"):
        PriceConstraint(currency=SAR)


@pytest.mark.parametrize("amount", [Decimal("-1"), Decimal("-0.01")])
def test_negative_prices_are_rejected(amount: Decimal) -> None:
    with pytest.raises(ValidationError):
        PriceConstraint(currency=SAR, max_amount=amount)


def test_price_min_above_max_is_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        PriceConstraint(currency=SAR, min_amount=Decimal("6000"), max_amount=Decimal("2500"))


def test_prices_are_decimal_not_float() -> None:
    price = PriceConstraint(currency=SAR, max_amount=Decimal("0.1"))
    assert isinstance(price.max_amount, Decimal)
    assert price.max_amount == Decimal("0.1")


# ── seating capacity ────────────────────────────────────────────────────────


def test_seating_exactly() -> None:
    capacity = SeatingCapacityConstraint.exactly(3)
    assert (capacity.min_capacity, capacity.max_capacity) == (3, 3)


def test_seating_at_least() -> None:
    capacity = SeatingCapacityConstraint.at_least(4)
    assert (capacity.min_capacity, capacity.max_capacity) == (4, None)


def test_seating_at_most() -> None:
    capacity = SeatingCapacityConstraint.at_most(3)
    assert (capacity.min_capacity, capacity.max_capacity) == (None, 3)


def test_seating_min_above_max_is_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        SeatingCapacityConstraint(min_capacity=5, max_capacity=2)


@pytest.mark.parametrize("capacity", [0, -1])
def test_non_positive_capacity_is_rejected(capacity: int) -> None:
    with pytest.raises(ValidationError):
        SeatingCapacityConstraint(min_capacity=capacity)


def test_a_capacity_constraint_needs_at_least_one_bound() -> None:
    with pytest.raises(ValidationError, match="at least one bound"):
        SeatingCapacityConstraint()


# ── limit and sort ──────────────────────────────────────────────────────────


def test_limit_defaults_to_unset_so_the_service_applies_configuration() -> None:
    assert ProductSearchRequest(commerce_category="seating").limit is None


@pytest.mark.parametrize("limit", [0, -5])
def test_a_non_positive_limit_is_rejected(limit: int) -> None:
    with pytest.raises(ValidationError):
        ProductSearchRequest(commerce_category="seating", limit=limit)


@pytest.mark.parametrize("sort", list(ProductSort))
def test_every_sort_value_is_accepted(sort: ProductSort) -> None:
    assert ProductSearchRequest(commerce_category="seating", sort=sort).sort is sort


@pytest.mark.parametrize(
    "sort", ["relevance", "price_amount DESC", "id; DROP TABLE core_product", "name"]
)
def test_arbitrary_sort_values_are_rejected(sort: str) -> None:
    """Ordering is an enum, never a caller-supplied column name."""
    with pytest.raises(ValidationError):
        ProductSearchRequest(commerce_category="seating", sort=sort)


def test_there_are_only_deterministic_sorts() -> None:
    """No relevance ordering exists yet, so none may be offered."""
    assert {str(s) for s in ProductSort} == {"default", "price_asc", "price_desc"}
