"""Composing a seating requirement no single product can meet.

"A sofa for eight" where the largest sofa seats five is not a dead end - it is a
combination: a big sofa and a few chairs that together seat eight, within the
budget. This planner is the salesperson move made deterministic.

It owns every judgement that must be a fact rather than a guess (CLAUDE.md 3.3):

* what the store stocks and how high each type seats - from the catalog overview;
* which real products fill each piece - from the store-scoped search;
* whether the seats add up and the budget holds - summed here, in code, never
  trusted from a model.

The shape of a bundle is a large anchor - the highest-seating sofa, set or
sectional the store has within budget - plus enough one-seat accent pieces
(chairs, single-seaters) to reach the target. The seat count is *met*, not
relaxed: the requirement is satisfied by adding pieces, never by lowering it
(CLAUDE.md 13.4). The budget is the one thing a bundle may never break.

No model is called. This is the ``build_combination`` tool the agent loop will
reach for, usable on its own today.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.logging import get_logger
from app.repositories.products import ProductRepository
from app.schemas.catalog_overview import CatalogOverview, SubcategoryShelf
from app.schemas.discovery import (
    PriceConstraint,
    ProductSearchRequest,
    ProductSort,
    SeatingCapacityConstraint,
)
from app.schemas.retailer import RetailerContext
from app.schemas.seating_solution import (
    SeatingBundle,
    SeatingBundleLine,
    SeatingSolution,
    SeatingSolutionOutcome,
)
from app.services.catalog_capability import CatalogCapabilityService

logger = get_logger(__name__)

SEATING_CATEGORY = "seating"

MAX_BUNDLES = 3
"""How many alternative combinations to offer. Enough for a real choice, few
enough to stay a proposal rather than a catalogue."""

FILLER_SCAN_LIMIT = 40
"""How many of the cheapest seating rows to scan for an accent piece. The
filler is the cheapest one-seat type, so it surfaces near the top; a bounded
scan keeps the lookup one query."""


class SeatingSolutionPlanner:
    """Meet a seat count with several pieces when no single one can."""

    def __init__(self, capability: CatalogCapabilityService, repository: ProductRepository) -> None:
        self._capability = capability
        self._repository = repository

    async def plan(
        self,
        *,
        target_seats: int,
        budget_amount: Decimal | None,
        currency: str,
        context: RetailerContext,
    ) -> SeatingSolution:
        """Combinations that seat ``target_seats`` within ``budget_amount``.

        Returns an outcome for every case a caller must tell apart: a single
        piece already suffices, the store has no seating, combinations exist but
        none fits the budget, or here are the ones that do. Never an error and
        never a silent empty - each is a different thing to say.
        """
        overview = await self._capability.overview(context)
        base = {
            "target_seats": target_seats,
            "budget_amount": budget_amount,
            "currency": currency,
        }

        ceiling = overview.max_seats_in(SEATING_CATEGORY)
        if ceiling is None:
            return SeatingSolution(**base, outcome=SeatingSolutionOutcome.NO_SEATING)
        if ceiling >= target_seats:
            return SeatingSolution(**base, outcome=SeatingSolutionOutcome.SINGLE_PIECE_SUFFICES)

        filler = await self._cheapest_filler(overview, budget_amount, currency, context)
        anchors = [
            shelf
            for shelf in overview.shelves_in(SEATING_CATEGORY)
            if shelf.seating is not None and shelf.commerce_subcategory is not None
        ]
        anchors.sort(key=lambda shelf: shelf.max_seats or 0, reverse=True)

        bundles: list[SeatingBundle] = []
        if filler is not None:
            for shelf in anchors:
                bundle = await self._compose(
                    shelf, filler, target_seats, budget_amount, currency, context
                )
                if bundle is not None:
                    bundles.append(bundle)
                if len(bundles) >= MAX_BUNDLES:
                    break

        bundles.sort(key=lambda bundle: bundle.total_price)
        outcome = (
            SeatingSolutionOutcome.BUNDLES if bundles else SeatingSolutionOutcome.NONE_WITHIN_BUDGET
        )
        logger.info(
            "seating_solution_planned",
            store_id=context.store_id,
            target_seats=target_seats,
            single_piece_ceiling=ceiling,
            bundle_count=len(bundles),
            outcome=str(outcome),
        )
        return SeatingSolution(**base, outcome=outcome, bundles=tuple(bundles))

    async def _compose(
        self,
        shelf: SubcategoryShelf,
        filler: SeatingBundleLine,
        target_seats: int,
        budget: Decimal | None,
        currency: str,
        context: RetailerContext,
    ) -> SeatingBundle | None:
        """One anchor type worked into a bundle, or ``None`` if it cannot fit.

        The anchor is the highest-seating product of the type within budget; the
        rest of the target is made up of accent pieces. A type whose best anchor
        plus the fillers it needs overruns the budget yields no bundle - the
        budget is not negotiable.
        """
        anchor_seats = shelf.max_seats
        if anchor_seats is None or anchor_seats >= target_seats:
            return None
        subcategory = shelf.commerce_subcategory
        assert subcategory is not None  # filtered by the caller

        anchor = await self._best_anchor(subcategory, anchor_seats, budget, currency, context)
        if anchor is None:
            return None

        needed = target_seats - anchor_seats
        total = anchor.unit_price + needed * filler.unit_price
        if budget is not None and total > budget:
            return None

        fillers = filler.model_copy(update={"quantity": needed})
        return SeatingBundle(
            lines=(anchor, fillers),
            total_seats=anchor.line_seats + fillers.line_seats,
            total_price=total,
            currency=currency,
        )

    async def _best_anchor(
        self,
        subcategory: str,
        seats: int,
        budget: Decimal | None,
        currency: str,
        context: RetailerContext,
    ) -> SeatingBundleLine | None:
        """The cheapest product of a type that seats the most it can, or ``None``.

        Constrained to the type's confirmed ceiling so the anchor carries the
        most seats it can - which is what keeps the accent count small and the
        bundle tidy.
        """
        request = ProductSearchRequest(
            commerce_category=SEATING_CATEGORY,
            commerce_subcategory=subcategory,
            price=PriceConstraint.at_most(budget, currency) if budget is not None else None,
            seating_capacity=SeatingCapacityConstraint.exactly(seats),
            sort=ProductSort.PRICE_ASC,
        )
        rows = await self._repository.search(request, context, limit=1)
        if not rows:
            return None
        row = rows[0]
        return SeatingBundleLine(
            product_id=row.id,
            name=row.name_english,
            commerce_subcategory=subcategory,
            unit_price=row.price_amount,
            quantity=1,
            seats_each=seats,
            seats_are_confirmed=True,
            image_url=row.image_url,
            product_url=row.product_url,
        )

    async def _cheapest_filler(
        self,
        overview: CatalogOverview,
        budget: Decimal | None,
        currency: str,
        context: RetailerContext,
    ) -> SeatingBundleLine | None:
        """The cheapest one-seat accent piece the store stocks, or ``None``.

        A single query for the cheapest seating within budget, taking the first
        row whose type is a reviewed one-seater. Its seat is marked *not*
        confirmed: a chair seats one by review, and a reply must not present that
        as a figure the catalogue recorded (CLAUDE.md 6.2).
        """
        filler_types = {
            shelf.commerce_subcategory
            for shelf in overview.shelves_in(SEATING_CATEGORY)
            if shelf.implied_seats == 1 and shelf.commerce_subcategory is not None
        }
        if not filler_types:
            return None

        request = ProductSearchRequest(
            commerce_category=SEATING_CATEGORY,
            price=PriceConstraint.at_most(budget, currency) if budget is not None else None,
            sort=ProductSort.PRICE_ASC,
        )
        rows = await self._repository.search(request, context, limit=FILLER_SCAN_LIMIT)
        for row in rows:
            subcategory = row.commerce.subcategory
            if subcategory is not None and subcategory in filler_types:
                return SeatingBundleLine(
                    product_id=row.id,
                    name=row.name_english,
                    commerce_subcategory=subcategory,
                    unit_price=row.price_amount,
                    quantity=1,
                    seats_each=1,
                    seats_are_confirmed=False,
                    image_url=row.image_url,
                    product_url=row.product_url,
                )
        return None
