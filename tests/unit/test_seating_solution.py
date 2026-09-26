"""Composing a seat count from several pieces - the seating-combination planner.

Every real combination is worked out: two or three sofas together, or sofas
with a few armchairs. The invariants the schema also guards are checked from
the planner's side - the seats add up, the total never exceeds the budget - and
every piece is held to the rest of the request. No model decides what exists,
what it seats or what it costs.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from app.schemas.catalog_overview import CatalogOverview, SeatingSpread, SubcategoryShelf
from app.schemas.dimensions import RawDimensions
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    ProductSearchRequest,
)
from app.schemas.product import (
    CommerceClassification,
    EligibleProduct,
    ProductCandidate,
    ProductRow,
)
from app.schemas.retailer import RetailerContext
from app.schemas.seating_solution import (
    SeatingBundle,
    SeatingRequirements,
    SeatingShape,
    SeatingSolution,
    SeatingSolutionOutcome,
)
from app.services.discovery import to_candidate
from app.services.seating_solution import SeatingSolutionPlanner
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.dimensions import DimensionRole
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.seating import load_seating_semantics

CONTEXT = RetailerContext(store_id=50)


def _row(
    product_id: int,
    subcategory: str,
    capacity: int | None,
    price: str,
    *,
    colour: str = "Beige",
    styles: tuple[str, ...] = ("Modern",),
) -> ProductRow:
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
        main_color=colour,
        styles=styles,
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


class FakeDiscovery:
    """The eligible pool, filtered the way discovery does for the fields the
    planner uses: type, price ceiling, strict colours and styles, and a width
    limit read off the row's length."""

    def __init__(self, rows: list[ProductRow]) -> None:
        self.rows = rows
        self.requests: list[ProductSearchRequest] = []

    async def eligible_pool(
        self, request: ProductSearchRequest, context: RetailerContext
    ) -> tuple[EligibleProduct, ...]:
        self.requests.append(request)
        return tuple(
            EligibleProduct(
                product_id=r.id,
                price_amount=r.price_amount,
                main_color=r.main_color,
                styles=r.styles,
                seating_capacity=r.commerce.seating_capacity,
            )
            for r in self.rows
            if _eligible(r, request)
        )


def _eligible(row: ProductRow, request: ProductSearchRequest) -> bool:
    if row.commerce.category != request.commerce_category:
        return False
    if request.commerce_subcategory not in (None, row.commerce.subcategory):
        return False
    price = request.price
    if price is not None and price.max_amount is not None and row.price_amount > price.max_amount:
        return False
    if request.colors_any_of and row.main_color not in request.colors_any_of:
        return False
    if any(style not in row.styles for style in request.styles_all_of):
        return False
    width = row.dimensions.length
    return all(
        c.max_cm is None or (width is not None and width <= c.max_cm) for c in request.dimensions
    )


class FakeHydration:
    def __init__(self, rows: list[ProductRow]) -> None:
        self.rows = {r.id: r for r in rows}

    async def hydrate_ids(
        self, product_ids: list[int], context: RetailerContext
    ) -> tuple[ProductCandidate, ...]:
        return tuple(to_candidate(self.rows[i]) for i in product_ids if i in self.rows)


SEATING = load_seating_semantics(taxonomy=load_taxonomy())

_OVERVIEW = CatalogOverview(
    store_id=50,
    currency="SAR",
    shelves=(
        _shelf("sofa", seats=(2, 3), implied=None),
        _shelf("sofa-set", seats=(6, 7), implied=None),
        _shelf("sofa-bed", seats=(3, 3), implied=None),
        _shelf("chaise-lounge", seats=(3, 3), implied=None),
        _shelf("chair", seats=None, implied=1),
        _shelf("single-seater-sofa", seats=None, implied=1),
        _shelf("office-chair", seats=None, implied=1),
    ),
)

_STOCK = [
    _row(1, "sofa-set", 6, "2450"),
    _row(2, "sofa-set", 7, "4450", colour="Light Grey"),
    _row(3, "sofa", 2, "990"),
    _row(4, "sofa", 3, "1250"),
    _row(5, "sofa-bed", 3, "1650"),
    _row(6, "chair", None, "420", colour="Denim Blue"),
    _row(7, "single-seater-sofa", None, "650"),
    # Never part of a combination: an office chair belongs in another room,
    # and a chaise's seat count is not settled by review.
    _row(8, "office-chair", None, "200"),
    _row(9, "chaise-lounge", 3, "900"),
    # A sofa with no recorded capacity never fills a seat.
    _row(10, "sofa", None, "100"),
]


async def _plan(
    rows: list[ProductRow] = _STOCK,
    requirements: SeatingRequirements | None = None,
    *,
    seats: int = 8,
    budget: str | None = "6000",
    shape: SeatingShape | None = None,
    overview: CatalogOverview = _OVERVIEW,
) -> SeatingSolution:
    planner = SeatingSolutionPlanner(
        FakeCapability(overview),  # type: ignore[arg-type]
        FakeDiscovery(rows),  # type: ignore[arg-type]
        FakeHydration(rows),  # type: ignore[arg-type]
        SEATING,
    )
    return await planner.plan(
        target_seats=seats,
        budget_amount=Decimal(budget) if budget else None,
        currency="SAR",
        context=CONTEXT,
        requirements=requirements,
        shape=shape,
    )


def _ids(solution: SeatingSolution) -> list[list[int]]:
    return [sorted(line.product_id for line in bundle.lines) for bundle in solution.bundles]


# ── the combinations ────────────────────────────────────────────────────────


async def test_every_combination_seats_exactly_the_target_within_budget() -> None:
    solution = await _plan()

    assert solution.outcome is SeatingSolutionOutcome.BUNDLES
    for bundle in solution.bundles:
        assert bundle.total_seats == 8
        assert bundle.total_price <= Decimal("6000")
        assert bundle.total_seats == sum(line.line_seats for line in bundle.lines)
        assert bundle.total_price == sum((line.line_total for line in bundle.lines), Decimal(0))


async def test_two_sofas_together_are_offered() -> None:
    """Asked for live: "why not this sofa plus another sofa?"."""
    solution = await _plan(shape=SeatingShape.SEPARATE_SOFAS)

    assert solution.bundles
    for bundle in solution.bundles:
        assert bundle.shape is SeatingShape.SEPARATE_SOFAS
        assert all(line.seats_are_confirmed for line in bundle.lines)
    # The six-seat set with a two-seater, the cheapest real pairing.
    assert [1, 3] in _ids(solution)


async def test_every_shape_the_store_can_build_is_listed_with_its_lowest_total() -> None:
    solution = await _plan()

    options = {option.shape: option for option in solution.options}
    assert set(options) == {SeatingShape.SEPARATE_SOFAS, SeatingShape.SOFA_WITH_EXTRA_SEATS}
    # 6-seat set + 2-seater.
    assert options[SeatingShape.SEPARATE_SOFAS].from_price == Decimal("3440")
    # 6-seat set + two of the Denim Blue chair (2,450 + 2 x 420).
    assert options[SeatingShape.SOFA_WITH_EXTRA_SEATS].from_price == Decimal("3290")
    assert [o.from_price for o in solution.options] == sorted(
        o.from_price for o in solution.options
    )


async def test_without_a_shape_each_shape_is_shown() -> None:
    solution = await _plan()

    assert {bundle.shape for bundle in solution.bundles} == {
        SeatingShape.SEPARATE_SOFAS,
        SeatingShape.SOFA_WITH_EXTRA_SEATS,
    }
    assert len(solution.bundles) <= 3


async def test_a_chosen_shape_shows_only_that_shape() -> None:
    solution = await _plan(shape=SeatingShape.SOFA_WITH_EXTRA_SEATS)

    assert {bundle.shape for bundle in solution.bundles} == {SeatingShape.SOFA_WITH_EXTRA_SEATS}


async def test_only_reviewed_types_are_used() -> None:
    """No office chair, however cheap, and no chaise until its seats are
    reviewed. A sofa with no recorded capacity never fills a seat."""
    solution = await _plan(budget=None)

    used = {line.product_id for bundle in solution.bundles for line in bundle.lines}
    assert used.isdisjoint({8, 9, 10})


async def test_an_extra_seat_is_reviewed_not_recorded() -> None:
    solution = await _plan(shape=SeatingShape.SOFA_WITH_EXTRA_SEATS)

    for bundle in solution.bundles:
        for line in bundle.lines:
            large = line.commerce_subcategory in {"sofa", "sofa-set", "sofa-bed"}
            assert line.seats_are_confirmed is large


async def test_combinations_stay_small() -> None:
    solution = await _plan(budget=None)

    for bundle in solution.bundles:
        large = sum(line.quantity for line in bundle.lines if line.seats_are_confirmed)
        extras = sum(line.quantity for line in bundle.lines if not line.seats_are_confirmed)
        assert large <= 3
        assert extras <= 3


async def test_never_more_than_three_extra_seats() -> None:
    """Seven from two-seaters and chairs: two sofas and three chairs is the only
    combination - one sofa and five chairs is a queue, not a living room."""
    rows = [_row(70, "sofa", 2, "900"), _row(71, "chair", None, "300")]
    overview = CatalogOverview(
        store_id=50,
        currency="SAR",
        shelves=(
            _shelf("sofa", seats=(2, 2), implied=None),
            _shelf("chair", seats=None, implied=1),
        ),
    )

    solution = await _plan(rows, seats=7, budget=None, overview=overview)

    assert sum(option.combination_count for option in solution.options) == 1
    assert [(line.product_id, line.quantity) for line in solution.bundles[0].lines] == [
        (70, 2),
        (71, 3),
    ]


async def test_a_spare_seat_only_when_nothing_seats_it_exactly() -> None:
    """Nine seats from a six and a three-seater - exact, so no spare seat."""
    exact = await _plan(seats=9)
    assert all(bundle.total_seats == 9 for bundle in exact.bundles)

    only_threes = [_row(20, "sofa", 3, "1000"), _row(21, "sofa-set", 6, "2000")]
    overview = CatalogOverview(
        store_id=50,
        currency="SAR",
        shelves=(
            _shelf("sofa", seats=(3, 3), implied=None),
            _shelf("sofa-set", seats=(6, 6), implied=None),
        ),
    )
    spare = await _plan(only_threes, seats=8, overview=overview)
    assert spare.bundles and all(bundle.total_seats == 9 for bundle in spare.bundles)


async def test_a_single_piece_that_suffices_is_not_a_combination() -> None:
    solution = await _plan(seats=6)

    assert solution.outcome is SeatingSolutionOutcome.SINGLE_PIECE_SUFFICES
    assert solution.bundles == ()


async def test_a_budget_too_low_for_any_combination_is_honest_not_empty() -> None:
    solution = await _plan(budget="1000")

    assert solution.outcome is SeatingSolutionOutcome.NONE_WITHIN_BUDGET
    assert solution.bundles == ()
    assert solution.options == ()


async def test_a_store_with_no_seating_composes_nothing() -> None:
    empty = CatalogOverview(store_id=50, currency="SAR", shelves=())

    solution = await _plan([], overview=empty)

    assert solution.outcome is SeatingSolutionOutcome.NO_SEATING


# ── every piece is held to the rest of the request ──────────────────────────

_MIXED = [
    _row(10, "sofa-set", 7, "4000", colour="Light Grey"),
    _row(11, "sofa-set", 7, "4600", colour="Beige"),
    _row(12, "sofa", 5, "2500", colour="Light Grey"),
    _row(20, "chair", None, "300", colour="Denim Blue"),
    _row(21, "chair", None, "450", colour="Beige"),
]

_MIXED_OVERVIEW = CatalogOverview(
    store_id=50,
    currency="SAR",
    shelves=(
        _shelf("sofa", seats=(5, 5), implied=None),
        _shelf("sofa-set", seats=(7, 7), implied=None),
        _shelf("chair", seats=None, implied=1),
    ),
)


async def _mixed(
    requirements: SeatingRequirements, rows: list[ProductRow] = _MIXED, budget: str | None = "6000"
) -> SeatingSolution:
    return await _plan(rows, requirements, budget=budget, overview=_MIXED_OVERVIEW)


async def test_a_strict_colour_holds_for_every_piece() -> None:
    """ "Only beige" once came back grey and blue while the reply said beige."""
    solution = await _mixed(SeatingRequirements(colors_any_of=("Beige",)))

    assert solution.outcome is SeatingSolutionOutcome.BUNDLES
    for bundle in solution.bundles:
        assert bundle.lifted == ()
        assert {line.product_id for line in bundle.lines} <= {11, 21}


async def test_a_strict_colour_is_lifted_only_when_no_combination_meets_it() -> None:
    no_beige_set = [r for r in _MIXED if r.id != 11]

    solution = await _mixed(SeatingRequirements(colors_any_of=("Beige",)), no_beige_set)

    assert solution.outcome is SeatingSolutionOutcome.BUNDLES
    assert all(bundle.lifted == (AttributeFamily.COLOR,) for bundle in solution.bundles)


async def test_a_combination_that_meets_it_hides_the_ones_that_would_not() -> None:
    """A requirement some combination meets is never lifted (CLAUDE.md 12.4)."""
    solution = await _mixed(SeatingRequirements(colors_any_of=("Beige",)))

    assert {bundle.lines[0].product_id for bundle in solution.bundles} == {11}


async def test_a_colour_no_approved_value_expresses_is_lifted_from_the_start() -> None:
    solution = await _mixed(SeatingRequirements(unmatchable_strict=(AttributeFamily.COLOR,)))

    assert solution.outcome is SeatingSolutionOutcome.BUNDLES
    assert all(bundle.lifted == (AttributeFamily.COLOR,) for bundle in solution.bundles)


async def test_a_strict_style_holds_for_every_piece() -> None:
    rows = [
        _row(30, "sofa-set", 7, "4000", styles=("Classic",)),
        _row(31, "sofa-set", 7, "4500", styles=("Modern", "Minimalist")),
        _row(32, "chair", None, "300", styles=("Classic",)),
        _row(33, "chair", None, "400", styles=("Modern",)),
    ]

    solution = await _mixed(SeatingRequirements(styles_all_of=("Modern",)), rows)

    assert _ids(solution) == [[31, 33]]
    assert solution.bundles[0].lifted == ()


async def test_wishes_choose_the_pieces_without_filtering_anything() -> None:
    solution = await _mixed(SeatingRequirements(wished_colors=("Beige",)), budget=None)

    assert _ids(solution)[0] == [11, 21]
    assert all(line.matches_wish for line in solution.bundles[0].lines)
    # Nothing was filtered: the grey pieces still appear elsewhere.
    assert any(12 in ids or 10 in ids for ids in _ids(solution))


async def test_a_wished_colour_beats_a_tidier_combination() -> None:
    """For a beige request, one more beige armchair is better than two pieces
    in the wrong colour - live, a Steel Blue set once came first."""
    rows = [
        _row(40, "sofa-set", 7, "3000", colour="Steel Blue"),
        _row(41, "sofa-set", 6, "3000", colour="Beige"),
        _row(42, "chair", None, "300", colour="Denim Blue"),
        _row(43, "chair", None, "400", colour="Beige"),
    ]
    overview = CatalogOverview(
        store_id=50,
        currency="SAR",
        shelves=(
            _shelf("sofa-set", seats=(6, 7), implied=None),
            _shelf("chair", seats=None, implied=1),
        ),
    )

    solution = await _plan(
        rows, SeatingRequirements(wished_colors=("Beige",)), budget=None, overview=overview
    )

    first = solution.bundles[0]
    assert {line.product_id for line in first.lines} == {41, 43}
    assert first.total_seats == 8


async def test_a_piece_meeting_every_wish_beats_one_meeting_some() -> None:
    rows = [
        _row(50, "sofa-set", 7, "4000", colour="Steel Blue", styles=("Modern",)),
        _row(51, "sofa-set", 7, "7000", colour="Beige", styles=("Modern",)),
        _row(52, "chair", None, "300", colour="Denim Blue", styles=("Modern",)),
        _row(53, "chair", None, "600", colour="Beige", styles=("Modern",)),
    ]

    solution = await _mixed(
        SeatingRequirements(wished_colors=("Beige",), wished_styles=("Modern",)),
        rows,
        budget=None,
    )

    assert _ids(solution)[0] == [51, 53]


async def test_a_wish_never_breaks_the_budget() -> None:
    """The beige set and chair (4,600 + 450) would overrun 4,950. The
    cheaper grey set fits, and still with the beige chair beside it - the most
    of their wish the budget allows."""
    solution = await _mixed(SeatingRequirements(wished_colors=("Beige",)), budget="4950")

    assert all(bundle.total_price <= Decimal("4950") for bundle in solution.bundles)
    assert [10, 21] in _ids(solution)
    assert [11, 21] not in _ids(solution)


def _wide(product_id: int, length: str) -> ProductRow:
    row = _row(product_id, "sofa", 5, "2500")
    return row.model_copy(update={"dimensions": RawDimensions(unit="cm", length=Decimal(length))})


async def test_their_sizes_measure_only_the_type_they_were_given_for() -> None:
    narrow = DimensionConstraint(
        role=DimensionRole.OVERALL_WIDTH, kind=DimensionConstraintKind.MAX, max_cm=Decimal("200")
    )
    rows = [_wide(60, "240"), _wide(61, "190"), *[r for r in _MIXED if r.id != 12]]

    solution = await _mixed(
        SeatingRequirements(sized_type="sofa", dimensions=(narrow,)), rows, budget=None
    )

    used = {line.product_id for bundle in solution.bundles for line in bundle.lines}
    assert 60 not in used
    # The width never filtered the sofa sets: it was not given for them.
    assert 11 in used or 10 in used
    for bundle in solution.bundles:
        types = {line.commerce_subcategory for line in bundle.lines if line.seats_are_confirmed}
        # A sofa set is a different type: the width was not about it, and it says so.
        assert bundle.sizes_applied is (types == {"sofa"})


async def test_no_sizes_given_is_not_reported_as_unapplied() -> None:
    solution = await _mixed(SeatingRequirements())

    assert all(bundle.sizes_applied is None for bundle in solution.bundles)


async def test_a_from_price_is_the_price_of_what_would_be_shown() -> None:
    """For a beige request, "separate sofas from X" must be the beige
    arrangement's price, not a cheaper one with a grey piece in it."""
    rows = [
        _row(80, "sofa-set", 6, "2450"),
        _row(81, "sofa", 2, "990"),
        # A different piece, so the grey combination really exists and is cheaper.
        _row(82, "sofa-bed", 2, "860", colour="Ash Grey"),
    ]
    overview = CatalogOverview(
        store_id=50,
        currency="SAR",
        shelves=(
            _shelf("sofa", seats=(2, 2), implied=None),
            _shelf("sofa-set", seats=(6, 6), implied=None),
            _shelf("sofa-bed", seats=(2, 2), implied=None),
        ),
    )

    solution = await _plan(
        rows, SeatingRequirements(wished_colors=("Beige",)), budget=None, overview=overview
    )

    assert solution.options[0].from_price == Decimal("3440")
    assert solution.bundles[0].total_price == Decimal("3440")


# ── never the same combination twice ────────────────────────────────────────


def _contents(bundle: SeatingBundle) -> tuple[tuple[int, int], ...]:
    return tuple(sorted((line.product_id, line.quantity) for line in bundle.lines))


async def test_combinations_already_seen_are_never_shown_again() -> None:
    stock = [*_STOCK, _row(11, "sofa", 3, "1300"), _row(12, "sofa", 2, "1050")]
    first = await _plan(stock, shape=SeatingShape.SEPARATE_SOFAS, budget=None)
    seen = frozenset(_contents(b) for b in first.bundles)

    planner = SeatingSolutionPlanner(
        FakeCapability(_OVERVIEW),  # type: ignore[arg-type]
        FakeDiscovery(stock),  # type: ignore[arg-type]
        FakeHydration(stock),  # type: ignore[arg-type]
        SEATING,
    )
    more = await planner.plan(
        target_seats=8,
        budget_amount=None,
        currency="SAR",
        context=CONTEXT,
        shape=SeatingShape.SEPARATE_SOFAS,
        exclude=seen,
    )

    assert more.outcome is SeatingSolutionOutcome.BUNDLES
    assert not {_contents(b) for b in more.bundles} & seen
    # The reply is told these are new, so it never says there is nothing new.
    assert more.already_seen == len(seen)
    assert first.already_seen == 0


async def _plan_excluding(
    exclude: frozenset[tuple[tuple[int, int], ...]], shape: SeatingShape | None
) -> SeatingSolution:
    planner = SeatingSolutionPlanner(
        FakeCapability(_OVERVIEW),  # type: ignore[arg-type]
        FakeDiscovery(_STOCK),  # type: ignore[arg-type]
        FakeHydration(_STOCK),  # type: ignore[arg-type]
        SEATING,
    )
    return await planner.plan(
        target_seats=8,
        budget_amount=Decimal("6000"),
        currency="SAR",
        context=CONTEXT,
        shape=shape,
        exclude=exclude,
    )


async def _every(shape: SeatingShape | None) -> frozenset[tuple[tuple[int, int], ...]]:
    """Page through until nothing new comes back."""
    seen: frozenset[tuple[tuple[int, int], ...]] = frozenset()
    for _ in range(40):
        page = await _plan_excluding(seen, shape)
        if page.outcome is not SeatingSolutionOutcome.BUNDLES:
            break
        seen |= {_contents(b) for b in page.bundles}
    return seen


async def test_a_shape_that_has_run_out_says_so_and_offers_the_other() -> None:
    seen = await _every(SeatingShape.SEPARATE_SOFAS)

    done = await _plan_excluding(seen, SeatingShape.SEPARATE_SOFAS)

    assert done.outcome is SeatingSolutionOutcome.NO_MORE
    assert done.bundles == ()
    assert done.requested_shape is SeatingShape.SEPARATE_SOFAS
    assert [o.shape for o in done.options] == [SeatingShape.SOFA_WITH_EXTRA_SEATS]


async def test_when_everything_has_been_seen_no_shape_is_offered() -> None:
    seen = await _every(None)

    done = await _plan_excluding(seen, None)

    assert done.outcome is SeatingSolutionOutcome.NO_MORE
    assert done.options == ()


# ── many products of one kind ───────────────────────────────────────────────

_THREE_SEATERS = [_row(90 + n, "sofa", 3, str(1600 + 100 * n)) for n in range(6)]
_ONE_SET = [_row(99, "sofa-set", 6, "2400")]
_KINDS = CatalogOverview(
    store_id=50,
    currency="SAR",
    shelves=(
        _shelf("sofa", seats=(3, 3), implied=None),
        _shelf("sofa-set", seats=(6, 6), implied=None),
    ),
)


async def _page(
    exclude: frozenset[tuple[tuple[int, int], ...]], budget: str = "4500"
) -> SeatingSolution:
    rows = [*_THREE_SEATERS, *_ONE_SET]
    planner = SeatingSolutionPlanner(
        FakeCapability(_KINDS),  # type: ignore[arg-type]
        FakeDiscovery(rows),  # type: ignore[arg-type]
        FakeHydration(rows),  # type: ignore[arg-type]
        SEATING,
    )
    return await planner.plan(
        target_seats=9,
        budget_amount=Decimal(budget),
        currency="SAR",
        context=CONTEXT,
        shape=SeatingShape.SEPARATE_SOFAS,
        exclude=exclude,
    )


async def test_the_same_layout_comes_in_other_products() -> None:
    """Live, one 3-seater stood in for all twelve dark ones, and "show more"
    said there was nothing more."""
    first = await _page(frozenset())

    with_set = [b for b in first.bundles if 99 in {line.product_id for line in b.lines}]
    assert len(with_set) >= 2


async def test_paging_reaches_every_product_that_fits() -> None:
    """Each page looks further down each kind, so the fifth and sixth
    3-seater - beyond the first page's reach - still come before "no more"."""
    seen: frozenset[tuple[tuple[int, int], ...]] = frozenset()
    sofas_used: set[int] = set()
    for _ in range(10):
        page = await _page(seen)
        if page.outcome is not SeatingSolutionOutcome.BUNDLES:
            break
        for bundle in page.bundles:
            sofas_used |= {line.product_id for line in bundle.lines if line.product_id != 99}
        seen |= {_contents(b) for b in page.bundles}

    # Every 3-seater that fits beside the set within 4,500 (up to 2,100).
    assert sofas_used >= {90, 91, 92, 93, 94, 95}


async def test_three_of_a_kind_need_not_match() -> None:
    first = await _page(frozenset(), budget="6000")
    seen = frozenset(_contents(b) for b in first.bundles)
    second = await _page(seen, budget="6000")

    mixed = [
        b
        for b in (*first.bundles, *second.bundles)
        if all(line.seats_each == 3 for line in b.lines) and len(b.lines) > 1
    ]
    assert mixed, "three 3-seaters may be different sofas"


def test_the_cap_keeps_the_best_not_the_first(monkeypatch: Any) -> None:
    import app.services.seating_solution as planner_module

    monkeypatch.setattr(planner_module, "MAX_DRAFTS", 2)
    pieces = [
        planner_module._Piece(
            subcategory="sofa",
            seats=3,
            confirmed=True,
            variants=tuple(
                EligibleProduct(product_id=n, price_amount=Decimal(price))
                for n, price in ((1, "3000"), (2, "1000"), (3, "2000"))
            ),
        )
    ]

    drafts = planner_module._combinations(pieces, 6, None, SeatingRequirements(), ())

    assert [d.total for d in drafts] == [Decimal("2000"), Decimal("3000")]


async def test_a_cheaper_piece_outside_their_wish_still_meets_the_budget() -> None:
    """Every beige 3-seater is too dear to fit beside the set; a wish orders,
    it never filters, so the grey one that fits still makes a combination."""
    rows = [
        *[_row(110 + n, "sofa", 3, str(3000 + 100 * n)) for n in range(4)],
        _row(120, "sofa", 3, "1000", colour="Grey"),
        _row(121, "sofa-set", 6, "2000"),
    ]
    overview = CatalogOverview(
        store_id=50,
        currency="SAR",
        shelves=(
            _shelf("sofa", seats=(3, 3), implied=None),
            _shelf("sofa-set", seats=(6, 6), implied=None),
        ),
    )

    solution = await _plan(
        rows,
        SeatingRequirements(wished_colors=("Beige",)),
        seats=9,
        budget="3200",
        overview=overview,
    )

    assert solution.outcome is SeatingSolutionOutcome.BUNDLES
    assert [120, 121] in _ids(solution)


# ── a room's own seating ────────────────────────────────────────────────────

ROOM_TYPES = frozenset({"sofa", "sectional-sofa", "sofa-set", "single-seater-sofa", "chair"})


async def _arrange(
    seats: int, budget: str | None = "12000", rows: list[ProductRow] = _STOCK
) -> tuple[Any, ...]:
    planner = SeatingSolutionPlanner(
        FakeCapability(_OVERVIEW),  # type: ignore[arg-type]
        FakeDiscovery(rows),  # type: ignore[arg-type]
        FakeHydration(rows),  # type: ignore[arg-type]
        SEATING,
    )
    return await planner.arrangements(
        target_seats=seats,
        budget_amount=Decimal(budget) if budget else None,
        currency="SAR",
        context=CONTEXT,
        types=ROOM_TYPES,
    )


async def test_a_room_that_one_piece_seats_is_offered_that_piece_first() -> None:
    arrangements = await _arrange(3)

    first = arrangements[0]
    assert [(line.product_id, line.quantity) for line in first.lines] == [(4, 1)]
    assert first.total_seats == 3


async def test_a_room_beyond_any_one_piece_is_seated_exactly_by_a_combination() -> None:
    arrangements = await _arrange(9)

    assert arrangements
    for arrangement in arrangements:
        assert arrangement.total_seats == 9
        assert arrangement.total_price <= Decimal("12000")


async def test_a_room_uses_only_its_own_seating_types() -> None:
    """A sofa bed and a chaise are real seating, but not this room's."""
    for seats in (3, 5, 9):
        for arrangement in await _arrange(seats):
            assert {line.commerce_subcategory for line in arrangement.lines} <= ROOM_TYPES


async def test_a_small_group_may_be_seated_by_single_seats_alone() -> None:
    only_chairs = [r for r in _STOCK if r.commerce.subcategory in ("chair", "single-seater-sofa")]

    arrangements = await _arrange(2, rows=only_chairs)

    assert arrangements
    assert all(arrangement.total_seats == 2 for arrangement in arrangements)


async def test_a_room_nothing_seats_within_budget_gets_no_arrangement() -> None:
    assert await _arrange(9, budget="1000") == ()


async def test_beyond_the_best_few_only_the_cheapest_is_added() -> None:
    """The room's other pieces share the budget, so the cheapest way to seat
    them is weighed even when it is not among the best ranked."""
    planner = SeatingSolutionPlanner(
        FakeCapability(_OVERVIEW),  # type: ignore[arg-type]
        FakeDiscovery(_STOCK),  # type: ignore[arg-type]
        FakeHydration(_STOCK),  # type: ignore[arg-type]
        SEATING,
    )
    every = await _arrange(9)
    few = await planner.arrangements(
        target_seats=9,
        budget_amount=Decimal("12000"),
        currency="SAR",
        context=CONTEXT,
        types=ROOM_TYPES,
        limit=1,
    )

    assert 1 <= len(few) <= 2
    assert few[0] == every[0]
    assert min(a.total_price for a in few) == min(a.total_price for a in every)



async def test_a_sofa_bed_is_a_main_piece_only_when_they_asked_for_sofa_beds() -> None:
    """Nobody asking for sofas for eight wants a sofa bed they never mentioned."""
    usual = await _plan(budget=None)
    asked = await _plan(requirements=SeatingRequirements(asked_type="sofa-bed"), budget=None)

    def sofa_beds(solution: SeatingSolution) -> int:
        return sum(
            line.commerce_subcategory == "sofa-bed"
            for bundle in solution.bundles
            for line in bundle.lines
        )

    assert usual.bundles and sofa_beds(usual) == 0
    assert sum(o.combination_count for o in asked.options) > sum(
        o.combination_count for o in usual.options
    )


async def test_over_budget_says_the_closest_real_total() -> None:
    """Nothing seats eight under 1,000: the reply can offer the lowest real
    total that does, the budget set aside and everything else kept."""
    solution = await _plan(budget="1000")
    unlimited = await _plan(budget=None)

    assert solution.outcome is SeatingSolutionOutcome.NONE_WITHIN_BUDGET
    assert solution.closest_total is not None
    assert solution.closest_total > Decimal("1000")
    assert solution.closest_total == min(o.from_price for o in unlimited.options)


async def test_the_closest_total_holds_to_everything_else_they_asked() -> None:
    strict = SeatingRequirements(colors_any_of=("Beige",))

    solution = await _plan(requirements=strict, budget="1000")
    any_colour = await _plan(budget="1000")

    assert solution.closest_total is not None and any_colour.closest_total is not None
    assert solution.closest_total >= any_colour.closest_total
