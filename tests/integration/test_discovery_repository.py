"""Discovery against real PostgreSQL: filtering, NULLs, isolation, ordering."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.core.config import DiscoverySettings
from app.db.tables import core_product, core_store
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.schemas.discovery import (
    PriceConstraint,
    ProductSearchRequest,
    ProductSearchResult,
    ProductSort,
    SeatingCapacityConstraint,
)
from app.schemas.retailer import RetailerContext
from app.services.discovery import ProductDiscoveryService
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


def _product(
    product_id: int,
    store_id: int,
    *,
    is_active: bool = True,
    category: str | None = "seating",
    subcategory: str | None = "sofa",
    capacity: int | None = 3,
    price: str = "2450.00",
    currency: str = SAR,
) -> dict[str, Any]:
    return {
        "id": product_id,
        "uuid": uuid4(),
        "store_id": store_id,
        "name_english": f"Product {product_id}",
        "name_arabic": f"منتج {product_id}",
        "price_amount": Decimal(price),
        "price_unit": currency,
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "3-seater-sofa",
        "commerce_category": category,
        "commerce_subcategory": subcategory,
        "seating_capacity": capacity,
        "length": Decimal("90.00"),
        "width": Decimal("220.00"),
        "height": Decimal("85.00"),
        "dimension_unit": "cm",
        "is_active": is_active,
        "detection": False,
    }


@pytest.fixture
async def catalog(writable_engine: AsyncEngine) -> None:
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
                # Store A: a spread of seating.
                _product(1, STORE_A, price="990.00", capacity=2),
                _product(2, STORE_A, price="2450.00", capacity=3),
                _product(3, STORE_A, price="9950.00", capacity=4),
                _product(4, STORE_A, subcategory="sectional-sofa", price="7500.00", capacity=5),
                _product(
                    5, STORE_A, subcategory="single-seater-sofa",
                    capacity=None, price="1200.00",
                ),
                # Other categories, and rows that must never match.
                _product(6, STORE_A, category="lighting", subcategory="table-lamp", capacity=None),
                _product(7, STORE_A, category=None, subcategory=None, capacity=None),
                _product(8, STORE_A, is_active=False, price="100.00", capacity=3),
                _product(9, STORE_A, price="500.00", capacity=3, currency="USD"),
                # Store B: the same classification, a different retailer.
                _product(20, STORE_B, price="2450.00", capacity=3),
                _product(21, STORE_B, subcategory="sectional-sofa", capacity=5),
            ],
        )


async def _search(
    database: Database, request: ProductSearchRequest, context: RetailerContext
) -> ProductSearchResult:
    async with database.session() as session:
        service = ProductDiscoveryService(
            ProductRepository(session),
            load_taxonomy(),
            DiscoverySettings(),
            load_catalog_attributes(),
            load_dimension_semantics(taxonomy=load_taxonomy()),
        )
        return await service.search(request, context)


def _ids(result: ProductSearchResult) -> list[int]:
    return [candidate.product_id for candidate in result.candidates]


# ── filtering ───────────────────────────────────────────────────────────────


async def test_category_filtering(database: Database, catalog: None) -> None:
    result = await _search(
        database, ProductSearchRequest(commerce_category="seating"), CONTEXT_A
    )

    assert _ids(result) == [1, 2, 3, 4, 5, 9]


async def test_subcategory_filtering(database: Database, catalog: None) -> None:
    result = await _search(
        database,
        ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa"),
        CONTEXT_A,
    )

    assert _ids(result) == [1, 2, 3, 9]


async def test_a_different_category_returns_its_own_products(
    database: Database, catalog: None
) -> None:
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="lighting", commerce_subcategory="table-lamp"
        ),
        CONTEXT_A,
    )

    assert _ids(result) == [6]


async def test_price_maximum(database: Database, catalog: None) -> None:
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sofa",
            price=PriceConstraint.at_most(Decimal("2450"), SAR),
        ),
        CONTEXT_A,
    )

    assert _ids(result) == [1, 2]  # inclusive upper bound


async def test_price_range(database: Database, catalog: None) -> None:
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating",
            price=PriceConstraint.between(Decimal("2450"), Decimal("7500"), SAR),
        ),
        CONTEXT_A,
    )

    assert _ids(result) == [2, 4]


async def test_currency_is_matched_exactly_and_never_converted(
    database: Database, catalog: None
) -> None:
    """Product 9 costs USD 500. A SAR constraint must not reach it."""
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating",
            price=PriceConstraint.at_most(Decimal("1000"), SAR),
        ),
        CONTEXT_A,
    )

    assert _ids(result) == [1]

    in_usd = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating",
            price=PriceConstraint.at_most(Decimal("1000"), "USD"),
        ),
        CONTEXT_A,
    )
    assert _ids(in_usd) == [9]


async def test_seating_capacity_exactly(database: Database, catalog: None) -> None:
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating",
            seating_capacity=SeatingCapacityConstraint.exactly(3),
        ),
        CONTEXT_A,
    )

    assert _ids(result) == [2, 9]


async def test_seating_capacity_at_least(database: Database, catalog: None) -> None:
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating",
            seating_capacity=SeatingCapacityConstraint.at_least(4),
        ),
        CONTEXT_A,
    )

    assert _ids(result) == [3, 4]


async def test_combined_structured_filters(database: Database, catalog: None) -> None:
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sofa",
            price=PriceConstraint.between(Decimal("1000"), Decimal("5000"), SAR),
            seating_capacity=SeatingCapacityConstraint.exactly(3),
        ),
        CONTEXT_A,
    )

    assert _ids(result) == [2]


# ── NULL behaviour ──────────────────────────────────────────────────────────


async def test_an_unclassified_product_never_matches_a_category(
    database: Database, catalog: None
) -> None:
    """Product 7 has NULL commerce fields and no fallback to visual category."""
    for category in ("seating", "lighting", "tables"):
        result = await _search(
            database, ProductSearchRequest(commerce_category=category), CONTEXT_A
        )
        assert 7 not in _ids(result), category


async def test_null_capacity_never_satisfies_a_capacity_constraint(
    database: Database, catalog: None
) -> None:
    """Product 5 is a single-seater-sofa whose reviewed capacity is NULL."""
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating",
            seating_capacity=SeatingCapacityConstraint.exactly(1),
        ),
        CONTEXT_A,
    )

    assert _ids(result) == []


async def test_capacity_is_not_inferred_from_the_subcategory(
    database: Database, catalog: None
) -> None:
    """`single-seater-sofa` is not silently read as capacity 1 (CLAUDE.md 31)."""
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating", commerce_subcategory="single-seater-sofa"
        ),
        CONTEXT_A,
    )

    assert _ids(result) == [5]
    assert result.candidates[0].commerce.seating_capacity is None


async def test_null_capacity_is_eligible_when_no_capacity_is_requested(
    database: Database, catalog: None
) -> None:
    result = await _search(
        database, ProductSearchRequest(commerce_category="seating"), CONTEXT_A
    )

    assert 5 in _ids(result)


# ── retailer isolation and active filtering ─────────────────────────────────


async def test_inactive_products_are_excluded(database: Database, catalog: None) -> None:
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating",
            price=PriceConstraint.at_most(Decimal("200"), SAR),
        ),
        CONTEXT_A,
    )

    assert _ids(result) == []  # product 8 costs 100 but is inactive


async def test_the_same_category_does_not_leak_across_stores(
    database: Database, catalog: None
) -> None:
    from_a = await _search(
        database, ProductSearchRequest(commerce_category="seating"), CONTEXT_A
    )
    from_b = await _search(
        database, ProductSearchRequest(commerce_category="seating"), CONTEXT_B
    )

    assert set(_ids(from_a)).isdisjoint(_ids(from_b))
    assert _ids(from_b) == [20, 21]


async def test_a_store_with_no_products_in_a_category_gets_an_empty_result(
    database: Database, catalog: None
) -> None:
    result = await _search(
        database, ProductSearchRequest(commerce_category="lighting"), CONTEXT_B
    )

    assert result.candidates == ()
    assert result.truncated is False


# ── bounding and ordering ───────────────────────────────────────────────────


async def test_the_limit_is_enforced_in_sql(database: Database, catalog: None) -> None:
    result = await _search(
        database, ProductSearchRequest(commerce_category="seating", limit=2), CONTEXT_A
    )

    assert len(result.candidates) == 2
    assert result.truncated is True


async def test_default_ordering_is_stable(database: Database, catalog: None) -> None:
    request = ProductSearchRequest(commerce_category="seating")

    first = await _search(database, request, CONTEXT_A)
    second = await _search(database, request, CONTEXT_A)

    assert _ids(first) == _ids(second) == [1, 2, 3, 4, 5, 9]


async def test_price_ascending(database: Database, catalog: None) -> None:
    result = await _search(
        database,
        ProductSearchRequest(commerce_category="seating", sort=ProductSort.PRICE_ASC),
        CONTEXT_A,
    )

    prices = [candidate.price_amount for candidate in result.candidates]
    assert prices == sorted(prices)
    assert _ids(result)[0] == 9  # USD 500 — sorted by amount, not converted


async def test_price_descending(database: Database, catalog: None) -> None:
    result = await _search(
        database,
        ProductSearchRequest(commerce_category="seating", sort=ProductSort.PRICE_DESC),
        CONTEXT_A,
    )

    prices = [candidate.price_amount for candidate in result.candidates]
    assert prices == sorted(prices, reverse=True)
    assert _ids(result)[0] == 3


# ── ordering is applied before truncation ───────────────────────────────────


@pytest.fixture
async def priced_catalog(writable_engine: AsyncEngine) -> None:
    """Twelve sofas whose price order deliberately contradicts their id order."""
    async with writable_engine.begin() as connection:
        await connection.execute(
            core_store.insert(),
            [
                {
                    "id": STORE_A,
                    "uuid": uuid4(),
                    "name_english": "Store A",
                    "name_arabic": None,
                    "active_status": True,
                }
            ],
        )
        await connection.execute(
            core_product.insert(),
            [
                # id 100 is dearest, id 111 cheapest: id order reverses price order.
                _product(100 + offset, STORE_A, price=f"{1200 - offset * 100}.00")
                for offset in range(12)
            ],
        )


async def test_price_ascending_returns_the_true_cheapest_not_the_first_by_id(
    database: Database, priced_catalog: None
) -> None:
    """ORDER BY must be applied before LIMIT, or truncation loses the cheapest."""
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating", sort=ProductSort.PRICE_ASC, limit=3
        ),
        CONTEXT_A,
    )

    assert [c.price_amount for c in result.candidates] == [
        Decimal("100.00"),
        Decimal("200.00"),
        Decimal("300.00"),
    ]
    assert _ids(result) == [111, 110, 109]
    assert result.truncated is True


async def test_price_descending_returns_the_true_dearest(
    database: Database, priced_catalog: None
) -> None:
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating", sort=ProductSort.PRICE_DESC, limit=3
        ),
        CONTEXT_A,
    )

    assert [c.price_amount for c in result.candidates] == [
        Decimal("1200.00"),
        Decimal("1100.00"),
        Decimal("1000.00"),
    ]
    assert _ids(result) == [100, 101, 102]


async def test_a_price_bound_and_a_price_sort_compose(
    database: Database, priced_catalog: None
) -> None:
    """The cheapest under a ceiling, not the first three under it by id."""
    result = await _search(
        database,
        ProductSearchRequest(
            commerce_category="seating",
            price=PriceConstraint.at_most(Decimal("500"), SAR),
            sort=ProductSort.PRICE_ASC,
            limit=2,
        ),
        CONTEXT_A,
    )

    assert [c.price_amount for c in result.candidates] == [
        Decimal("100.00"),
        Decimal("200.00"),
    ]


async def test_default_ordering_is_by_id_and_makes_no_price_claim(
    database: Database, priced_catalog: None
) -> None:
    """Truncated DEFAULT results are an id-ordered slice, never "the cheapest"."""
    result = await _search(
        database,
        ProductSearchRequest(commerce_category="seating", limit=3),
        CONTEXT_A,
    )

    assert _ids(result) == [100, 101, 102]
    assert [c.price_amount for c in result.candidates] == [
        Decimal("1200.00"),
        Decimal("1100.00"),
        Decimal("1000.00"),
    ]
