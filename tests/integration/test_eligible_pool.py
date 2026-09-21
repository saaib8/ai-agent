"""The eligible-id pool against real PostgreSQL.

The point of this file: the 50-row presentation limit must not be able to
decide what semantic ranking gets to see. A pool of 173 has to arrive as 173.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.db.tables import core_product, core_store
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.schemas.discovery import PriceConstraint, ProductSearchRequest
from app.schemas.retailer import RetailerContext
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

STORE_A, STORE_B = 1, 2
CONTEXT_A, CONTEXT_B = RetailerContext(store_id=STORE_A), RetailerContext(store_id=STORE_B)
SOFA_COUNT = 173  # the real store-50 sofa pool, reproduced here


def _product(product_id: int, store_id: int, *, price: str = "2000.00") -> dict[str, Any]:
    return {
        "id": product_id, "uuid": uuid4(), "store_id": store_id,
        "name_english": f"Sofa {product_id}", "name_arabic": f"أريكة {product_id}",
        "price_amount": Decimal(price), "price_unit": "SAR",
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "3-seater-sofa", "commerce_category": "seating",
        "commerce_subcategory": "sofa", "seating_capacity": 3,
        "main_color": "Beige", "styles": "Modern, Minimalist",
        "length": None, "width": None, "height": None, "dimension_unit": None,
        "is_active": True, "detection": False,
    }


@pytest.fixture
async def catalog(writable_engine: AsyncEngine) -> None:
    async with writable_engine.begin() as connection:
        await connection.execute(core_store.insert(), [
            {"id": s, "uuid": uuid4(), "name_english": f"Store {s}",
             "name_arabic": None, "active_status": True}
            for s in (STORE_A, STORE_B)
        ])
        rows = [_product(i, STORE_A) for i in range(1, SOFA_COUNT + 1)]
        rows += [_product(1000 + i, STORE_B) for i in range(1, 11)]
        # One inactive and one unclassified, which must never be eligible.
        rows.append({**_product(9001, STORE_A), "is_active": False})
        rows.append({**_product(9002, STORE_A), "commerce_subcategory": None})
        await connection.execute(core_product.insert(), rows)


def _request(**kwargs: Any) -> ProductSearchRequest:
    return ProductSearchRequest(
        commerce_category="seating", commerce_subcategory="sofa", **kwargs
    )


async def test_the_pool_exceeds_the_fifty_row_presentation_limit(
    database: Database, catalog: None
) -> None:
    """173 eligible sofas must reach ranking as 173, not as the first 50."""
    async with database.session() as session:
        ids = await ProductRepository(session).search_eligible_ids(_request(), CONTEXT_A)

    assert len(ids) == SOFA_COUNT
    assert len(ids) > 50


async def test_search_still_honours_its_presentation_limit(
    database: Database, catalog: None
) -> None:
    """The two paths differ only in presentation, and deliberately so."""
    async with database.session() as session:
        repo = ProductRepository(session)
        shown = await repo.search(_request(), CONTEXT_A, limit=50)
        pool = await repo.search_eligible_ids(_request(), CONTEXT_A)

    assert len(shown) == 50
    assert len(pool) == SOFA_COUNT
    assert {r.id for r in shown} <= set(pool)


async def test_the_pool_and_search_agree_on_eligibility(
    database: Database, catalog: None
) -> None:
    """Same predicates, so an unbounded search must return exactly the pool."""
    async with database.session() as session:
        repo = ProductRepository(session)
        pool = await repo.search_eligible_ids(_request(), CONTEXT_A)
        rows = await repo.search(_request(), CONTEXT_A, limit=10_000)

    assert sorted(r.id for r in rows) == sorted(pool)


async def test_the_pool_excludes_inactive_and_unclassified_products(
    database: Database, catalog: None
) -> None:
    async with database.session() as session:
        ids = await ProductRepository(session).search_eligible_ids(_request(), CONTEXT_A)

    assert 9001 not in ids, "inactive"
    assert 9002 not in ids, "no commerce subcategory"


async def test_the_pool_is_scoped_to_one_retailer(
    database: Database, catalog: None
) -> None:
    async with database.session() as session:
        repo = ProductRepository(session)
        a = await repo.search_eligible_ids(_request(), CONTEXT_A)
        b = await repo.search_eligible_ids(_request(), CONTEXT_B)

    assert set(a) & set(b) == set()
    assert len(b) == 10
    assert all(i <= SOFA_COUNT for i in a)


async def test_filters_narrow_the_pool_exactly_as_they_narrow_search(
    database: Database, catalog: None
) -> None:
    constrained = _request(price=PriceConstraint.at_most(Decimal("1000"), "SAR"))
    async with database.session() as session:
        repo = ProductRepository(session)
        pool = await repo.search_eligible_ids(constrained, CONTEXT_A)
        rows = await repo.search(constrained, CONTEXT_A, limit=10_000)

    assert pool == []
    assert rows == []
