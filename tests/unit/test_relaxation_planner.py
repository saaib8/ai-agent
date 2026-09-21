"""Relaxation policy: what may widen, by how much, and in what order."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.core.config import RelaxationSettings
from app.schemas.discovery import (
    PriceConstraint,
    ProductSearchRequest,
    ProductSort,
    SeatingCapacityConstraint,
)
from app.schemas.query import ConstraintSemantics, ConstraintStrength, ResolvedSearch
from app.schemas.relaxation import RelaxableField, RelaxationChange
from app.services.relaxation import PlannedRelaxation, RelaxationPlanner

LOCKED = ConstraintStrength.LOCKED
PREFERRED = ConstraintStrength.PREFERRED
APPROXIMATE = ConstraintStrength.APPROXIMATE
SAR = "SAR"

PLANNER = RelaxationPlanner(RelaxationSettings())


def _resolved(
    *,
    price: PriceConstraint | None = None,
    capacity: SeatingCapacityConstraint | None = None,
    sort: ProductSort = ProductSort.DEFAULT,
    product_type: str | None = "sofa",
    **semantics: ConstraintStrength | None,
) -> ResolvedSearch:
    return ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory=product_type,
            price=price,
            seating_capacity=capacity,
            sort=sort,
        ),
        semantics=ConstraintSemantics(**semantics),
    )


def _max_prices(steps: tuple[PlannedRelaxation, ...]) -> list[Decimal | None]:
    return [s.request.price.max_amount if s.request.price else None for s in steps]


def _min_prices(steps: tuple[PlannedRelaxation, ...]) -> list[Decimal | None]:
    return [s.request.price.min_amount if s.request.price else None for s in steps]


def _seats(steps: tuple[PlannedRelaxation, ...]) -> list[tuple[int | None, int | None]]:
    return [
        (s.request.seating_capacity.min_capacity, s.request.seating_capacity.max_capacity)
        if s.request.seating_capacity
        else (None, None)
        for s in steps
    ]


# ── permission ──────────────────────────────────────────────────────────────


def test_locked_constraints_are_never_widened() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            capacity=SeatingCapacityConstraint.exactly(4),
            price_max=LOCKED,
            seating_min=LOCKED,
            seating_max=LOCKED,
        )
    )

    assert steps == ()


def test_missing_semantics_never_grant_permission() -> None:
    """Absent metadata is not consent (CLAUDE.md 13)."""
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            capacity=SeatingCapacityConstraint.exactly(4),
        )
    )

    assert steps == ()


def test_a_request_with_no_constraints_yields_no_steps() -> None:
    assert PLANNER.plan(_resolved()) == ()


def test_approximate_is_offered_before_preferred() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            capacity=SeatingCapacityConstraint.exactly(4),
            price_max=PREFERRED,
            seating_min=APPROXIMATE,
            seating_max=APPROXIMATE,
        )
    )

    first_fields = {c.field for c in steps[0].changes}
    assert first_fields == {RelaxableField.SEATING_MIN, RelaxableField.SEATING_MAX}
    assert steps[0].changes[0].strength is APPROXIMATE
    assert steps[1].changes[0].strength is PREFERRED


def test_within_one_strength_price_is_offered_before_seating() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            capacity=SeatingCapacityConstraint.exactly(4),
            price_max=APPROXIMATE,
            seating_min=APPROXIMATE,
            seating_max=APPROXIMATE,
        )
    )

    assert [c.field for c in steps[0].changes] == [RelaxableField.PRICE_MAX]
    assert [c.field for c in steps[1].changes] == [RelaxableField.PRICE_MAX]
    assert {c.field for c in steps[2].changes} == {
        RelaxableField.SEATING_MIN,
        RelaxableField.SEATING_MAX,
    }


# ── price ───────────────────────────────────────────────────────────────────


def test_an_upper_bound_widens_ten_then_twenty_percent() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=APPROXIMATE,
        )
    )

    assert _max_prices(steps) == [Decimal("5500.00"), Decimal("6000.00")]


def test_percentages_derive_from_the_original_and_never_compound() -> None:
    """5000 -> 5500 -> 6000. Compounding would give 6600."""
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=APPROXIMATE,
        )
    )

    assert steps[1].request.price is not None
    assert steps[1].request.price.max_amount == Decimal("6000.00")
    assert steps[1].request.price.max_amount != Decimal("6600.00")
    # The change records the running value it moved from, and the original-derived value to.
    moved = steps[1].changes[0]
    assert isinstance(moved, RelaxationChange)
    assert moved.from_value == Decimal("5500.00")
    assert moved.to_value == Decimal("6000.00")


def test_a_lower_bound_widens_downwards() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, min_amount=Decimal("5000")),
            price_min=APPROXIMATE,
        )
    )

    assert _min_prices(steps) == [Decimal("4500.00"), Decimal("4000.00")]


def test_a_lower_bound_never_goes_below_zero() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, min_amount=Decimal("0")),
            price_min=APPROXIMATE,
        )
    )

    assert steps == ()  # already at the floor: a no-op step is not emitted


def test_both_soft_bounds_widen_together() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(
                currency=SAR, min_amount=Decimal("3000"), max_amount=Decimal("5000")
            ),
            price_min=PREFERRED,
            price_max=PREFERRED,
        )
    )

    assert _min_prices(steps) == [Decimal("2700.00"), Decimal("2400.00")]
    assert _max_prices(steps) == [Decimal("5500.00"), Decimal("6000.00")]


def test_a_locked_bound_holds_while_the_soft_one_moves() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(
                currency=SAR, min_amount=Decimal("3000"), max_amount=Decimal("5000")
            ),
            price_min=LOCKED,
            price_max=PREFERRED,
        )
    )

    assert _min_prices(steps) == [Decimal("3000"), Decimal("3000")]
    assert _max_prices(steps) == [Decimal("5500.00"), Decimal("6000.00")]


def test_the_currency_never_changes() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=APPROXIMATE,
        )
    )

    assert all(s.request.price is not None and s.request.price.currency == SAR for s in steps)


def test_an_upper_bound_is_never_removed() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=APPROXIMATE,
        )
    )

    assert all(s.request.price is not None for s in steps)
    assert all(s.request.price.max_amount is not None for s in steps)  # type: ignore[union-attr]


def test_prices_stay_decimal() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("1999.99")),
            price_max=APPROXIMATE,
        )
    )

    for step in steps:
        assert step.request.price is not None
        assert isinstance(step.request.price.max_amount, Decimal)


# ── seating ─────────────────────────────────────────────────────────────────


def test_an_exact_capacity_widens_by_one_seat_each_way() -> None:
    steps = PLANNER.plan(
        _resolved(
            capacity=SeatingCapacityConstraint.exactly(4),
            seating_min=APPROXIMATE,
            seating_max=APPROXIMATE,
        )
    )

    assert _seats(steps) == [(3, 5)]


def test_a_minimum_capacity_drops_by_one() -> None:
    steps = PLANNER.plan(
        _resolved(
            capacity=SeatingCapacityConstraint.at_least(4), seating_min=PREFERRED
        )
    )

    assert _seats(steps) == [(3, None)]


def test_a_maximum_capacity_rises_by_one() -> None:
    steps = PLANNER.plan(
        _resolved(capacity=SeatingCapacityConstraint.at_most(4), seating_max=PREFERRED)
    )

    assert _seats(steps) == [(None, 5)]


def test_a_minimum_capacity_clamps_at_one_seat() -> None:
    steps = PLANNER.plan(
        _resolved(
            capacity=SeatingCapacityConstraint.at_least(1), seating_min=APPROXIMATE
        )
    )

    assert steps == ()  # already at the floor


def test_capacity_widens_by_exactly_one_seat_and_no_more() -> None:
    steps = PLANNER.plan(
        _resolved(
            capacity=SeatingCapacityConstraint.exactly(4),
            seating_min=APPROXIMATE,
            seating_max=APPROXIMATE,
        )
    )

    assert _seats(steps) == [(3, 5)]
    assert (2, 6) not in _seats(steps)


def test_the_capacity_filter_is_never_removed() -> None:
    """NULL capacity means unverified, not "any": dropping the filter would
    release products whose capacity nobody confirmed."""
    steps = PLANNER.plan(
        _resolved(
            capacity=SeatingCapacityConstraint.exactly(4),
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            seating_min=APPROXIMATE,
            seating_max=APPROXIMATE,
            price_max=PREFERRED,
        )
    )

    assert steps
    assert all(s.request.seating_capacity is not None for s in steps)


def test_locked_seating_never_moves() -> None:
    steps = PLANNER.plan(
        _resolved(
            capacity=SeatingCapacityConstraint.exactly(4),
            seating_min=LOCKED,
            seating_max=LOCKED,
        )
    )

    assert steps == ()


def test_mixed_seating_strengths_move_only_the_soft_side() -> None:
    steps = PLANNER.plan(
        _resolved(
            capacity=SeatingCapacityConstraint(min_capacity=3, max_capacity=5),
            seating_min=LOCKED,
            seating_max=PREFERRED,
        )
    )

    assert _seats(steps) == [(3, 6)]


# ── what must never change ──────────────────────────────────────────────────


def test_the_category_never_changes() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=APPROXIMATE,
        )
    )

    assert all(s.request.commerce_category == "seating" for s in steps)


@pytest.mark.parametrize("strength", [PREFERRED, APPROXIMATE])
def test_the_subcategory_never_changes_even_when_soft(
    strength: ConstraintStrength,
) -> None:
    """No sectional-sofa -> sofa, no widening to null, no sibling map."""
    steps = PLANNER.plan(
        _resolved(
            product_type="sectional-sofa",
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=APPROXIMATE,
            subcategory=strength,
        )
    )

    assert steps
    assert all(s.request.commerce_subcategory == "sectional-sofa" for s in steps)
    assert not any("subcategory" in str(c.field) for s in steps for c in s.changes)


def test_a_soft_subcategory_alone_yields_no_steps() -> None:
    assert PLANNER.plan(_resolved(subcategory=PREFERRED)) == ()


@pytest.mark.parametrize("sort", list(ProductSort))
def test_the_sort_never_changes(sort: ProductSort) -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=APPROXIMATE,
            sort=sort,
        )
    )

    assert all(s.request.sort is sort for s in steps)


def test_the_original_request_is_never_mutated() -> None:
    resolved = _resolved(
        price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
        capacity=SeatingCapacityConstraint.exactly(4),
        price_max=APPROXIMATE,
        seating_min=APPROXIMATE,
        seating_max=APPROXIMATE,
    )
    before = resolved.request.model_dump()

    PLANNER.plan(resolved)

    assert resolved.request.model_dump() == before
    assert resolved.request.price is not None
    assert resolved.request.price.max_amount == Decimal("5000")


def test_the_limit_is_carried_through_untouched() -> None:
    """Raising it for execution is the service's job, not the policy's."""
    resolved = ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating",
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            limit=2,
        ),
        semantics=ConstraintSemantics(price_max=APPROXIMATE),
    )

    assert all(s.request.limit == 2 for s in PLANNER.plan(resolved))


# ── boundedness and validity ────────────────────────────────────────────────


def test_every_derived_request_is_a_validated_contract() -> None:
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(
                currency=SAR, min_amount=Decimal("3000"), max_amount=Decimal("5000")
            ),
            capacity=SeatingCapacityConstraint(min_capacity=3, max_capacity=5),
            price_min=APPROXIMATE,
            price_max=APPROXIMATE,
            seating_min=PREFERRED,
            seating_max=PREFERRED,
        )
    )

    for step in steps:
        # Re-validating a valid model is a no-op; an invalid one would raise.
        ProductSearchRequest.model_validate(step.request.model_dump())
        assert step.request.price is not None
        assert step.request.price.min_amount <= step.request.price.max_amount  # type: ignore[operator]


def test_the_plan_is_bounded_at_six_steps() -> None:
    """Two price fractions and one seating step, per strength."""
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(
                currency=SAR, min_amount=Decimal("3000"), max_amount=Decimal("5000")
            ),
            capacity=SeatingCapacityConstraint(min_capacity=3, max_capacity=5),
            price_min=APPROXIMATE,
            price_max=PREFERRED,
            seating_min=APPROXIMATE,
            seating_max=PREFERRED,
        )
    )

    assert len(steps) == 6
    assert len(steps) <= 6


def test_no_op_steps_are_skipped() -> None:
    """A price constraint with no soft bound contributes no attempts."""
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            capacity=SeatingCapacityConstraint.exactly(4),
            price_max=LOCKED,
            seating_min=APPROXIMATE,
            seating_max=APPROXIMATE,
        )
    )

    assert len(steps) == 1
    assert {c.field for c in steps[0].changes} == {
        RelaxableField.SEATING_MIN,
        RelaxableField.SEATING_MAX,
    }


def test_relaxations_accumulate_across_steps() -> None:
    """Widening seats does not reset the price back to the original."""
    steps = PLANNER.plan(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            capacity=SeatingCapacityConstraint.exactly(4),
            price_max=APPROXIMATE,
            seating_min=PREFERRED,
            seating_max=PREFERRED,
        )
    )

    last = steps[-1]
    assert last.request.price is not None
    assert last.request.price.max_amount == Decimal("6000.00")
    assert _seats(steps)[-1] == (3, 5)
