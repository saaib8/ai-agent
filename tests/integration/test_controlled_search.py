"""Controlled relaxation over the real stack: service -> discovery -> PostgreSQL."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.core.config import DiscoverySettings, RelaxationSettings
from app.db.tables import core_product, core_store
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.schemas.discovery import (
    PriceConstraint,
    ProductSearchRequest,
    ProductSort,
    SeatingCapacityConstraint,
)
from app.schemas.query import ConstraintSemantics, ConstraintStrength, ResolvedSearch
from app.schemas.relaxation import ControlledSearchResult, StopReason
from app.schemas.retailer import RetailerContext
from app.services.controlled_search import ControlledRelaxationService
from app.services.discovery import ProductDiscoveryService
from app.services.relaxation import RelaxationPlanner
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

STORE_A = 1
STORE_B = 2
CONTEXT_A = RetailerContext(store_id=STORE_A)
CONTEXT_B = RetailerContext(store_id=STORE_B)
SAR = "SAR"
APPROXIMATE = ConstraintStrength.APPROXIMATE
PREFERRED = ConstraintStrength.PREFERRED
LOCKED = ConstraintStrength.LOCKED


def _product(
    product_id: int,
    store_id: int,
    *,
    price: str,
    capacity: int | None = 3,
    subcategory: str | None = "sofa",
) -> dict[str, Any]:
    return {
        "id": product_id,
        "uuid": uuid4(),
        "store_id": store_id,
        "name_english": f"Product {product_id}",
        "name_arabic": f"منتج {product_id}",
        "price_amount": Decimal(price),
        "price_unit": SAR,
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "3-seater-sofa",
        "commerce_category": "seating",
        "commerce_subcategory": subcategory,
        "seating_capacity": capacity,
        "length": None,
        "width": None,
        "height": None,
        "dimension_unit": None,
        "is_active": True,
        "detection": False,
    }


@pytest.fixture
async def catalog(writable_engine: AsyncEngine) -> None:
    """Prices straddle the 10% and 20% widening thresholds of a 5000 ceiling."""
    async with writable_engine.begin() as connection:
        await connection.execute(
            core_store.insert(),
            [
                {
                    "id": store_id,
                    "uuid": uuid4(),
                    "name_english": f"Store {store_id}",
                    "name_arabic": None,
                    "active_status": True,
                }
                for store_id in (STORE_A, STORE_B)
            ],
        )
        await connection.execute(
            core_product.insert(),
            [
                # At or under 5000: found by the exact search.
                _product(1, STORE_A, price="4000.00"),
                _product(2, STORE_A, price="5000.00"),
                # Between 5000 and 5500: needs the 10% step.
                _product(3, STORE_A, price="5200.00"),
                _product(4, STORE_A, price="5400.00"),
                # Between 5500 and 6000: needs the 20% step.
                _product(5, STORE_A, price="5900.00"),
                # Beyond any permitted widening.
                _product(6, STORE_A, price="9000.00"),
                # Priced beyond every permitted widening, so these two take no
                # part in the price cases; they exist for the seating ones.
                # Capacity 4: reachable only once seats widen.
                _product(7, STORE_A, price="9500.00", capacity=4),
                # Capacity unverified: must never be released by widening.
                _product(8, STORE_A, price="9500.00", capacity=None),
                # Another retailer holding the same shape of stock.
                _product(20, STORE_B, price="4000.00"),
                _product(21, STORE_B, price="4100.00"),
                _product(22, STORE_B, price="4200.00"),
                _product(23, STORE_B, price="4300.00"),
                _product(24, STORE_B, price="4400.00"),
            ],
        )


def _service(database: Database, session: Any, **policy: Any) -> ControlledRelaxationService:
    settings = RelaxationSettings(**policy)
    return ControlledRelaxationService(
        ProductDiscoveryService(
            ProductRepository(session),
            load_taxonomy(),
            DiscoverySettings(),
            load_catalog_attributes(),
            load_dimension_semantics(taxonomy=load_taxonomy()),
        ),
        RelaxationPlanner(settings),
        settings,
    )


async def _run(
    database: Database,
    resolved: ResolvedSearch,
    context: RetailerContext,
    **policy: Any,
) -> ControlledSearchResult:
    async with database.session() as session:
        return await _service(database, session, **policy).search(resolved, context)


def _ids(result: ControlledSearchResult) -> list[int]:
    return [c.product.product_id for c in result.candidates]


def _resolved(
    *,
    price: PriceConstraint | None = None,
    capacity: SeatingCapacityConstraint | None = None,
    sort: ProductSort = ProductSort.DEFAULT,
    **semantics: ConstraintStrength | None,
) -> ResolvedSearch:
    return ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sofa",
            price=price,
            seating_capacity=capacity,
            sort=sort,
        ),
        semantics=ConstraintSemantics(**semantics),
    )


# ── exact sufficiency ───────────────────────────────────────────────────────


async def test_a_sufficient_exact_search_is_never_widened(
    database: Database, catalog: None
) -> None:
    result = await _run(
        database,
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("9999")),
            price_max=APPROXIMATE,
        ),
        CONTEXT_A,
    )

    assert result.stop_reason is StopReason.EXACT_SUFFICIENT
    assert result.relaxation_attempt_count == 0
    assert all(c.relaxation_depth == 0 for c in result.candidates)


# ── price widening ──────────────────────────────────────────────────────────


async def test_approximate_price_widens_in_two_steps_and_stops(
    database: Database, catalog: None
) -> None:
    """Exact finds 2; +10% adds 2; +20% adds the fifth and reaches the target."""
    result = await _run(
        database,
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=APPROXIMATE,
        ),
        CONTEXT_A,
    )

    assert result.exact_candidate_count == 2
    assert result.stop_reason is StopReason.TARGET_REACHED
    assert result.relaxation_attempt_count == 2
    assert _ids(result) == [1, 2, 3, 4, 5]
    depths = {c.product.product_id: c.relaxation_depth for c in result.candidates}
    assert depths == {1: 0, 2: 0, 3: 1, 4: 1, 5: 2}
    assert result.final_request.price is not None
    assert result.final_request.price.max_amount == Decimal("6000.00")


async def test_preferred_price_widens_the_same_way(
    database: Database, catalog: None
) -> None:
    result = await _run(
        database,
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=PREFERRED,
        ),
        CONTEXT_A,
    )

    assert result.stop_reason is StopReason.TARGET_REACHED
    assert all(
        c.strength is PREFERRED for a in result.attempts for c in a.changes
    )


async def test_the_product_beyond_the_ceiling_is_never_reached(
    database: Database, catalog: None
) -> None:
    """9000 is past +20% of 5000, so no permitted widening admits it."""
    result = await _run(
        database,
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=APPROXIMATE,
        ),
        CONTEXT_A,
    )

    assert 6 not in _ids(result)


async def test_a_locked_ceiling_is_honoured_even_when_short(
    database: Database, catalog: None
) -> None:
    result = await _run(
        database,
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=LOCKED,
        ),
        CONTEXT_A,
    )

    assert result.stop_reason is StopReason.NO_RELAXABLE_CONSTRAINTS
    assert result.relaxation_attempt_count == 0
    assert _ids(result) == [1, 2]
    assert 3 not in _ids(result)


# ── seating ─────────────────────────────────────────────────────────────────


async def test_seating_widens_by_one_seat(database: Database, catalog: None) -> None:
    """Capacity 3 exactly finds the 3-seaters; widening to 2..4 admits id 7."""
    result = await _run(
        database,
        _resolved(
            capacity=SeatingCapacityConstraint.exactly(3),
            seating_min=APPROXIMATE,
            seating_max=APPROXIMATE,
        ),
        CONTEXT_A,
        target_candidates=7,
    )

    assert 7 in _ids(result)
    assert result.relaxation_attempt_count == 1
    assert result.final_request.seating_capacity is not None
    assert result.final_request.seating_capacity.max_capacity == 4


async def test_unverified_capacity_is_never_released_by_widening(
    database: Database, catalog: None
) -> None:
    """NULL capacity means unknown. Widening the range must not admit it."""
    result = await _run(
        database,
        _resolved(
            capacity=SeatingCapacityConstraint.exactly(3),
            seating_min=APPROXIMATE,
            seating_max=APPROXIMATE,
        ),
        CONTEXT_A,
        target_candidates=7,
    )

    assert 8 not in _ids(result)
    assert all(a.request.seating_capacity is not None for a in result.attempts)


# ── retailer isolation ──────────────────────────────────────────────────────


async def test_widening_never_reaches_another_retailer(
    database: Database, catalog: None
) -> None:
    """Store B holds five matching products; store A must never see them."""
    result = await _run(
        database,
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=APPROXIMATE,
        ),
        CONTEXT_A,
    )

    assert all(product_id < 20 for product_id in _ids(result))


async def test_each_retailer_gets_its_own_outcome(
    database: Database, catalog: None
) -> None:
    resolved = _resolved(
        price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
        price_max=APPROXIMATE,
    )

    from_b = await _run(database, resolved, CONTEXT_B)

    assert from_b.stop_reason is StopReason.EXACT_SUFFICIENT
    assert _ids(from_b) == [20, 21, 22, 23, 24]


# ── sort ────────────────────────────────────────────────────────────────────


async def test_price_sorting_is_preserved_through_widening(
    database: Database, catalog: None
) -> None:
    result = await _run(
        database,
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=APPROXIMATE,
            sort=ProductSort.PRICE_DESC,
        ),
        CONTEXT_A,
    )

    assert all(a.request.sort is ProductSort.PRICE_DESC for a in result.attempts)
    # Within the exact attempt, the dearest eligible product came first.
    assert _ids(result)[0] == 2


async def test_every_generated_request_stays_valid(
    database: Database, catalog: None
) -> None:
    result = await _run(
        database,
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            capacity=SeatingCapacityConstraint.exactly(3),
            price_max=APPROXIMATE,
            seating_min=PREFERRED,
            seating_max=PREFERRED,
        ),
        CONTEXT_A,
        target_candidates=20,
    )

    for attempt in result.attempts:
        ProductSearchRequest.model_validate(attempt.request.model_dump())
        assert attempt.request.commerce_category == "seating"
        assert attempt.request.commerce_subcategory == "sofa"
