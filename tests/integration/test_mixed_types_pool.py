"""A sofa search covering sofa sets and sectionals, against real PostgreSQL.

Every other constraint applies to the types beside it alike, store scope holds,
and a request without them reads exactly as it always did.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.db.tables import core_product, core_store
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.schemas.discovery import (
    PriceConstraint,
    ProductSearchRequest,
    SeatingCapacityConstraint,
)
from app.schemas.retailer import RetailerContext
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

STORE, OTHER_STORE = 1, 2
CONTEXT = RetailerContext(store_id=STORE)
FAMILY = ("sofa-set", "sectional-sofa")


def _product(
    product_id: int,
    subcategory: str,
    *,
    seats: int | None = 3,
    price: str = "2000.00",
    store_id: int = STORE,
) -> dict[str, Any]:
    return {
        "id": product_id, "uuid": uuid4(), "store_id": store_id,
        "name_english": f"Piece {product_id}", "name_arabic": f"قطعة {product_id}",
        "price_amount": Decimal(price), "price_unit": "SAR",
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "sofa", "commerce_category": "seating",
        "commerce_subcategory": subcategory, "seating_capacity": seats,
        "main_color": "Beige", "styles": "Modern",
        "length": None, "width": None, "height": None, "dimension_unit": None,
        "is_active": True, "detection": False,
    }


@pytest.fixture
async def catalog(writable_engine: AsyncEngine) -> None:
    async with writable_engine.begin() as connection:
        await connection.execute(core_store.insert(), [
            {"id": s, "uuid": uuid4(), "name_english": f"Store {s}",
             "name_arabic": None, "active_status": True}
            for s in (STORE, OTHER_STORE)
        ])
        await connection.execute(core_product.insert(), [
            _product(1, "sofa", seats=5, price="4550.00"),
            _product(2, "sofa", seats=3),
            _product(3, "sofa-set", seats=5, price="4850.00"),
            _product(4, "sofa-set", seats=7, price="9000.00"),
            _product(5, "sectional-sofa", seats=4),
            # Never searched beside a sofa: not one of the reviewed types.
            _product(6, "sofa-bed", seats=5),
            _product(7, "chair", seats=None),
            # Another store's set never reaches this store's search.
            _product(8, "sofa-set", seats=5, store_id=OTHER_STORE),
        ])


def _sofas(**kwargs: Any) -> ProductSearchRequest:
    return ProductSearchRequest(
        commerce_category="seating", commerce_subcategory="sofa", **kwargs
    )


async def test_the_types_beside_a_sofa_search_are_searched_with_it(
    database: Database, catalog: None
) -> None:
    async with database.session() as session:
        pool = await ProductRepository(session).search_eligible_pool(
            _sofas(alongside_subcategories=FAMILY), CONTEXT
        )

    assert {(p.product_id, p.subcategory) for p in pool} == {
        (1, "sofa"),
        (2, "sofa"),
        (3, "sofa-set"),
        (4, "sofa-set"),
        (5, "sectional-sofa"),
    }


async def test_every_other_constraint_applies_to_them_alike(
    database: Database, catalog: None
) -> None:
    """Five or more seats under 5,000: the 5-seat sofa and the 5-seat set."""
    request = _sofas(
        alongside_subcategories=FAMILY,
        seating_capacity=SeatingCapacityConstraint.at_least(5),
        price=PriceConstraint(currency="SAR", max_amount=Decimal("5000")),
    )
    async with database.session() as session:
        pool = await ProductRepository(session).search_eligible_pool(request, CONTEXT)

    assert sorted(p.product_id for p in pool) == [1, 3]


async def test_a_sofa_search_alone_is_sofas_only(database: Database, catalog: None) -> None:
    async with database.session() as session:
        pool = await ProductRepository(session).search_eligible_pool(_sofas(), CONTEXT)

    assert sorted(p.product_id for p in pool) == [1, 2]
    assert {p.subcategory for p in pool} == {"sofa"}
