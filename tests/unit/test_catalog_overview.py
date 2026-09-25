"""The store's shelf as the agent reads it - ``catalog_overview``.

Distinct from capabilities: same approved-vocabulary discipline, but carrying
the seat ceilings, price bands and palettes a salesperson reasons with before
choosing a move. Every test here keeps it capability, never inventory, and
keeps the taxonomy the only gate on what is surfaced.
"""

from __future__ import annotations

from decimal import Decimal

from app.repositories.products import CatalogOverviewRow
from app.schemas.retailer import RetailerContext
from app.services.catalog_capability import CatalogCapabilityService
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.seating import load_seating_semantics

TAXONOMY = load_taxonomy()
SEATING = load_seating_semantics(taxonomy=TAXONOMY)
CONTEXT = RetailerContext(store_id=50)


class FakeRepository:
    def __init__(self, rows: tuple[CatalogOverviewRow, ...]) -> None:
        self.rows = rows
        self.calls: list[RetailerContext] = []

    async def catalog_overview(self, context: RetailerContext) -> tuple[CatalogOverviewRow, ...]:
        self.calls.append(context)
        return self.rows


def _row(
    category: str,
    subcategory: str | None,
    *,
    count: int = 10,
    seat_known: int = 0,
    seat_min: int | None = None,
    seat_max: int | None = None,
    price_min: str = "1000",
    price_max: str = "5000",
    colours: tuple[str, ...] = (),
    units: tuple[str, ...] = ("SAR",),
) -> CatalogOverviewRow:
    return CatalogOverviewRow(
        commerce_category=category,
        commerce_subcategory=subcategory,
        active_count=count,
        seat_known=seat_known,
        seat_minimum=seat_min,
        seat_maximum=seat_max,
        price_minimum=Decimal(price_min),
        price_maximum=Decimal(price_max),
        colours=colours,
        price_units=units,
    )


def _service(*rows: CatalogOverviewRow) -> tuple[CatalogCapabilityService, FakeRepository]:
    repository = FakeRepository(tuple(rows))
    return CatalogCapabilityService(repository, TAXONOMY, SEATING), repository  # type: ignore[arg-type]


async def test_it_carries_seat_and_price_ranges_and_the_palette() -> None:
    service, _ = _service(
        _row(
            "seating",
            "sofa",
            count=166,
            seat_known=160,
            seat_min=1,
            seat_max=5,
            price_min="990",
            price_max="8000",
            colours=("Beige", "Charcoal", "Grey"),
        )
    )

    overview = await service.overview(CONTEXT)

    (sofa,) = overview.shelves
    assert sofa.commerce_subcategory == "sofa"
    assert sofa.active_count == 166
    assert sofa.price_minimum == Decimal("990")
    assert sofa.price_maximum == Decimal("8000")
    assert sofa.seating is not None
    assert (sofa.seating.minimum, sofa.seating.maximum, sofa.seating.known_count) == (1, 5, 160)
    assert sofa.max_seats == 5
    assert sofa.colours == ("Beige", "Charcoal", "Grey")


async def test_a_type_with_no_seat_knowledge_at_all_has_no_ceiling() -> None:
    """A non-seating type carries neither a confirmed spread nor a reviewed
    default, so its ceiling is None - never read as zero, never a guess."""
    service, _ = _service(_row("tables", "console", seat_known=0))

    overview = await service.overview(CONTEXT)

    (console,) = overview.shelves
    assert console.seating is None
    assert console.implied_seats is None
    assert console.max_seats is None


async def test_implied_seats_let_a_seatless_type_count_as_one() -> None:
    """Store 50's chairs and single-seaters record no seat count, but a reviewer
    says each seats one. That reviewed default - kept distinct from a confirmed
    spread (CLAUDE.md 6.2) - is what lets a chair fill a seat in a combination
    where before it counted for nothing. A confirmed count still wins over it."""
    service, _ = _service(
        _row("seating", "chair", seat_known=0),
        _row("seating", "sofa", seat_known=5, seat_min=2, seat_max=5),
    )

    overview = await service.overview(CONTEXT)
    shelves = {s.commerce_subcategory: s for s in overview.shelves}

    assert shelves["chair"].seating is None
    assert shelves["chair"].implied_seats == 1
    assert shelves["chair"].max_seats == 1
    assert shelves["sofa"].implied_seats is None
    assert shelves["sofa"].max_seats == 5


async def test_max_seats_in_a_family_is_the_single_piece_ceiling() -> None:
    """The figure the combination move turns on: the most any one piece seats,
    across every seating type - so a request for more is composed, not dropped."""
    service, _ = _service(
        _row("seating", "sofa", seat_known=5, seat_min=2, seat_max=5),
        _row("seating", "sofa-set", seat_known=5, seat_min=5, seat_max=7),
        _row("seating", "chair", seat_known=0),
    )

    overview = await service.overview(CONTEXT)

    assert overview.max_seats_in("seating") == 7
    assert overview.max_seats_in("tables") is None


async def test_a_family_with_only_implied_counts_still_has_a_ceiling() -> None:
    """Chairs record no seat count, but each seats one by review - so a
    chair-only seating family has a ceiling of 1, not None. The reviewed default
    fills the gap the catalog leaves, which is what makes chairs usable in a
    combination. A non-seating family, with no default, stays None."""
    service, _ = _service(_row("seating", "chair", seat_known=0))

    overview = await service.overview(CONTEXT)

    assert overview.max_seats_in("seating") == 1
    assert overview.max_seats_in("tables") is None


async def test_unapproved_pairs_are_dropped_not_surfaced() -> None:
    """A stale or mistaken classification must never become a capability a move
    is built on (CLAUDE.md 14.3). Dropping is safe in the direction that matters."""
    service, _ = _service(
        _row("seating", "sofa"),
        _row("seating", "luxury-couch"),  # not in the approved vocabulary
        _row("furniture", "sofa"),  # not an approved category
    )

    overview = await service.overview(CONTEXT)

    assert [s.commerce_subcategory for s in overview.shelves] == ["sofa"]
    assert overview.stocks("seating", "sofa")
    assert not overview.stocks("seating", "luxury-couch")


async def test_shelves_in_gathers_a_whole_family_for_nearest_type() -> None:
    service, _ = _service(
        _row("seating", "sofa"),
        _row("seating", "chair"),
        _row("tables", "console"),
    )

    overview = await service.overview(CONTEXT)

    seating = {s.commerce_subcategory for s in overview.shelves_in("seating")}
    assert seating == {"sofa", "chair"}
    assert {s.commerce_subcategory for s in overview.shelves_in("tables")} == {"console"}


async def test_currency_is_the_one_clean_code_or_nothing() -> None:
    clean, _ = _service(
        _row("seating", "sofa", units=("SAR",)),
        _row("tables", "console", units=("SAR",)),
    )
    assert (await clean.overview(CONTEXT)).currency == "SAR"

    mixed, _ = _service(_row("seating", "sofa", units=("SAR", "USD")))
    assert (await mixed.overview(CONTEXT)).currency is None

    junk, _ = _service(_row("seating", "sofa", units=("test",)))
    assert (await junk.overview(CONTEXT)).currency is None


async def test_the_overview_is_scoped_by_the_context_it_is_given() -> None:
    service, repository = _service(_row("seating", "sofa"))

    await service.overview(CONTEXT)

    assert repository.calls == [CONTEXT]
