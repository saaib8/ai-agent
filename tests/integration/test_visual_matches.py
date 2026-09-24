"""Furniture Finder's catalog reads against a real PostgreSQL.

The unit suite proves both queries carry the scope predicates; this proves
they return the right rows: an index vector resolves to a product only when
this store sells it, by vector id or by product page, and a store is offered
only the visual categories it sells something in.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.db.tables import core_product, core_store
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.schemas.retailer import RetailerContext
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

STORE_A = 1
STORE_B = 2


def _product(
    product_id: int,
    store_id: int,
    *,
    category: str = "sofa",
    pinecone_id: str | None = None,
    product_url: str | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    return {
        "id": product_id,
        "uuid": uuid4(),
        "store_id": store_id,
        "name_english": f"Product {product_id}",
        "name_arabic": f"منتج {product_id}",
        "price_amount": Decimal("1000.00"),
        "price_unit": "SAR",
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": product_url or f"https://example.test/{product_id}",
        "category": category,
        "commerce_category": "seating",
        "commerce_subcategory": "sofa",
        "seating_capacity": 3,
        "is_active": is_active,
        "detection": False,
        "pinecone_id": pinecone_id,
    }


@pytest.fixture
async def seeded(writable_engine: AsyncEngine) -> None:
    async with writable_engine.begin() as connection:
        await connection.execute(
            core_store.insert(),
            [
                {"id": s, "uuid": uuid4(), "name_english": f"Store {s}", "active_status": True}
                for s in (STORE_A, STORE_B)
            ],
        )
        await connection.execute(
            core_product.insert(),
            [
                _product(10, STORE_A, pinecone_id="vec-10"),
                _product(11, STORE_A, pinecone_id="vec-11", product_url="https://shop/sofa-11"),
                _product(12, STORE_A, pinecone_id="vec-12", is_active=False),
                _product(13, STORE_A, category="Side-Table", pinecone_id="vec-13"),
                _product(14, STORE_A, category="lampshade", pinecone_id=""),
                _product(15, STORE_A, category="carpet"),
                _product(20, STORE_B, category="bed", pinecone_id="vec-20"),
                _product(21, STORE_B, pinecone_id="vec-21", product_url="https://shop/sofa-21"),
            ],
        )


async def test_categories_are_the_ones_this_store_sells_in(
    database: Database, seeded: None
) -> None:
    async with database.session() as session:
        categories = await ProductRepository(session).visual_categories(
            RetailerContext(store_id=STORE_A)
        )
    # Lower-cased; bed is another store's.
    assert categories == frozenset({"sofa", "side-table", "lampshade", "carpet"})


async def test_vectors_resolve_only_to_products_this_store_sells(
    database: Database, seeded: None
) -> None:
    async with database.session() as session:
        by_vector, by_url = await ProductRepository(session).ids_for_visual_matches(
            ["vec-10", "vec-12", "vec-20", "vec-missing"],
            ["https://shop/sofa-11", "https://shop/sofa-21"],
            RetailerContext(store_id=STORE_A),
        )
    # vec-12 is inactive, vec-20 and sofa-21 belong to store B.
    assert by_vector == {"vec-10": 10, "vec-11": 11}
    assert by_url == {"https://example.test/10": 10, "https://shop/sofa-11": 11}
