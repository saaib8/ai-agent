"""The repository against a real PostgreSQL: scoping, and read-only access."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.core.exceptions import CatalogSchemaError
from app.db.catalog_schema import (
    present_commerce_columns,
    verify_commerce_schema,
    verify_product_data_schema,
)
from app.db.tables import (
    PRODUCT_DATA_COLUMNS,
    REQUIRED_COMMERCE_COLUMNS,
    core_product,
    core_store,
)
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.repositories.stores import StoreRepository
from app.schemas.discovery import ProductSearchRequest, ProductSort
from app.schemas.product import BestSellerRule
from app.schemas.retailer import RetailerContext
from app.services.retailer_context import RetailerContextProvider
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

STORE_A = 1
STORE_B = 2


def _product(
    product_id: int,
    store_id: int,
    *,
    is_active: bool = True,
    commerce_category: str | None = "seating",
    commerce_subcategory: str | None = "sofa",
    seating_capacity: int | None = 3,
) -> dict[str, Any]:
    return {
        "id": product_id,
        "uuid": uuid4(),
        "store_id": store_id,
        "name_english": f"Product {product_id}",
        "name_arabic": f"منتج {product_id}",
        "price_amount": Decimal("1999.00"),
        "price_unit": "SAR",
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "3-seater-sofa",
        "commerce_category": commerce_category,
        "commerce_subcategory": commerce_subcategory,
        "seating_capacity": seating_capacity,
        "length": Decimal("90.00"),
        "width": Decimal("220.00"),
        "height": Decimal("85.00"),
        "dimension_unit": "cm",
        "is_active": is_active,
        "detection": False,
    }


@pytest.fixture
async def seeded(writable_engine: AsyncEngine) -> None:
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
                },
                {
                    "id": STORE_B,
                    "uuid": uuid4(),
                    "name_english": "Store B",
                    "name_arabic": None,
                    "active_status": True,
                },
                {
                    "id": 3,
                    "uuid": uuid4(),
                    "name_english": "Closed Store",
                    "name_arabic": None,
                    "active_status": False,
                },
            ],
        )
        await connection.execute(
            core_product.insert(),
            [
                _product(10, STORE_A),
                _product(11, STORE_A),
                _product(12, STORE_A, is_active=False),
                _product(20, STORE_B),
                _product(21, STORE_B),
            ],
        )


async def test_a_store_sees_only_its_own_active_products(database: Database, seeded: None) -> None:
    async with database.session() as session:
        rows = await ProductRepository(session).get_by_ids(
            [10, 11, 12, 20, 21], RetailerContext(store_id=STORE_A)
        )

    assert [row.id for row in rows] == [10, 11]
    assert {row.store_id for row in rows} == {STORE_A}


async def test_asking_for_another_stores_product_returns_nothing(
    database: Database, seeded: None
) -> None:
    """The caller learns nothing about a catalog that is not theirs."""
    async with database.session() as session:
        rows = await ProductRepository(session).get_by_ids(
            [20, 21], RetailerContext(store_id=STORE_A)
        )

    assert rows == []


async def test_counts_are_scoped_and_exclude_inactive_rows(
    database: Database, seeded: None
) -> None:
    async with database.session() as session:
        repository = ProductRepository(session)
        assert await repository.count_active(RetailerContext(store_id=STORE_A)) == 2
        assert await repository.count_active(RetailerContext(store_id=STORE_B)) == 2
        assert await repository.count_active(RetailerContext(store_id=999)) == 0


async def test_rows_carry_raw_dimensions_with_their_unit(database: Database, seeded: None) -> None:
    """Values arrive unconverted; normalisation is a later, dedicated concern."""
    async with database.session() as session:
        rows = await ProductRepository(session).get_by_ids([10], RetailerContext(store_id=STORE_A))

    dimensions = rows[0].dimensions
    assert dimensions.width == Decimal("220.00")
    assert dimensions.unit == "cm"
    assert rows[0].visual_category == "3-seater-sofa"


async def test_only_active_stores_resolve_to_a_scope(database: Database, seeded: None) -> None:
    async with database.session() as session:
        stores = StoreRepository(session)
        assert await stores.get_active(STORE_A) is not None
        assert await stores.get_active(3) is None
        assert await stores.get_active(999) is None


async def test_the_provider_builds_a_scope_from_a_live_store(
    database: Database, seeded: None
) -> None:
    async with database.session() as session:
        provider = RetailerContextProvider(StoreRepository(session))
        assert await provider.resolve(STORE_A) == RetailerContext(store_id=STORE_A)


async def test_the_service_cannot_write_to_the_catalog(database: Database, seeded: None) -> None:
    """Django owns catalog writes; PostgreSQL enforces that, not convention."""
    with pytest.raises(DBAPIError, match="read-only"):
        async with database.session() as session:
            await session.execute(text("UPDATE core_product SET price_amount = 1 WHERE id = 10"))


async def test_required_commerce_columns_are_present(
    database: Database, writable_engine: AsyncEngine
) -> None:
    async with database.engine.connect() as connection:
        present = await present_commerce_columns(connection)
        await verify_commerce_schema(connection)

    assert present == REQUIRED_COMMERCE_COLUMNS


async def test_material_is_product_data_not_commerce(
    database: Database, writable_engine: AsyncEngine
) -> None:
    """`material` is read as product data - by meaning, never a filter - and is
    not one of the commerce columns a search filters on."""
    assert "material" not in REQUIRED_COMMERCE_COLUMNS
    assert "material" in PRODUCT_DATA_COLUMNS
    assert "material" in core_product.c


async def test_a_missing_commerce_column_fails_the_schema_check(
    database: Database, writable_engine: AsyncEngine
) -> None:
    """A reachable catalog missing a required column is a deployment error."""
    async with writable_engine.begin() as connection:
        await connection.execute(text("ALTER TABLE core_product DROP COLUMN seating_capacity"))

    with pytest.raises(CatalogSchemaError, match="missing required column"):
        async with database.engine.connect() as connection:
            await verify_commerce_schema(connection)


async def test_a_missing_product_data_column_fails_the_schema_check(
    database: Database, writable_engine: AsyncEngine
) -> None:
    """Django's product-data migrations go first: without them, startup fails."""
    async with writable_engine.begin() as connection:
        await connection.execute(text("ALTER TABLE core_product DROP COLUMN reviews_json"))

    with pytest.raises(CatalogSchemaError, match="missing product data column"):
        async with database.engine.connect() as connection:
            await verify_product_data_schema(connection)


async def test_reviewed_commerce_fields_are_read_as_stored(
    database: Database, seeded: None
) -> None:
    async with database.session() as session:
        rows = await ProductRepository(session).get_by_ids([10], RetailerContext(store_id=STORE_A))

    commerce = rows[0].commerce
    assert commerce.category == "seating"
    assert commerce.subcategory == "sofa"
    assert commerce.seating_capacity == 3
    # The visual classification is carried separately and never conflated.
    assert rows[0].visual_category == "3-seater-sofa"


async def test_absent_commerce_classification_is_not_derived(
    database: Database, writable_engine: AsyncEngine, seeded: None
) -> None:
    """No fallback to the visual `category`, and no guessed capacity."""
    async with writable_engine.begin() as connection:
        await connection.execute(
            core_product.insert(),
            [
                _product(
                    99,
                    STORE_A,
                    commerce_category=None,
                    commerce_subcategory=None,
                    seating_capacity=None,
                )
            ],
        )

    async with database.session() as session:
        rows = await ProductRepository(session).get_by_ids([99], RetailerContext(store_id=STORE_A))

    commerce = rows[0].commerce
    assert commerce.category is None
    assert commerce.subcategory is None
    assert commerce.seating_capacity is None
    assert rows[0].visual_category == "3-seater-sofa"


async def test_commerce_fields_do_not_weaken_store_isolation(
    database: Database, writable_engine: AsyncEngine, seeded: None
) -> None:
    """Two stores sharing a commerce classification stay isolated."""
    async with writable_engine.begin() as connection:
        await connection.execute(
            core_product.insert(), [_product(30, STORE_B, commerce_category="seating")]
        )

    async with database.session() as session:
        repository = ProductRepository(session)
        rows = await repository.get_by_ids([10, 30], RetailerContext(store_id=STORE_A))

    assert [row.id for row in rows] == [10]
    assert all(row.commerce.category == "seating" for row in rows)


async def test_listing_and_details_text_are_read_for_the_store_alone(
    database: Database, writable_engine: AsyncEngine, seeded: None
) -> None:
    """The listed extras ride with every product read; the long text only with
    one product's details - and neither crosses to another store."""
    async with writable_engine.begin() as connection:
        await connection.execute(
            core_product.update()
            .where(core_product.c.id == 10)
            .values(
                material="Velvet",
                regular_price=Decimal("2499.00"),
                rating=Decimal("4.50"),
                review_count=2,
                weight_kg=Decimal("42.500"),
                sku="001234",
                warranty_months=24,
                sold_quantity=7,
                description="  A deep   velvet sofa. ",
                reviews_json=[{"stars": 5, "text": "Lovely", "verified_buyer": True}],
            )
        )
    async with database.session() as session:
        repository = ProductRepository(session)
        (row,) = await repository.get_by_ids([10], RetailerContext(store_id=STORE_A))
        mine = await repository.details_text(10, RetailerContext(store_id=STORE_A))
        theirs = await repository.details_text(10, RetailerContext(store_id=STORE_B))

    assert row.listing.material == "Velvet"
    assert row.listing.sku == "001234"
    assert row.listing.warranty_months == 24
    assert "sold_quantity" not in type(row.listing).model_fields
    assert mine is not None and mine.description == "  A deep   velvet sofa. "
    assert theirs is None


async def _set(writable_engine: AsyncEngine, product_id: int, **values: Any) -> None:
    async with writable_engine.begin() as connection:
        await connection.execute(
            core_product.update().where(core_product.c.id == product_id).values(**values)
        )


async def test_best_rated_weighs_reviews_and_puts_the_unrated_last(
    database: Database, writable_engine: AsyncEngine, seeded: None
) -> None:
    """One 5-star review does not outrank 4.9 from many; no reviews come last."""
    await _set(writable_engine, 10, rating=Decimal("5.00"), review_count=1)
    await _set(writable_engine, 11, rating=Decimal("4.90"), review_count=40)
    request = ProductSearchRequest(
        commerce_category="seating", commerce_subcategory="sofa", sort=ProductSort.BEST_RATED
    )
    async with database.session() as session:
        rows = await ProductRepository(session).search(
            request, RetailerContext(store_id=STORE_A), limit=10
        )

    assert [r.id for r in rows] == [11, 10]


async def test_best_sellers_follow_the_rule_within_the_store(
    database: Database, writable_engine: AsyncEngine, seeded: None
) -> None:
    """At least the floor, top of its kind in its own store - another store's
    sales never count, and an unsold product never qualifies."""
    await _set(writable_engine, 10, sold_quantity=12)
    await _set(writable_engine, 11, sold_quantity=2)
    await _set(writable_engine, 20, sold_quantity=500)  # store B
    rule = BestSellerRule(min_sold=5, top_share=0.5, max_per_kind=10)
    async with database.session() as session:
        repository = ProductRepository(session)
        named = await repository.best_sellers(
            RetailerContext(store_id=STORE_A), rule, product_ids=[10, 11, 20]
        )
        of_kind = await repository.best_sellers(
            RetailerContext(store_id=STORE_A), rule, kinds=["sofa"]
        )

    assert named == [10]
    assert of_kind == [10]
