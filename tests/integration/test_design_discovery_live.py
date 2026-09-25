"""A room plan turned into real products, against real PostgreSQL.

What a fake pipeline cannot show: that capability, eligibility and hydration
agree on one catalog. A plan asking for a sofa, a table and a lamp meets a shop
that stocks two of the three, and the answer has to distinguish "we searched
and found four" from "there was nothing to search" — with the products
themselves read from the database rather than from anything the specialist
said.
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
from app.schemas.design_discovery import DesignNeedSkipReason
from app.schemas.discovery import PriceConstraint, SeatingCapacityConstraint
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
from app.taxonomy.seating import load_seating_semantics
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

STORE, OTHER_STORE = 1, 2
CONTEXT = RetailerContext(store_id=STORE)
TAXONOMY = load_taxonomy()
SEATING = load_seating_semantics(taxonomy=TAXONOMY)

SOFA_COUNT = 60
TABLE_COUNT = 12


def _product(
    product_id: int,
    *,
    store_id: int = STORE,
    category: str = "seating",
    subcategory: str = "sofa",
    seats: int | None = 3,
    price: str = "4000.00",
) -> dict[str, Any]:
    return {
        "id": product_id, "uuid": uuid4(), "store_id": store_id,
        "name_english": f"Item {product_id}", "name_arabic": "منتج",
        "price_amount": Decimal(price), "price_unit": "SAR",
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "3-seater-sofa", "commerce_category": category,
        "commerce_subcategory": subcategory, "seating_capacity": seats,
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
            for s in (STORE, OTHER_STORE)
        ])
        rows = [_product(i) for i in range(1, SOFA_COUNT + 1)]
        rows += [
            _product(200 + i, category="tables", subcategory="center-table", seats=None)
            for i in range(1, TABLE_COUNT + 1)
        ]
        # A four-seat sofa, so a design-derived capacity has something to find.
        rows.append(_product(300, seats=4))
        # Another retailer's lamp: stocked somewhere, never here.
        rows.append(
            _product(400, store_id=OTHER_STORE, category="lighting",
                     subcategory="floor-lamp", seats=None)
        )
        await connection.execute(core_product.insert(), rows)


def _need(
    category: str,
    subcategory: str,
    *,
    priority: DesignPriority = DesignPriority.REQUIRED,
    seats: SeatingCapacityConstraint | None = None,
) -> DesignCategoryNeed:
    return DesignCategoryNeed(
        commerce_category=category,
        commerce_subcategory=subcategory,
        priority=priority,
        seating_capacity=seats,
    )


async def _discover(
    database: Database, *needs: DesignCategoryNeed, budget: PriceConstraint | None = None
) -> Any:
    async with database.session() as session:
        repository = ProductRepository(session)
        capabilities = await CatalogCapabilityService(repository, TAXONOMY, SEATING).capabilities(
            CONTEXT
        )
        policy = RelaxationSettings()
        pipeline = ProductSearchPipeline(
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
            presentation_limit=3,
        )
        request = InteriorDesignRequest(
            task=DesignTask.ROOM_PLAN,
            design_brief="a calm living room for a family of six",
            budget=budget,
            catalog_capabilities=capabilities,
        )
        return await DesignDiscoveryService(
            cast(Any, pipeline), TAXONOMY
        ).discover(request, InteriorDesignResult(needs=needs), CONTEXT)


async def test_a_plan_meets_a_catalog_that_stocks_two_of_three(
    database: Database, catalog: None
) -> None:
    result = await _discover(
        database,
        _need("seating", "sofa"),
        _need("tables", "center-table"),
        _need("lighting", "floor-lamp", priority=DesignPriority.RECOMMENDED),
    )

    assert result.needs[0].candidate_count == SOFA_COUNT + 1
    assert result.needs[1].candidate_count == TABLE_COUNT
    assert result.needs[2].skipped is DesignNeedSkipReason.RETAILER_CANNOT_SUPPLY
    assert result.searched_count == 2


async def test_the_pool_per_need_is_not_the_presentation_limit(
    database: Database, catalog: None
) -> None:
    """Three cards would have been shown; the optimiser gets sixty-one sofas."""
    result = await _discover(database, _need("seating", "sofa"))

    assert result.needs[0].candidate_count == SOFA_COUNT + 1


async def test_a_design_derived_capacity_filters_on_reviewed_data_only(
    database: Database, catalog: None
) -> None:
    """Only the reviewed four-seat row qualifies. A NULL capacity would stay
    unverified and never match (CLAUDE.md 6.1)."""
    result = await _discover(
        database,
        _need("seating", "sofa", seats=SeatingCapacityConstraint.at_least(4)),
    )

    assert [c.product.product_id for c in result.needs[0].pool.candidates] == [300]


async def test_the_room_budget_does_not_shrink_any_pool(
    database: Database, catalog: None
) -> None:
    """Every sofa costs 4000 and the room's whole budget is 5000. Applied per
    item it would leave the pool intact here and the room unaffordable; the
    point is that it is not applied at all."""
    result = await _discover(
        database,
        _need("seating", "sofa"),
        _need("tables", "center-table"),
        budget=PriceConstraint.at_most(Decimal("5000"), "SAR"),
    )

    assert result.needs[0].candidate_count == SOFA_COUNT + 1
    assert result.needs[1].candidate_count == TABLE_COUNT


async def test_a_zero_result_need_is_preserved_beside_a_full_one(
    database: Database, catalog: None
) -> None:
    """The retailer stocks sofas, but none seating nine. Searched, and empty."""
    result = await _discover(
        database,
        _need("seating", "sofa", seats=SeatingCapacityConstraint.at_least(9)),
        _need("tables", "center-table"),
    )

    assert result.needs[0].pool is not None
    assert result.needs[0].candidate_count == 0
    assert result.needs[0].need.priority is DesignPriority.REQUIRED
    assert result.needs[1].candidate_count == TABLE_COUNT


async def test_candidates_never_come_from_another_store(
    database: Database, catalog: None
) -> None:
    result = await _discover(database, _need("seating", "sofa"))

    ids = {c.product.product_id for c in result.needs[0].pool.candidates}
    assert 400 not in ids
