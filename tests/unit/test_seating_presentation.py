"""A seating combination rendered through the room-bundle cards.

The one rule worth a test of its own: a reviewed accent seat must render with no
capacity, so a chair never shows a confirmed "1 seat" it does not have
(CLAUDE.md 6.2). The rest is that the planner's figures reach the card unchanged.
"""

from __future__ import annotations

from decimal import Decimal

from app.schemas.acquisition import BundleAcquisition
from app.schemas.bundle import BundleStatus
from app.schemas.seating_solution import (
    SeatingBundle,
    SeatingBundleLine,
    SeatingShape,
    SeatingSolution,
    SeatingSolutionOutcome,
)
from app.services.seating_presentation import present_seating_solution


def _anchor() -> SeatingBundleLine:
    return SeatingBundleLine(
        product_id=2,
        name="5 Seater Sofa",
        commerce_subcategory="sofa",
        unit_price=Decimal("2500"),
        quantity=1,
        seats_each=5,
        seats_are_confirmed=True,
        image_url="https://example.test/2.jpg",
        product_url="https://example.test/2",
    )


def _filler(quantity: int) -> SeatingBundleLine:
    return SeatingBundleLine(
        product_id=3,
        name="Accent Chair",
        commerce_subcategory="chair",
        unit_price=Decimal("400"),
        quantity=quantity,
        seats_each=1,
        seats_are_confirmed=False,
        image_url="https://example.test/3.jpg",
        product_url="https://example.test/3",
    )


def _solution() -> SeatingSolution:
    bundle = SeatingBundle(
        shape=SeatingShape.SOFA_WITH_EXTRA_SEATS,
        lines=(_anchor(), _filler(3)),
        total_seats=8,
        total_price=Decimal("3700"),
        currency="SAR",
    )
    return SeatingSolution(
        target_seats=8,
        budget_amount=Decimal("5000"),
        currency="SAR",
        outcome=SeatingSolutionOutcome.BUNDLES,
        bundles=(bundle,),
    )


def test_a_confirmed_anchor_shows_its_capacity_a_reviewed_filler_does_not() -> None:
    (rendered,) = present_seating_solution(_solution())

    anchor, filler = rendered.items
    assert anchor.commerce.seating_capacity == 5  # recorded on the sofa
    assert filler.commerce.seating_capacity is None  # reviewed, so not shown as confirmed


def test_the_figures_reach_the_card_unchanged() -> None:
    (rendered,) = present_seating_solution(_solution())

    anchor, filler = rendered.items
    assert rendered.status is BundleStatus.COMPLETE
    assert anchor.quantity == 1 and anchor.new_spend_line_total == Decimal("2500")
    assert filler.quantity == 3 and filler.new_spend_line_total == Decimal("1200")
    assert rendered.totals.new_spend_total == Decimal("3700")
    assert rendered.totals.currency == "SAR"
    assert rendered.totals.budget_max_amount == Decimal("5000")
    assert rendered.totals.within_budget is True
    for item in rendered.items:
        assert item.acquisition is BundleAcquisition.TO_BUY
        assert not item.locked


def test_refs_run_one_to_n_in_order() -> None:
    (rendered,) = present_seating_solution(_solution())
    assert tuple(item.grounding_ref for item in rendered.items) == (1, 2)


def test_a_non_bundle_outcome_renders_nothing() -> None:
    solution = SeatingSolution(
        target_seats=8,
        budget_amount=Decimal("5000"),
        currency="SAR",
        outcome=SeatingSolutionOutcome.NONE_WITHIN_BUDGET,
    )
    assert present_seating_solution(solution) == ()
