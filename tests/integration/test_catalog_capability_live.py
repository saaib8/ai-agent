"""Capability against the real catalog.

The invariant that matters here cannot be checked with a fake: whether the live
data can even be represented. `RetailerCatalogCapabilities` refuses a category
described both broadly and by its children, so if any store classified some
products by category alone and others by subcategory, the contract would reject
its own catalog.
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
from app.services.catalog_capability import CatalogCapabilityService
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.seating import load_seating_semantics
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

STORE_A, STORE_B = 1, 2
CONTEXT_A, CONTEXT_B = RetailerContext(store_id=STORE_A), RetailerContext(store_id=STORE_B)
TAXONOMY = load_taxonomy()
SEATING = load_seating_semantics(taxonomy=TAXONOMY)


def _product(
    product_id: int,
    store_id: int,
    *,
    category: str | None = "seating",
    subcategory: str | None = "sofa",
    active: bool = True,
) -> dict[str, Any]:
    return {
        "id": product_id, "uuid": uuid4(), "store_id": store_id,
        "name_english": f"Product {product_id}", "name_arabic": "منتج",
        "price_amount": Decimal("1000.00"), "price_unit": "SAR",
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "3-seater-sofa", "commerce_category": category,
        "commerce_subcategory": subcategory, "seating_capacity": None,
        "main_color": None, "styles": None,
        "length": None, "width": None, "height": None, "dimension_unit": None,
        "is_active": active, "detection": False,
    }


@pytest.fixture
async def catalog(writable_engine: AsyncEngine) -> None:
    async with writable_engine.begin() as connection:
        await connection.execute(core_store.insert(), [
            {"id": s, "uuid": uuid4(), "name_english": f"Store {s}",
             "name_arabic": None, "active_status": True}
            for s in (STORE_A, STORE_B)
        ])
        await connection.execute(core_product.insert(), [
            _product(1, STORE_A, subcategory="sofa"),
            _product(2, STORE_A, subcategory="sofa"),
            _product(3, STORE_A, category="tables", subcategory="console"),
            _product(4, STORE_A, category="lighting", subcategory="floor-lamp"),
            # Excluded: inactive, unclassified, and another retailer's stock.
            _product(5, STORE_A, category="tables", subcategory="nightstand", active=False),
            _product(6, STORE_A, category=None, subcategory=None),
            _product(7, STORE_B, category="storage", subcategory="shelve"),
        ])


async def _capabilities(database: Database, context: RetailerContext) -> Any:
    async with database.session() as session:
        service = CatalogCapabilityService(ProductRepository(session), TAXONOMY, SEATING)
        return await service.capabilities(context)


async def test_capability_reflects_only_this_stores_live_catalog(
    database: Database, catalog: None
) -> None:
    capabilities = await _capabilities(database, CONTEXT_A)

    assert capabilities.supports("seating", "sofa")
    assert capabilities.supports("tables", "console")
    assert capabilities.supports("lighting", "floor-lamp")
    assert not capabilities.supports("tables", "nightstand"), "inactive"
    assert not capabilities.supports("storage", "shelve"), "another retailer"


async def test_two_retailers_do_not_share_capabilities(
    database: Database, catalog: None
) -> None:
    a = await _capabilities(database, CONTEXT_A)
    b = await _capabilities(database, CONTEXT_B)

    assert b.supports("storage", "shelve")
    assert not b.supports("seating", "sofa")
    assert not a.supports("storage", "shelve")


async def test_an_unclassified_product_contributes_nothing(
    database: Database, catalog: None
) -> None:
    """A product nobody reviewed says nothing about what the retailer sells."""
    capabilities = await _capabilities(database, CONTEXT_A)

    assert all(
        entry.commerce_category is not None for entry in capabilities.capabilities
    )
    assert len(capabilities.capabilities) == 3


async def test_the_result_is_deterministically_ordered(
    database: Database, catalog: None
) -> None:
    first = await _capabilities(database, CONTEXT_A)
    second = await _capabilities(database, CONTEXT_A)

    assert first.capabilities == second.capabilities
    assert [c.commerce_category for c in first.capabilities] == sorted(
        c.commerce_category for c in first.capabilities
    )


async def test_the_live_shape_is_representable(
    database: Database, catalog: None
) -> None:
    """The broad/narrow contradiction cannot arise from this catalog: every
    classified row names a subcategory, so no category is described both ways.

    Construction is the assertion — the contract would have raised.
    """
    capabilities = await _capabilities(database, CONTEXT_A)

    assert all(
        entry.commerce_subcategory is not None for entry in capabilities.capabilities
    )


async def test_a_store_with_nothing_supports_nothing(
    database: Database, catalog: None
) -> None:
    capabilities = await _capabilities(database, RetailerContext(store_id=999))

    assert capabilities.capabilities == ()


async def test_depth_is_counted_from_the_live_catalog(
    database: Database, catalog: None
) -> None:
    """How many of each type, against real SQL.

    The fixture gives store A two sofas and one console, so the two entries
    differ - which is the whole point of carrying a count. A planner asking
    "what would go with this?" can now tell a type the retailer can show a
    range of from one it has a single example of (CLAUDE.md 9).
    """
    capabilities = await _capabilities(database, CONTEXT_A)

    depths = {
        (entry.commerce_category, entry.commerce_subcategory): entry.active_product_count
        for entry in capabilities.capabilities
    }
    assert depths[("seating", "sofa")] == 2
    assert depths[("tables", "console")] == 1
    assert depths[("lighting", "floor-lamp")] == 1


async def test_depth_counts_only_what_a_customer_could_buy(
    database: Database, catalog: None
) -> None:
    """Scoped and active, exactly like the capability it qualifies.

    A count drawn from a wider set than the capability would be worse than no
    count: it would report depth the retailer cannot actually supply, which is
    the failure this field exists to prevent rather than cause.
    """
    a = await _capabilities(database, CONTEXT_A)
    b = await _capabilities(database, CONTEXT_B)

    assert all(entry.active_product_count >= 1 for entry in a.capabilities)
    # The inactive nightstand is absent rather than counted as a thin type.
    assert not any(
        entry.commerce_subcategory == "nightstand" for entry in a.capabilities
    )
    # Store B's single shelf is its own, and store A's stock never inflates it.
    assert [
        (entry.commerce_subcategory, entry.active_product_count)
        for entry in b.capabilities
    ] == [("shelve", 1)]
