"""Every non-widened field survives a widened request.

The defect this closes: `RelaxationPlanner._build` used to rebuild
`ProductSearchRequest` by naming each field, so adding a field to the contract
silently dropped it from every widened attempt. A product exclusion would hold
at depth 0 and lapse at depth 1; a strict price bound would quietly become
inclusive. Both are invisible in a passing suite and wrong in production.

The first test is the general guard. The rest pin the two fields that would
have failed, because a general guard is easy to weaken by accident.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.core.config import RelaxationSettings
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PriceConstraint,
    ProductSearchRequest,
    ProductSort,
    SeatingCapacityConstraint,
)
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    ResolvedSearch,
)
from app.services.relaxation import RelaxationPlanner
from app.taxonomy.dimensions import DimensionRole

SAR = "SAR"
APPROXIMATE = ConstraintStrength.APPROXIMATE

# What relaxation is allowed to change. Everything else must arrive untouched.
WIDENED_FIELDS = {"price", "seating_capacity", "dimensions"}


def _resolved(**request_overrides: Any) -> ResolvedSearch:
    values: dict[str, Any] = {
        "commerce_category": "seating",
        "commerce_subcategory": "sofa",
        "price": PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
    }
    values.update(request_overrides)
    return ResolvedSearch(
        request=ProductSearchRequest(**values),
        semantics=ConstraintSemantics(price_max=APPROXIMATE),
    )


def _plan(resolved: ResolvedSearch) -> tuple[Any, ...]:
    planned = RelaxationPlanner(RelaxationSettings()).plan(resolved)
    assert planned, "this fixture must actually widen something"
    return planned


def test_every_non_widened_field_survives_reconstruction() -> None:
    """Reflection over the contract, so the NEXT field added cannot vanish.

    Deliberately not a list of field names: a list is the same mistake the
    reconstruction made.
    """
    resolved = _resolved(
        commerce_subcategory="sofa",
        colors_any_of=("Beige",),
        styles_all_of=("Modern",),
        exclude_product_ids=(101, 202),
        limit=17,
        sort=ProductSort.PRICE_DESC,
    )
    original = resolved.request

    checked = set()
    for planned in _plan(resolved):
        for name in ProductSearchRequest.model_fields:
            if name in WIDENED_FIELDS:
                continue
            assert getattr(planned.request, name) == getattr(original, name), name
            checked.add(name)

    # The guard is worthless if the contract's fields stopped being visible.
    assert checked == set(ProductSearchRequest.model_fields) - WIDENED_FIELDS
    assert len(checked) >= 6


def test_the_widened_fields_are_the_only_ones_that_may_move() -> None:
    """Pins the allowlist above to the contract, so a new field is a decision."""
    assert set(ProductSearchRequest.model_fields) >= WIDENED_FIELDS
    assert "commerce_category" not in WIDENED_FIELDS
    assert "exclude_product_ids" not in WIDENED_FIELDS


# ── the two fields that would have been dropped ─────────────────────────────


def test_an_excluded_product_stays_excluded_at_every_depth() -> None:
    """Otherwise "something similar to this" returns that one once it widens."""
    planned = _plan(_resolved(exclude_product_ids=(42,)))

    assert all(p.request.exclude_product_ids == (42,) for p in planned)


def test_price_exclusivity_survives_every_widening() -> None:
    resolved = ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating",
            price=PriceConstraint.below(Decimal("5000"), SAR),
        ),
        semantics=ConstraintSemantics(price_max=APPROXIMATE),
    )

    planned = _plan(resolved)

    assert all(p.request.price is not None for p in planned)
    assert all(p.request.price.max_exclusive is True for p in planned)
    # The endpoint moves outward; excluding it does not stop being true.
    assert [p.request.price.max_amount for p in planned] == [
        Decimal("5500.00"),
        Decimal("6000.00"),
    ]


def test_an_exclusive_floor_survives_a_widened_floor() -> None:
    resolved = ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating",
            price=PriceConstraint.above(Decimal("5000"), SAR),
        ),
        semantics=ConstraintSemantics(price_min=APPROXIMATE),
    )

    planned = _plan(resolved)

    assert all(p.request.price.min_exclusive is True for p in planned)


# ── the widened fields still widen ──────────────────────────────────────────


def test_preservation_did_not_stop_the_widening() -> None:
    """A reconstruction that copied everything, including the bound, would
    pass every test above and relax nothing."""
    planned = _plan(_resolved())

    assert [p.request.price.max_amount for p in planned] == [
        Decimal("5500.00"),
        Decimal("6000.00"),
    ]


def test_capacity_and_dimensions_still_widen_together_with_price() -> None:
    resolved = ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sofa",
            seating_capacity=SeatingCapacityConstraint.exactly(3),
            dimensions=(
                DimensionConstraint(
                    role=DimensionRole.OVERALL_WIDTH,
                    kind=DimensionConstraintKind.MAX,
                    max_cm=Decimal("220"),
                ),
            ),
        ),
        semantics=ConstraintSemantics(
            seating_min=APPROXIMATE,
            seating_max=APPROXIMATE,
            dimensions=(
                DimensionConstraintSemantics(
                    role=DimensionRole.OVERALL_WIDTH, strength=APPROXIMATE
                ),
            ),
        ),
    )

    planned = _plan(resolved)
    widths = {
        c.max_cm
        for p in planned
        for c in p.request.dimensions
        if c.role is DimensionRole.OVERALL_WIDTH
    }

    assert any(p.request.seating_capacity != resolved.request.seating_capacity
               for p in planned)
    assert widths != {Decimal("220")}


@pytest.mark.parametrize("field", ["colors_any_of", "styles_all_of"])
def test_exact_attributes_are_never_relaxed(field: str) -> None:
    resolved = _resolved(colors_any_of=("Beige",), styles_all_of=("Modern",))
    original = getattr(resolved.request, field)

    assert all(getattr(p.request, field) == original for p in _plan(resolved))
