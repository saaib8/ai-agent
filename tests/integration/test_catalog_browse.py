"""Browse Catalogue's reads against a real PostgreSQL.

The unit suite proves every query carries the scope predicates and that a
page and its count share one WHERE clause; this proves they return the right
rows: another store's and inactive products never appear, name words match
literally in either language, style matching is by token, and facets count
what the store actually holds.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.db.tables import core_product, core_store
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.schemas.catalog import CatalogFilter
from app.schemas.discovery import ProductSort
from app.schemas.retailer import RetailerContext
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

STORE_A = 1
STORE_B = 2
CONTEXT = RetailerContext(store_id=STORE_A)


def _product(
    product_id: int,
    store_id: int = STORE_A,
    *,
    name: str | None = None,
    arabic: str = "منتج",
    category: str = "seating",
    subcategory: str = "sofa",
    price: str = "1000",
    color: str | None = "Beige",
    styles: str | None = "Modern, Minimalist",
    is_active: bool = True,
) -> dict[str, Any]:
    return {
        "id": product_id,
        "uuid": uuid4(),
        "store_id": store_id,
        "name_english": name or f"Product {product_id}",
        "name_arabic": arabic,
        "price_amount": Decimal(price),
        "price_unit": "SAR",
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "sofa",
        "commerce_category": category,
        "commerce_subcategory": subcategory,
        "is_active": is_active,
        "detection": False,
        "main_color": color,
        "styles": styles,
        "length": Decimal("200"),
        "width": Decimal("90"),
        "dimension_unit": "cm",
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
                _product(1, name="Boucle Sofa", price="3600"),
                _product(2, name="Grey Linen Sofa", price="2400", color="Grey"),
                _product(
                    3,
                    name="Accent Chair",
                    subcategory="chair",
                    price="900",
                    styles="Modern_Classic",
                ),
                _product(
                    4,
                    name="Wool Rug 50% off",
                    category="decor",
                    subcategory="carpet",
                    price="450",
                    styles="Boho",
                ),
                _product(
                    5,
                    name="Oak Console",
                    arabic="طاولة كونسول",
                    category="tables",
                    subcategory="console",
                    price="1200",
                    styles=None,
                ),
                _product(6, name="Retired Sofa", is_active=False),
                _product(7, name="Other Store Sofa", store_id=STORE_B),
            ],
        )


async def _browse(database: Database, filters: CatalogFilter, limit: int = 20) -> list[int]:
    async with database.session() as session:
        rows = await ProductRepository(session).browse(filters, CONTEXT, offset=0, limit=limit)
    return [row.id for row in rows]


async def test_only_this_store_s_active_products_are_browsed(
    database: Database, seeded: None
) -> None:
    assert await _browse(database, CatalogFilter()) == [1, 2, 3, 4, 5]
    async with database.session() as session:
        assert await ProductRepository(session).count_browse(CatalogFilter(), CONTEXT) == 5


async def test_name_words_all_match_in_either_language(database: Database, seeded: None) -> None:
    assert await _browse(database, CatalogFilter(name_words=("sofa",))) == [1, 2]
    assert await _browse(database, CatalogFilter(name_words=("grey", "SOFA"))) == [2]
    assert await _browse(database, CatalogFilter(name_words=("كونسول",))) == [5]


async def test_a_wildcard_in_a_search_is_only_a_character(database: Database, seeded: None) -> None:
    assert await _browse(database, CatalogFilter(name_words=("50%",))) == [4]
    assert await _browse(database, CatalogFilter(name_words=("5_%",))) == []


async def test_filters_combine(database: Database, seeded: None) -> None:
    assert await _browse(database, CatalogFilter(category="seating", color="Grey")) == [2]
    assert await _browse(
        database,
        CatalogFilter(min_price=Decimal("1000"), max_price=Decimal("3000"), currency="SAR"),
    ) == [2, 5]
    assert (
        await _browse(database, CatalogFilter(max_price=Decimal("3000"), currency="USD")) == []
    ), "amounts are never compared across currencies"


async def test_style_matches_by_token_not_substring(database: Database, seeded: None) -> None:
    assert await _browse(database, CatalogFilter(style="Modern")) == [1, 2]
    assert await _browse(database, CatalogFilter(style="Modern_Classic")) == [3]


async def test_lead_categories_come_first_then_the_default_order(
    database: Database, seeded: None
) -> None:
    ids = await _browse(database, CatalogFilter(lead_categories=("tables",)))
    assert ids == [5, 1, 2, 3, 4]
    cheapest = await _browse(
        database, CatalogFilter(lead_categories=("tables",), sort=ProductSort.PRICE_ASC)
    )
    assert cheapest == [4, 3, 5, 2, 1]


async def test_facets_count_what_this_store_holds(database: Database, seeded: None) -> None:
    async with database.session() as session:
        counts = await ProductRepository(session).facet_counts(CONTEXT)
    assert dict(counts.colors) == {"Beige": 4, "Grey": 1}
    assert dict(counts.styles) == {"Modern": 2, "Minimalist": 2, "Modern_Classic": 1, "Boho": 1}
    assert counts.prices == (("SAR", Decimal("450.00"), Decimal("3600.00"), 5),)
