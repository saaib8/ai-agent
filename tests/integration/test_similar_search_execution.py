"""A similar-search seed against real PostgreSQL.

The property that matters end to end: the product the customer pointed at must
not come back as its own alternative — and must stay out at every relaxation
depth, including the ones wide enough to make it eligible again. M11B-1 proved
the exclusion primitive; this proves a seed actually carries it there.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.core.config import DiscoverySettings, RelaxationSettings
from app.db.tables import core_product, core_store
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.schemas.query import ConstraintSemantics, ConstraintStrength, ResolvedSearch
from app.schemas.resolution import SimilarSearchSeed
from app.schemas.retailer import RetailerContext
from app.services.controlled_search import ControlledRelaxationService
from app.services.discovery import ProductDiscoveryService, to_candidate
from app.services.relaxation import RelaxationPlanner
from app.services.similar_search import SimilarSearchBuilder
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

STORE = 1
CONTEXT = RetailerContext(store_id=STORE)
REFERENCE = 1
PRICES = {1: "2000.00", 2: "2000.00", 3: "2200.00", 4: "2400.00", 5: "9000.00"}


def _product(product_id: int) -> dict[str, Any]:
    return {
        "id": product_id,
        "uuid": uuid4(),
        "store_id": STORE,
        "name_english": f"Sofa {product_id}",
        "name_arabic": f"أريكة {product_id}",
        "price_amount": Decimal(PRICES[product_id]),
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
            core_product.insert(), [_product(i) for i in PRICES]
        )


async def _seed(database: Database) -> SimilarSearchSeed:
    taxonomy = load_taxonomy()
    async with database.session() as session:
        rows = await ProductRepository(session).get_by_ids([REFERENCE], CONTEXT)
    builder = SimilarSearchBuilder(taxonomy, load_catalog_attributes())
    outcome = builder.build(to_candidate(rows[0]))
    assert isinstance(outcome, SimilarSearchSeed)
    return outcome


async def _run(database: Database, resolved: ResolvedSearch) -> Any:
    taxonomy = load_taxonomy()
    async with database.session() as session:
        policy = RelaxationSettings()
        service = ControlledRelaxationService(
            ProductDiscoveryService(
                ProductRepository(session),
                taxonomy,
                DiscoverySettings(),
                load_catalog_attributes(),
                load_dimension_semantics(taxonomy=taxonomy),
            ),
            RelaxationPlanner(policy),
            policy,
        )
        return await service.search(resolved, CONTEXT)


async def test_a_seed_is_built_from_the_reference_product(
    database: Database, catalog: None
) -> None:
    seed = await _seed(database)

    assert seed.reference_product_id == REFERENCE
    assert seed.resolved.request.commerce_subcategory == "sofa"
    assert seed.resolved.request.exclude_product_ids == (REFERENCE,)
    assert {p.canonical_value for p in seed.resolved.semantic_preferences} == {
        "Beige",
        "Modern",
        "Minimalist",
    }


async def test_the_reference_never_returns_as_its_own_alternative(
    database: Database, catalog: None
) -> None:
    seed = await _seed(database)

    result = await _run(database, seed.resolved)

    assert REFERENCE not in {c.product.product_id for c in result.candidates}
    assert result.candidates


async def test_the_exclusion_holds_at_every_relaxation_depth(
    database: Database, catalog: None
) -> None:
    """The seat count is approximate, so M8 widens — and the reference stays out.

    Without the exclusion travelling through `_build`, product 1 would become
    eligible again the moment the capacity widened.
    """
    seed = await _seed(database)

    result = await _run(database, seed.resolved)

    assert result.relaxation_attempt_count > 0
    assert all(a.request.exclude_product_ids == (REFERENCE,) for a in result.attempts)
    assert REFERENCE not in {c.product.product_id for c in result.candidates}


async def test_an_identically_specified_product_is_still_returned(
    database: Database, catalog: None
) -> None:
    """Proves the test above is not passing because nothing matched at all.

    Product 2 is the reference's twin in every respect except its id.
    """
    seed = await _seed(database)

    result = await _run(database, seed.resolved)

    assert 2 in {c.product.product_id for c in result.candidates}


async def test_colour_and_style_never_became_filters(
    database: Database, catalog: None
) -> None:
    seed = await _seed(database)

    assert seed.resolved.request.colors_any_of == ()
    assert seed.resolved.request.styles_all_of == ()


async def test_the_seat_count_is_carried_as_a_soft_requirement(
    database: Database, catalog: None
) -> None:
    """They asked for something *like* this, not for exactly three seats."""
    seed = await _seed(database)
    capacity = seed.resolved.request.seating_capacity

    assert capacity is not None
    assert (capacity.min_capacity, capacity.max_capacity) == (3, 3)
    assert seed.resolved.semantics.seating_min is ConstraintStrength.APPROXIMATE
    assert ConstraintSemantics().seating_min is None
