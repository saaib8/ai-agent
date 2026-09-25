"""Composing a seat count from several pieces - the seating-bundle planner.

The invariants that matter are the two the schema also guards, checked here from
the planner's side: the seats add up to at least the target, and the total never
exceeds the budget. Everything is summed by code from real products; no model
decides what exists, what it seats or what it costs.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from app.schemas.catalog_overview import CatalogOverview, SeatingSpread, SubcategoryShelf
from app.schemas.discovery import ProductSearchRequest, ProductSort
from app.schemas.product import CommerceClassification, ProductRow
from app.schemas.retailer import RetailerContext
from app.schemas.seating_solution import SeatingSolutionOutcome
from app.services.seating_solution import SeatingSolutionPlanner

CONTEXT = RetailerContext(store_id=50)


def _row(product_id: int, subcategory: str, capacity: int | None, price: str) -> ProductRow:
    from app.schemas.dimensions import RawDimensions

    return ProductRow(
        id=product_id,
        uuid=uuid4(),
        store_id=50,
        name_english=f"{subcategory} {product_id}",
        name_arabic="كنبة",
        price_amount=Decimal(price),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        visual_category=None,
        commerce=CommerceClassification(
            category="seating", subcategory=subcategory, seating_capacity=capacity
        ),
        dimensions=RawDimensions(unit="cm"),
        main_color="Beige",
        styles=("Modern",),
        is_active=True,
    )


def _shelf(
    subcategory: str, *, seats: tuple[int, int] | None, implied: int | None
) -> SubcategoryShelf:
    spread = None
    if seats is not None:
        spread = SeatingSpread(known_count=5, minimum=seats[0], maximum=seats[1])
    return SubcategoryShelf(
        commerce_category="seating",
        commerce_subcategory=subcategory,
        active_count=10,
        price_minimum=Decimal("400"),
        price_maximum=Decimal("8000"),
        seating=spread,
        implied_seats=implied,
        colours=(),
    )


class FakeCapability:
    def __init__(self, overview: CatalogOverview) -> None:
        self._overview = overview

    async def overview(self, context: RetailerContext) -> CatalogOverview:
        return self._overview


class FakeRepository:
    """A search that filters and sorts canned rows the way the real one does for
    the fields the planner uses: category, subcategory, price ceiling, an exact
    seat count, and a cheapest-first order."""

    def __init__(self, rows: list[ProductRow]) -> None:
        self.rows = rows

    async def search(
        self, request: ProductSearchRequest, context: RetailerContext, *, limit: int, **_: object
    ) -> list[ProductRow]:
        result = [r for r in self.rows if r.commerce.category == request.commerce_category]
        if request.commerce_subcategory is not None:
            result = [r for r in result if r.commerce.subcategory == request.commerce_subcategory]
        if request.price is not None and request.price.max_amount is not None:
            result = [r for r in result if r.price_amount <= request.price.max_amount]
        capacity = request.seating_capacity
        if capacity is not None and capacity.min_capacity is not None:
            result = [
                r
                for r in result
                if r.commerce.seating_capacity is not None
                and r.commerce.seating_capacity >= capacity.min_capacity
            ]
        if capacity is not None and capacity.max_capacity is not None:
            result = [
                r
                for r in result
                if r.commerce.seating_capacity is not None
                and r.commerce.seating_capacity <= capacity.max_capacity
            ]
        if request.sort is ProductSort.PRICE_ASC:
            result.sort(key=lambda r: (r.price_amount, r.id))
        return result[:limit]


def _planner(overview: CatalogOverview, rows: list[ProductRow]) -> SeatingSolutionPlanner:
    return SeatingSolutionPlanner(FakeCapability(overview), FakeRepository(rows))  # type: ignore[arg-type]


_OVERVIEW = CatalogOverview(
    store_id=50,
    currency="SAR",
    shelves=(
        _shelf("sofa", seats=(2, 5), implied=None),
        _shelf("sofa-set", seats=(5, 7), implied=None),
        _shelf("chair", seats=None, implied=1),
    ),
)

_STOCK = [
    _row(1, "sofa-set", 7, "4000"),
    _row(2, "sofa", 5, "2500"),
    _row(3, "chair", None, "400"),
    _row(4, "sofa", 2, "1200"),  # a smaller, cheaper sofa the anchor must not pick
]


async def test_it_composes_bundles_that_seat_the_target_within_budget() -> None:
    planner = _planner(_OVERVIEW, _STOCK)

    solution = await planner.plan(
        target_seats=8, budget_amount=Decimal("5000"), currency="SAR", context=CONTEXT
    )

    assert solution.outcome is SeatingSolutionOutcome.BUNDLES
    for bundle in solution.bundles:
        assert bundle.total_seats >= 8
        assert bundle.total_price <= Decimal("5000")
    # cheapest first: a 5-seat sofa + 3 chairs (3700) beats a 7-set + 1 chair (4400).
    cheapest = solution.bundles[0]
    assert cheapest.total_price == Decimal("3700")
    assert cheapest.total_seats == 8


async def test_the_anchor_is_the_highest_seating_piece_not_the_cheapest() -> None:
    """The 5-seat sofa (2,500) anchors, not the 2-seat sofa (1,200) - a tidy
    bundle carries its seats in the big piece, not a pile of chairs."""
    planner = _planner(_OVERVIEW, _STOCK)

    solution = await planner.plan(
        target_seats=8, budget_amount=Decimal("5000"), currency="SAR", context=CONTEXT
    )
    sofa_bundle = next(b for b in solution.bundles if b.lines[0].commerce_subcategory == "sofa")
    anchor = sofa_bundle.lines[0]

    assert anchor.product_id == 2
    assert anchor.seats_each == 5
    assert anchor.seats_are_confirmed is True


async def test_the_filler_seat_is_marked_not_confirmed() -> None:
    """A chair seats one by review, not by a recorded figure (CLAUDE.md 6.2)."""
    planner = _planner(_OVERVIEW, _STOCK)

    solution = await planner.plan(
        target_seats=8, budget_amount=Decimal("5000"), currency="SAR", context=CONTEXT
    )
    filler = solution.bundles[0].lines[1]

    assert filler.commerce_subcategory == "chair"
    assert filler.seats_each == 1
    assert filler.seats_are_confirmed is False
    assert filler.quantity == 3


async def test_a_single_piece_that_suffices_is_not_a_combination() -> None:
    planner = _planner(_OVERVIEW, _STOCK)

    solution = await planner.plan(
        target_seats=6, budget_amount=Decimal("5000"), currency="SAR", context=CONTEXT
    )

    assert solution.outcome is SeatingSolutionOutcome.SINGLE_PIECE_SUFFICES
    assert solution.bundles == ()


async def test_a_budget_too_low_for_any_combination_is_honest_not_empty() -> None:
    """The budget is the one thing recovery never breaks. When nothing fits, the
    outcome says so plainly rather than returning a silent empty list."""
    planner = _planner(_OVERVIEW, _STOCK)

    solution = await planner.plan(
        target_seats=8, budget_amount=Decimal("1000"), currency="SAR", context=CONTEXT
    )

    assert solution.outcome is SeatingSolutionOutcome.NONE_WITHIN_BUDGET
    assert solution.bundles == ()


async def test_a_store_with_no_seating_composes_nothing() -> None:
    empty = CatalogOverview(store_id=50, currency="SAR", shelves=())
    planner = _planner(empty, [])

    solution = await planner.plan(
        target_seats=8, budget_amount=Decimal("5000"), currency="SAR", context=CONTEXT
    )

    assert solution.outcome is SeatingSolutionOutcome.NO_SEATING


async def test_every_total_is_the_code_sum_of_real_lines() -> None:
    """The seats and the price a customer would be shown are computed here, from
    real products - never trusted from anywhere (CLAUDE.md 3.3)."""
    planner = _planner(_OVERVIEW, _STOCK)

    solution = await planner.plan(
        target_seats=8, budget_amount=Decimal("5000"), currency="SAR", context=CONTEXT
    )
    for bundle in solution.bundles:
        assert bundle.total_seats == sum(line.line_seats for line in bundle.lines)
        assert bundle.total_price == sum((line.line_total for line in bundle.lines), Decimal(0))
