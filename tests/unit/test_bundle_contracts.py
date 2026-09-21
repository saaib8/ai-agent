"""The whole-room contracts, and the three things they refuse to conflate.

A lock is preservation, not purchase. A physical unit is not a product. A total
is money still to spend. Each of those, collapsed, would let the optimiser
assert something nobody established, so each is defended here at construction
rather than in the service that builds them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.acquisition import BundleAcquisition
from app.schemas.bundle import (
    BundleLine,
    BundleStatus,
    BundleUnavailableReason,
    LockedBundleProduct,
    RoomBundle,
    TotalUnavailableReason,
    UnmetNeed,
    UnmetReason,
)
from app.schemas.design import DesignPriority
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.product import CommerceClassification, ProductCandidate
from pydantic import ValidationError


def product(
    product_id: int = 1, *, price: str = "1000.00", unit: str = "SAR"
) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Item {product_id}",
        name_arabic="منتج",
        price_amount=Decimal(price),
        price_unit=unit,
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


def _selected(**kwargs: object) -> BundleLine:
    defaults = {
        "need_index": 0,
        "product": product(),
        "quantity": 1,
        "locked": False,
        "acquisition": BundleAcquisition.TO_BUY,
        "relaxation_depth": 0,
    }
    return BundleLine(**{**defaults, **kwargs})


def _lock(**kwargs: object) -> BundleLine:
    defaults = {
        "need_index": 0,
        "product": product(),
        "quantity": 1,
        "locked": True,
        "acquisition": BundleAcquisition.TO_BUY,
    }
    return BundleLine(**{**defaults, **kwargs})


# ── acquisition is never inferred ───────────────────────────────────────────


def test_a_locked_product_must_state_its_acquisition() -> None:
    """A lock proves preservation and nothing about purchase, so there is no
    default to fall back on."""
    with pytest.raises(ValidationError):
        LockedBundleProduct(product=product())  # type: ignore[call-arg]


def test_acquisition_has_exactly_two_meanings() -> None:
    assert {a.value for a in BundleAcquisition} == {"to_buy", "already_owned"}


def test_a_locked_product_defaults_to_one_unit() -> None:
    lock = LockedBundleProduct(
        product=product(), acquisition=BundleAcquisition.ALREADY_OWNED
    )

    assert lock.quantity == 1


# ── provenance ──────────────────────────────────────────────────────────────


def test_a_lock_carries_no_search_provenance() -> None:
    """Depth zero would claim the lock satisfied a search that just ran."""
    with pytest.raises(ValidationError):
        _lock(relaxation_depth=0)


def test_a_selected_line_must_carry_its_provenance() -> None:
    with pytest.raises(ValidationError):
        _selected(relaxation_depth=None)


def test_a_newly_selected_product_is_always_bought() -> None:
    with pytest.raises(ValidationError):
        _selected(acquisition=BundleAcquisition.ALREADY_OWNED)


# ── money is derived, never stored twice ────────────────────────────────────


def test_line_money_reads_through_to_the_catalog_product() -> None:
    line = _selected(product=product(price="1250.50"), quantity=3)

    assert line.unit_price == Decimal("1250.50")
    assert line.line_total == Decimal("3751.50")
    assert line.price_unit == "SAR"


def test_no_monetary_value_is_stored_on_a_line() -> None:
    """One price, in one place. A stored copy could disagree with the catalog."""
    for absent in ("unit_price", "line_total", "price_unit"):
        assert absent not in BundleLine.model_fields


def test_line_totals_stay_decimal() -> None:
    assert isinstance(_selected(quantity=7).line_total, Decimal)


# ── a partial room cannot be called complete ────────────────────────────────


def _unmet(priority: DesignPriority = DesignPriority.REQUIRED) -> UnmetNeed:
    return UnmetNeed(
        need_index=1,
        priority=priority,
        shortfall=1,
        reason=UnmetReason.BUDGET_EXHAUSTED,
    )


def _bundle(**kwargs: object) -> RoomBundle:
    defaults = {
        "lines": (_selected(),),
        "status": BundleStatus.COMPLETE,
        "new_spend_total": Decimal("1000.00"),
        "currency": "SAR",
    }
    return RoomBundle(**{**defaults, **kwargs})


def test_a_bundle_missing_a_required_need_cannot_be_complete() -> None:
    with pytest.raises(ValidationError):
        _bundle(status=BundleStatus.COMPLETE, unmet=(_unmet(),))


def test_a_partial_bundle_must_actually_be_short_of_something_required() -> None:
    with pytest.raises(ValidationError):
        _bundle(status=BundleStatus.PARTIAL, unmet=())


def test_recommended_and_optional_gaps_do_not_prevent_completeness() -> None:
    """A room works when the pieces it cannot do without are there."""
    bundle = _bundle(
        status=BundleStatus.COMPLETE,
        unmet=(_unmet(DesignPriority.RECOMMENDED), _unmet(DesignPriority.OPTIONAL)),
    )

    assert bundle.status is BundleStatus.COMPLETE
    assert len(bundle.unmet) == 2


def test_an_infeasible_bundle_selected_nothing_new() -> None:
    with pytest.raises(ValidationError):
        _bundle(status=BundleStatus.INFEASIBLE, unmet=(_unmet(),))


def test_an_infeasible_bundle_still_returns_its_locks() -> None:
    bundle = _bundle(
        lines=(_lock(),), status=BundleStatus.INFEASIBLE, unmet=(_unmet(),)
    )

    assert bundle.lines[0].locked is True


def test_a_need_receives_at_most_one_newly_selected_product() -> None:
    """The residual is filled completely or not at all, never twice."""
    with pytest.raises(ValidationError):
        _bundle(lines=(_selected(need_index=0), _selected(need_index=0)))


def test_two_needs_may_select_the_same_product() -> None:
    """Distinct needs, distinct lines, one SKU. Forbidding this would couple
    needs and invalidate the optimiser's independence."""
    bundle = _bundle(
        lines=(
            _selected(need_index=0, product=product(5)),
            _selected(need_index=1, product=product(5)),
        ),
        new_spend_total=Decimal("2000.00"),
    )

    assert {line.product.product_id for line in bundle.lines} == {5}
    assert len(bundle.lines) == 2


# ── the total and its unit travel together ──────────────────────────────────


def test_a_total_needs_a_currency() -> None:
    with pytest.raises(ValidationError):
        _bundle(new_spend_total=Decimal("100"), currency=None)


def test_a_total_is_either_available_or_explained() -> None:
    with pytest.raises(ValidationError):
        _bundle(
            new_spend_total=Decimal("100"),
            currency="SAR",
            total_unavailable=TotalUnavailableReason.MIXED_PRICE_UNITS,
        )

    with pytest.raises(ValidationError):
        _bundle(new_spend_total=None, currency=None, total_unavailable=None)


def test_the_total_is_named_for_what_it_means() -> None:
    """`total` would read as the room's value while meaning money still owed."""
    assert "new_spend_total" in RoomBundle.model_fields
    assert "total" not in RoomBundle.model_fields


# ── the unavailable vocabulary ──────────────────────────────────────────────


def test_lock_hydration_failure_is_not_an_optimiser_outcome() -> None:
    """The optimiser receives verified products and cannot tell a lock that
    failed to hydrate from one nobody passed. That is the caller's check."""
    assert "LOCKED_PRODUCT_UNAVAILABLE" not in {
        reason.name for reason in BundleUnavailableReason
    }


def test_every_unavailable_reason_means_the_arithmetic_is_impossible() -> None:
    assert {reason.value for reason in BundleUnavailableReason} == {
        "unsupported_budget_form",
        "budget_not_comparable",
        "locked_price_unusable",
    }


def test_unmet_reasons_stay_distinguishable() -> None:
    """Four different things to tell a customer, not one shrug."""
    assert {reason.value for reason in UnmetReason} == {
        "no_candidates",
        "no_usable_price",
        "not_budget_comparable",
        "budget_exhausted",
    }


def test_an_unmet_need_is_actually_short() -> None:
    with pytest.raises(ValidationError):
        UnmetNeed(
            need_index=0,
            priority=DesignPriority.REQUIRED,
            shortfall=0,
            reason=UnmetReason.NO_CANDIDATES,
        )
