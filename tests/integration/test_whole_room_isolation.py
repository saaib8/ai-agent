"""Whole-room execution cannot cross retailers, against real PostgreSQL.

The repository tests already prove a scoped query filters by `store_id`. What
this adds is that every phase a whole-room turn passes through agrees on the
same scope - capability, discovery, lock verification and replacement - so a
product belonging to another retailer cannot enter the room by any of them.

The shape of the fixture is the point: both retailers stock the *same kinds of
thing*, at the same prices, differing only in `store_id`. A test where the
other shop sells something unique would pass even if scoping were broken, since
nothing would match anyway.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from app.core.config import DiscoverySettings, RelaxationSettings
from app.db.tables import core_product, core_store
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.schemas.design import (
    DesignCategoryNeed,
    DesignPriority,
    DesignTask,
    InteriorDesignRequest,
    InteriorDesignResult,
)
from app.schemas.discovery import MAX_EXCLUDED_PRODUCT_IDS  # noqa: F401  (contract anchor)
from app.schemas.retailer import RetailerContext
from app.services.catalog_capability import CatalogCapabilityService
from app.services.controlled_search import ControlledRelaxationService
from app.services.design_discovery import DesignDiscoveryService
from app.services.discovery import ProductDiscoveryService
from app.services.hydration import ProductHydrationService
from app.services.relaxation import RelaxationPlanner
from app.services.search_pipeline import ProductSearchPipeline
from app.services.semantic_ranking import SemanticRankingService
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

OURS, THEIRS = 51, 52
US = RetailerContext(store_id=OURS)
THEM = RetailerContext(store_id=THEIRS)
TAXONOMY = load_taxonomy()

OUR_SOFAS = [1, 2, 3]
THEIR_SOFAS = [101, 102, 103]
THEIR_LAMP = 150


def _row(
    product_id: int,
    store_id: int,
    *,
    category: str = "seating",
    subcategory: str = "sofa",
    price: str = "4000.00",
) -> dict[str, Any]:
    return {
        "id": product_id, "uuid": uuid4(), "store_id": store_id,
        "name_english": f"Item {product_id}", "name_arabic": "منتج",
        "price_amount": Decimal(price), "price_unit": "SAR",
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "3-seater-sofa", "commerce_category": category,
        "commerce_subcategory": subcategory, "seating_capacity": 3,
        "main_color": "Beige", "styles": "Modern",
        "length": None, "width": None, "height": None, "dimension_unit": None,
        "is_active": True, "detection": False,
    }


@pytest.fixture
async def two_retailers(writable_engine: AsyncEngine) -> None:
    async with writable_engine.begin() as connection:
        await connection.execute(core_store.insert(), [
            {"id": s, "uuid": uuid4(), "name_english": f"Store {s}",
             "name_arabic": None, "active_status": True}
            for s in (OURS, THEIRS)
        ])
        rows = [_row(i, OURS) for i in OUR_SOFAS]
        rows += [_row(i, THEIRS) for i in THEIR_SOFAS]
        # Something only the other retailer stocks at all.
        rows.append(
            _row(THEIR_LAMP, THEIRS, category="lighting", subcategory="floor-lamp")
        )
        await connection.execute(core_product.insert(), rows)


def _pipeline(repository: ProductRepository) -> ProductSearchPipeline:
    policy = RelaxationSettings()
    return ProductSearchPipeline(
        ControlledRelaxationService(
            ProductDiscoveryService(
                repository,
                TAXONOMY,
                DiscoverySettings(),
                load_catalog_attributes(),
                load_dimension_semantics(taxonomy=TAXONOMY),
            ),
            RelaxationPlanner(policy),
            policy,
        ),
        SemanticRankingService(None, None),
        ProductHydrationService(repository),
        presentation_limit=10,
    )


async def _room_for(
    database: Database, context: RetailerContext, *needs: DesignCategoryNeed
) -> Any:
    async with database.session() as session:
        repository = ProductRepository(session)
        capabilities = await CatalogCapabilityService(
            repository, TAXONOMY
        ).capabilities(context)
        request = InteriorDesignRequest(
            task=DesignTask.ROOM_PLAN,
            design_brief="a calm living room",
            catalog_capabilities=capabilities,
        )
        return await DesignDiscoveryService(
            cast(Any, _pipeline(repository)), TAXONOMY
        ).discover(request, InteriorDesignResult(needs=needs), context)


def _sofa() -> DesignCategoryNeed:
    return DesignCategoryNeed(
        commerce_category="seating",
        commerce_subcategory="sofa",
        priority=DesignPriority.REQUIRED,
    )


def _lamp() -> DesignCategoryNeed:
    return DesignCategoryNeed(
        commerce_category="lighting",
        commerce_subcategory="floor-lamp",
        priority=DesignPriority.REQUIRED,
    )


def _ids(result: Any) -> set[int]:
    return {
        candidate.product.product_id
        for found in result.needs
        if found.pool is not None
        for candidate in found.pool.candidates
    }


async def test_capability_describes_only_the_active_retailer(
    database: Database, two_retailers: None
) -> None:
    async with database.session() as session:
        repository = ProductRepository(session)
        service = CatalogCapabilityService(repository, TAXONOMY)

        ours = await service.capabilities(US)
        theirs = await service.capabilities(THEM)

    assert ours.supports("seating", "sofa")
    assert not ours.supports("lighting", "floor-lamp"), "only they stock lamps"
    assert theirs.supports("lighting", "floor-lamp")


async def test_discovery_for_a_room_never_returns_another_retailers_product(
    database: Database, two_retailers: None
) -> None:
    """Both shops stock the same sofa at the same price, so a leak would show."""
    ours = await _room_for(database, US, _sofa())
    theirs = await _room_for(database, THEM, _sofa())

    assert _ids(ours) == set(OUR_SOFAS)
    assert _ids(theirs) == set(THEIR_SOFAS)


async def test_a_category_only_they_stock_finds_nothing_here(
    database: Database, two_retailers: None
) -> None:
    result = await _room_for(database, US, _lamp())

    assert _ids(result) == set()
    assert THEIR_LAMP not in _ids(result)


async def test_verifying_a_lock_cannot_resolve_another_retailers_product(
    database: Database, two_retailers: None
) -> None:
    """The lock path a whole-room turn uses before it plans anything.

    A stale-looking lock and a cross-retailer one are indistinguishable here,
    and both must fail: the id exists globally, and that is not a reason to
    put it in this retailer's room.
    """
    async with database.session() as session:
        hydration = ProductHydrationService(ProductRepository(session))

        ours = await hydration.hydrate_ids([OUR_SOFAS[0]], US)
        theirs = await hydration.hydrate_ids([THEIR_SOFAS[0]], US)
        mixed = await hydration.hydrate_ids([OUR_SOFAS[0], THEIR_SOFAS[0]], US)

    assert [p.product_id for p in ours] == [OUR_SOFAS[0]]
    assert theirs == (), "exists globally, belongs to another retailer"
    assert [p.product_id for p in mixed] == [OUR_SOFAS[0]], "the rest is not admitted"


async def test_a_replacement_search_stays_inside_the_retailer(
    database: Database, two_retailers: None
) -> None:
    """Excluding every one of our own sofas finds nothing, not theirs."""
    async with database.session() as session:
        repository = ProductRepository(session)
        capabilities = await CatalogCapabilityService(
            repository, TAXONOMY
        ).capabilities(US)
        request = InteriorDesignRequest(
            task=DesignTask.ROOM_PLAN,
            design_brief="a different sofa",
            catalog_capabilities=capabilities,
        )
        from app.schemas.design_override import DesignNeedSearchOverride

        result = await DesignDiscoveryService(
            cast(Any, _pipeline(repository)), TAXONOMY
        ).discover(
            request,
            InteriorDesignResult(needs=(_sofa(),)),
            US,
            overrides={0: DesignNeedSearchOverride(
                need_id=1, exclude_product_ids=tuple(OUR_SOFAS)
            )},
        )

    assert _ids(result) == set(), "no fallback to another retailer's shelves"
