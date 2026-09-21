"""The composed pipeline against real PostgreSQL.

The regression that started M11B: a catalog of 173 sofas, the best semantic
match sitting at a high id, and only three products shown. Before the search
foundation was repaired, ranking saw the fifty lowest ids and that product was
unreachable — a function of when a merchant uploaded it, not of whether the
customer would like it.
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
from app.schemas.discovery import ProductSearchRequest, ProductSort
from app.schemas.grounding import SearchOutcome
from app.schemas.query import ResolvedSearch
from app.schemas.retailer import RetailerContext
from app.schemas.semantic import SemanticSkipReason
from app.services.controlled_search import ControlledRelaxationService
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

STORE = 1
CONTEXT = RetailerContext(store_id=STORE)
SOFA_COUNT = 173
BURIED = 168  # well beyond the fifty lowest ids
NAMESPACE = "store-1"


def _product(product_id: int) -> dict[str, Any]:
    return {
        "id": product_id,
        "uuid": uuid4(),
        "store_id": STORE,
        "name_english": f"Sofa {product_id}",
        "name_arabic": f"أريكة {product_id}",
        # Descending, so the dearest has the lowest id: an explicit cheapest
        # sort must reach the far end of the catalog to answer correctly.
        "price_amount": Decimal(f"{10000 - product_id}.00"),
        "price_unit": "SAR",
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "3-seater-sofa",
        "commerce_category": "seating",
        "commerce_subcategory": "sofa",
        "seating_capacity": 3,
        "main_color": "Beige",
        "styles": "Modern, Minimalist",
        "length": None,
        "width": None,
        "height": None,
        "dimension_unit": None,
        "is_active": True,
        "detection": False,
    }


@pytest.fixture
async def catalog(writable_engine: AsyncEngine) -> None:
    async with writable_engine.begin() as connection:
        await connection.execute(
            core_store.insert(),
            [
                {
                    "id": STORE,
                    "uuid": uuid4(),
                    "name_english": "Store",
                    "name_arabic": None,
                    "active_status": True,
                }
            ],
        )
        await connection.execute(
            core_product.insert(), [_product(i) for i in range(1, SOFA_COUNT + 1)]
        )


class FakeEmbedder:
    model = "test-embedding-model"

    async def embed_query(self, text: str) -> tuple[float, ...]:
        return (0.1, 0.2, 0.3)


class FakeIndex:
    """Scores whatever it is handed, favouring one deliberately high id."""

    def __init__(self, favourite: int) -> None:
        self.favourite = favourite
        self.scored: list[int] = []

    async def score(self, **kwargs: Any) -> dict[int, float]:
        ids = list(kwargs["product_ids"])
        self.scored.extend(ids)
        return {i: (0.99 if i == self.favourite else 0.1) for i in ids}


def _resolved(sort: ProductSort = ProductSort.DEFAULT) -> ResolvedSearch:
    return ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating", commerce_subcategory="sofa", sort=sort
        ),
        semantic_text="a cosy reading sofa",
    )


def _build(
    session: Any, *, limit: int, index: FakeIndex | None
) -> ProductSearchPipeline:
    taxonomy = load_taxonomy()
    policy = RelaxationSettings()
    repository = ProductRepository(session)
    return ProductSearchPipeline(
        ControlledRelaxationService(
            ProductDiscoveryService(
                repository,
                taxonomy,
                DiscoverySettings(),
                load_catalog_attributes(),
                load_dimension_semantics(taxonomy=taxonomy),
            ),
            RelaxationPlanner(policy),
            policy,
        ),
        SemanticRankingService(
            cast(Any, FakeEmbedder()) if index else None,
            cast(Any, index) if index else None,
        ),
        ProductHydrationService(repository),
        presentation_limit=limit,
    )


async def _run(
    database: Database,
    *,
    limit: int = 3,
    index: FakeIndex | None = None,
    sort: ProductSort = ProductSort.DEFAULT,
) -> Any:
    async with database.session() as session:
        pipeline = _build(session, limit=limit, index=index)
        return await pipeline.execute(_resolved(sort), CONTEXT)


async def test_the_whole_catalog_is_ranked_before_three_are_shown(
    database: Database, catalog: None
) -> None:
    index = FakeIndex(BURIED)

    result = await _run(database, index=index, limit=3)

    assert result.grounding.eligible_count == SOFA_COUNT
    assert result.grounding.ranked_count == SOFA_COUNT
    assert len(index.scored) == SOFA_COUNT
    assert result.grounding.selected_count == 3
    assert result.grounding.presented_count == 3


async def test_a_high_id_winner_is_presented_first(
    database: Database, catalog: None
) -> None:
    """The exact failure the milestone removed, through the composed path."""
    result = await _run(database, index=FakeIndex(BURIED), limit=3)

    assert result.presented_product_ids[0] == BURIED
    assert result.grounding.products[0].presented_ordinal == 1
    assert result.grounding.semantic_used is True


async def test_the_pool_is_not_capped_at_fifty(
    database: Database, catalog: None
) -> None:
    result = await _run(database, index=FakeIndex(BURIED), limit=3)

    assert result.grounding.eligible_count != 50
    assert result.grounding.truncated_for_presentation is True
    assert result.grounding.stale_dropped_count == 0


async def test_an_explicit_sort_reaches_the_far_end_of_the_catalog(
    database: Database, catalog: None
) -> None:
    """Cheapest over the fifty lowest ids would be a different, wrong answer."""
    result = await _run(
        database, index=FakeIndex(1), limit=1, sort=ProductSort.PRICE_ASC
    )

    assert result.presented_product_ids == (SOFA_COUNT,)


async def test_a_degraded_ranking_still_presents_products(
    database: Database, catalog: None
) -> None:
    result = await _run(database, index=None, limit=3)

    assert result.grounding.semantic_used is False
    assert result.grounding.semantic_skip_reason is SemanticSkipReason.NOT_CONFIGURED
    assert result.grounding.ranked_count == SOFA_COUNT
    assert result.grounding.presented_count == 3


async def test_every_presented_product_carries_verified_provenance(
    database: Database, catalog: None
) -> None:
    result = await _run(database, index=FakeIndex(BURIED), limit=3)

    assert all(p.relaxation_depth == 0 for p in result.grounding.products)
    assert all(p.matched_exactly is True for p in result.grounding.products)


async def test_the_ids_and_the_grounding_describe_the_same_products(
    database: Database, catalog: None
) -> None:
    result = await _run(database, index=FakeIndex(BURIED), limit=3)

    for product_id, grounded in zip(
        result.presented_product_ids, result.grounding.products, strict=True
    ):
        assert grounded.name_english == f"Sofa {product_id}"
        assert grounded.price_amount == Decimal(f"{10000 - product_id}.00")


async def test_a_search_that_matches_nothing_presents_nothing(
    database: Database, catalog: None
) -> None:
    taxonomy = load_taxonomy()
    async with database.session() as session:
        policy = RelaxationSettings()
        repository = ProductRepository(session)
        pipeline = ProductSearchPipeline(
            ControlledRelaxationService(
                ProductDiscoveryService(
                    repository,
                    taxonomy,
                    DiscoverySettings(),
                    load_catalog_attributes(),
                    load_dimension_semantics(taxonomy=taxonomy),
                ),
                RelaxationPlanner(policy),
                policy,
            ),
            SemanticRankingService(None, None),
            ProductHydrationService(repository),
            presentation_limit=3,
        )
        result = await pipeline.execute(
            ResolvedSearch(
                request=ProductSearchRequest(
                    commerce_category="lighting", commerce_subcategory="chandelier"
                )
            ),
            CONTEXT,
        )

    assert result.grounding.outcome is SearchOutcome.ZERO_RESULTS
    assert result.presented_product_ids == ()
    assert result.grounding.eligible_count == 0
    assert result.grounding.stale_dropped_count == 0
    assert result.grounding.truncated_for_presentation is False


# ── the internal candidate pool ═════════════════════════════════════════════


async def _pool(
    database: Database, *, limit: int = 3, index: FakeIndex | None = None
) -> Any:
    async with database.session() as session:
        pipeline = _build(session, limit=limit, index=index)
        return await pipeline.execute_candidate_pool(_resolved(), CONTEXT)


async def test_the_optimiser_sees_the_whole_catalog_not_the_three_cards(
    database: Database, catalog: None
) -> None:
    """The same 173 sofas, against a presentation limit of three."""
    pool = await _pool(database, limit=3)

    assert pool.eligible_count == SOFA_COUNT
    assert len(pool.candidates) == SOFA_COUNT
    assert pool.stale_dropped_count == 0


async def test_every_candidate_is_fully_hydrated_from_postgresql(
    database: Database, catalog: None
) -> None:
    """Names, prices and commerce fields come from the database, never from an
    index or a plan (CLAUDE.md 16)."""
    pool = await _pool(database)

    first = pool.candidates[0].product
    assert first.name_english.startswith("Sofa ")
    assert first.price_unit == "SAR"
    assert first.commerce.category == "seating"
    assert first.commerce.subcategory == "sofa"


async def test_the_pool_is_scoped_to_the_context_store(
    database: Database, catalog: None
) -> None:
    async with database.session() as session:
        pipeline = _build(session, limit=3, index=None)
        elsewhere = await pipeline.execute_candidate_pool(
            _resolved(), RetailerContext(store_id=STORE + 1)
        )

    assert elsewhere.candidates == ()
    assert elsewhere.eligible_count == 0


async def test_a_semantic_favourite_leads_the_pool(
    database: Database, catalog: None
) -> None:
    """Ranking orders the complete pool, so a buried product leads it."""
    index = FakeIndex(BURIED)

    pool = await _pool(database, limit=3, index=index)

    assert pool.candidates[0].product.product_id == BURIED
    assert len(index.scored) == SOFA_COUNT
    assert pool.semantic_used is True


async def test_the_page_is_the_head_of_the_pool(
    database: Database, catalog: None
) -> None:
    """Both paths, one search: what the customer would see is the pool's front."""
    index = FakeIndex(BURIED)
    page = await _run(database, limit=3, index=index)
    pool = await _pool(database, limit=3, index=FakeIndex(BURIED))

    assert page.presented_product_ids == tuple(
        c.product.product_id for c in pool.candidates[:3]
    )


async def test_the_customer_page_is_unchanged_by_the_pool_path(
    database: Database, catalog: None
) -> None:
    """M11 regression: three products, ordinals from one, grounding intact."""
    result = await _run(database, limit=3, index=FakeIndex(BURIED))

    assert result.grounding.outcome is SearchOutcome.RESULTS
    assert len(result.presented_product_ids) == 3
    assert [p.presented_ordinal for p in result.grounding.products] == [1, 2, 3]
    assert result.grounding.eligible_count == SOFA_COUNT
