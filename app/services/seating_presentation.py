"""A seating combination as the customer sees it.

The planner produces validated bundles; this renders each one through the same
customer-facing shape a whole room uses (:class:`GroundedBundlePresentation`),
so the existing bundle cards, prices and totals draw a seating combination with
no new presentation to build.

The same split holds as everywhere: the figures here are the ones the planner
computed from real products, and none of them passes through the response model
(CLAUDE.md 20.4). One honesty rule is load-bearing - an accent piece whose seat
count is only *reviewed*, not recorded, is rendered with **no** capacity on its
card, so a chair never shows a confirmed "1 seat" it does not have
(CLAUDE.md 6.2, 31).
"""

from __future__ import annotations

from app.schemas.acquisition import BundleAcquisition
from app.schemas.bundle import BundleStatus
from app.schemas.bundle_presentation import (
    GroundedBundleItem,
    GroundedBundlePresentation,
    GroundedBundleTotals,
)
from app.schemas.product import CommerceClassification
from app.schemas.seating_solution import SeatingBundle, SeatingSolution


def present_seating_solution(solution: SeatingSolution) -> tuple[GroundedBundlePresentation, ...]:
    """Each proposed bundle as a rendered package, cheapest first (the planner's
    order). Empty for any outcome other than one that carries bundles."""
    return tuple(_present_bundle(bundle, solution) for bundle in solution.bundles)


def _present_bundle(bundle: SeatingBundle, solution: SeatingSolution) -> GroundedBundlePresentation:
    items = tuple(
        GroundedBundleItem(
            grounding_ref=position,
            name_english=line.name,
            image_url=line.image_url,
            product_url=line.product_url,
            commerce=CommerceClassification(
                category="seating",
                subcategory=line.commerce_subcategory,
                # A reviewed default is never rendered as a recorded capacity:
                # a chair's card shows no seat count, a sofa's shows its own.
                seating_capacity=line.seats_each if line.seats_are_confirmed else None,
            ),
            quantity=line.quantity,
            acquisition=BundleAcquisition.TO_BUY,
            locked=False,
            unit_price=line.unit_price,
            price_unit=bundle.currency,
            new_spend_line_total=line.line_total,
        )
        for position, line in enumerate(bundle.lines, start=1)
    )
    totals = GroundedBundleTotals(
        new_spend_total=bundle.total_price,
        currency=bundle.currency,
        budget_max_amount=solution.budget_amount,
        budget_currency=solution.currency,
        budget_max_exclusive=False,
        # The planner never returns a bundle over budget, so a rendered one is
        # within it by construction.
        within_budget=True,
    )
    return GroundedBundlePresentation(status=BundleStatus.COMPLETE, items=items, totals=totals)
