"""Strict price bounds and product exclusion: the two additive M6 primitives.

"Cheaper than this one" is a strict relation. An inclusive ceiling at the
reference price returns products costing exactly the same, which are not
cheaper - so the contract has to be able to say `<` rather than `<=`, and say
it without an epsilon that would encode a column's scale in domain logic.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.discovery import (
    MAX_EXCLUDED_PRODUCT_IDS,
    PriceConstraint,
    ProductSearchRequest,
)
from pydantic import ValidationError

SAR = "SAR"


# ── defaults preserve every existing search ─────────────────────────────────


def test_bounds_are_inclusive_unless_asked_otherwise() -> None:
    """Every constraint written before these flags existed keeps its meaning."""
    constraint = PriceConstraint(
        currency=SAR, min_amount=Decimal("1000"), max_amount=Decimal("5000")
    )

    assert constraint.min_exclusive is False
    assert constraint.max_exclusive is False


def test_the_existing_helpers_stay_inclusive() -> None:
    assert PriceConstraint.at_most(Decimal("5000"), SAR).max_exclusive is False
    assert (
        PriceConstraint.between(Decimal("1"), Decimal("2"), SAR).min_exclusive is False
    )


# ── the strict helpers ──────────────────────────────────────────────────────


def test_below_is_strictly_cheaper() -> None:
    constraint = PriceConstraint.below(Decimal("5000"), SAR)

    assert constraint.max_amount == Decimal("5000")
    assert constraint.max_exclusive is True
    assert constraint.min_amount is None


def test_above_is_strictly_more_expensive() -> None:
    constraint = PriceConstraint.above(Decimal("5000"), SAR)

    assert constraint.min_amount == Decimal("5000")
    assert constraint.min_exclusive is True


# ── validation ──────────────────────────────────────────────────────────────


def test_an_exclusive_floor_needs_a_floor_to_exclude() -> None:
    with pytest.raises(ValidationError, match="min_exclusive"):
        PriceConstraint(currency=SAR, max_amount=Decimal("5000"), min_exclusive=True)


def test_an_exclusive_ceiling_needs_a_ceiling_to_exclude() -> None:
    with pytest.raises(ValidationError, match="max_exclusive"):
        PriceConstraint(currency=SAR, min_amount=Decimal("1000"), max_exclusive=True)


@pytest.mark.parametrize(
    ("min_exclusive", "max_exclusive"), [(True, False), (False, True), (True, True)]
)
def test_an_exclusive_bound_on_a_single_point_is_unsatisfiable(
    min_exclusive: bool, max_exclusive: bool
) -> None:
    """Asking for exactly 5000 but not 5000 admits nothing; say so at build time."""
    with pytest.raises(ValidationError, match="never be satisfied"):
        PriceConstraint(
            currency=SAR,
            min_amount=Decimal("5000"),
            max_amount=Decimal("5000"),
            min_exclusive=min_exclusive,
            max_exclusive=max_exclusive,
        )


def test_an_inclusive_single_point_is_still_allowed() -> None:
    constraint = PriceConstraint(
        currency=SAR, min_amount=Decimal("5000"), max_amount=Decimal("5000")
    )

    assert constraint.min_amount == constraint.max_amount


def test_exclusivity_does_not_relax_the_existing_bound_rules() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        PriceConstraint(
            currency=SAR,
            min_amount=Decimal("6000"),
            max_amount=Decimal("5000"),
            max_exclusive=True,
        )


# ── product exclusion ───────────────────────────────────────────────────────


def test_a_request_excludes_nothing_by_default() -> None:
    assert ProductSearchRequest(commerce_category="seating").exclude_product_ids == ()


def test_exclusions_are_reported_as_an_applied_filter() -> None:
    request = ProductSearchRequest(
        commerce_category="seating", exclude_product_ids=(42,)
    )

    assert "exclude_product_ids" in request.applied_filters()


def test_a_repeated_exclusion_is_refused() -> None:
    with pytest.raises(ValidationError, match="repeat"):
        ProductSearchRequest(commerce_category="seating", exclude_product_ids=(7, 7))


def test_the_exclusion_list_is_bounded() -> None:
    """An unbounded NOT IN would be a way to push a large payload into SQL."""
    too_many = tuple(range(MAX_EXCLUDED_PRODUCT_IDS + 1))

    with pytest.raises(ValidationError, match="at most"):
        ProductSearchRequest(
            commerce_category="seating", exclude_product_ids=too_many
        )


def test_the_bound_itself_is_allowed() -> None:
    at_limit = tuple(range(MAX_EXCLUDED_PRODUCT_IDS))
    request = ProductSearchRequest(
        commerce_category="seating", exclude_product_ids=at_limit
    )

    assert len(request.exclude_product_ids) == MAX_EXCLUDED_PRODUCT_IDS
