"""The M11B-1 repairs against real PostgreSQL.

Strict comparators, product exclusion, the lightweight pool projection and
style-token parity. These are the pieces a later phase builds "cheaper than
this one" and "something similar to this" on, so they are proved against the
database rather than against a fake that would agree with whatever we wrote.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.db.tables import core_product, core_store
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.schemas.discovery import PriceConstraint, ProductSearchRequest, ProductSort
from app.schemas.product import parse_style_tokens
from app.schemas.retailer import RetailerContext
from sqlalchemy import func, select, type_coerce
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.sqltypes import Text

pytestmark = pytest.mark.integration

STORE_A, STORE_B = 1, 2
CONTEXT_A = RetailerContext(store_id=STORE_A)
CONTEXT_B = RetailerContext(store_id=STORE_B)

# Prices chosen so that an inclusive and an exclusive bound at 2000 differ.
PRICES = {1: "1000.00", 2: "2000.00", 3: "2000.00", 4: "3000.00", 5: "5000.00"}
STYLES = {
    1: "Modern, Minimalist",
    2: "Modern,Coastal",
    3: "  Modern ,  Zen  ",
    4: None,
    5: "",
}


def _product(product_id: int, store_id: int) -> dict[str, Any]:
    return {
        "id": product_id,
        "uuid": uuid4(),
        "store_id": store_id,
        "name_english": f"Sofa {product_id}",
        "name_arabic": f"أريكة {product_id}",
        "price_amount": Decimal(PRICES.get(product_id, "2000.00")),
        "price_unit": "SAR",
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "3-seater-sofa",
        "commerce_category": "seating",
        "commerce_subcategory": "sofa",
        "seating_capacity": 3,
        "main_color": "Beige",
        "styles": STYLES.get(product_id, "Modern"),
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
                    "id": s,
                    "uuid": uuid4(),
                    "name_english": f"Store {s}",
                    "name_arabic": None,
                    "active_status": True,
                }
                for s in (STORE_A, STORE_B)
            ],
        )
        rows = [_product(i, STORE_A) for i in PRICES]
        rows.append(_product(99, STORE_B))
        await connection.execute(core_product.insert(), rows)


def _request(**kwargs: Any) -> ProductSearchRequest:
    return ProductSearchRequest(
        commerce_category="seating", commerce_subcategory="sofa", **kwargs
    )


async def _pool_ids(
    database: Database, request: ProductSearchRequest, context: RetailerContext
) -> list[int]:
    async with database.session() as session:
        pool = await ProductRepository(session).search_eligible_pool(request, context)
    return [p.product_id for p in pool]


# ── strict comparators ──────────────────────────────────────────────────────


async def test_an_inclusive_ceiling_keeps_products_at_the_bound(
    database: Database, catalog: None
) -> None:
    request = _request(price=PriceConstraint.at_most(Decimal("2000"), "SAR"))

    assert await _pool_ids(database, request, CONTEXT_A) == [1, 2, 3]


async def test_an_exclusive_ceiling_drops_products_at_the_bound(
    database: Database, catalog: None
) -> None:
    """"Cheaper than 2000" must not return the products that cost 2000."""
    request = _request(price=PriceConstraint.below(Decimal("2000"), "SAR"))

    assert await _pool_ids(database, request, CONTEXT_A) == [1]


async def test_an_inclusive_floor_keeps_products_at_the_bound(
    database: Database, catalog: None
) -> None:
    request = _request(
        price=PriceConstraint(currency="SAR", min_amount=Decimal("2000"))
    )

    assert await _pool_ids(database, request, CONTEXT_A) == [2, 3, 4, 5]


async def test_an_exclusive_floor_drops_products_at_the_bound(
    database: Database, catalog: None
) -> None:
    request = _request(price=PriceConstraint.above(Decimal("2000"), "SAR"))

    assert await _pool_ids(database, request, CONTEXT_A) == [4, 5]


async def test_strict_comparators_reach_the_presentation_query_too(
    database: Database, catalog: None
) -> None:
    """`search` and the pool share one set of eligibility clauses."""
    request = _request(price=PriceConstraint.below(Decimal("2000"), "SAR"))
    async with database.session() as session:
        rows = await ProductRepository(session).search(request, CONTEXT_A, limit=50)

    assert [r.id for r in rows] == [1]


# ── product exclusion ───────────────────────────────────────────────────────


async def test_an_excluded_product_is_not_eligible(
    database: Database, catalog: None
) -> None:
    assert await _pool_ids(database, _request(exclude_product_ids=(2, 4)), CONTEXT_A) == [
        1,
        3,
        5,
    ]


async def test_exclusion_happens_before_the_presentation_limit(
    database: Database, catalog: None
) -> None:
    """Excluding after a bounded page would silently return fewer products.

    With id 1 excluded and a limit of 2, a pre-limit exclusion returns two
    products; a post-limit filter would return one.
    """
    request = _request(exclude_product_ids=(1,))
    async with database.session() as session:
        rows = await ProductRepository(session).search(request, CONTEXT_A, limit=2)

    assert [r.id for r in rows] == [2, 3]


async def test_exclusion_cannot_reach_another_retailer(
    database: Database, catalog: None
) -> None:
    """Store scope still decides; exclusion only narrows within it."""
    assert await _pool_ids(database, _request(exclude_product_ids=(99,)), CONTEXT_B) == []
    assert await _pool_ids(database, _request(), CONTEXT_B) == [99]


# ── the pool projection ─────────────────────────────────────────────────────


async def test_the_pool_carries_the_price_ranking_sorts_on(
    database: Database, catalog: None
) -> None:
    async with database.session() as session:
        pool = await ProductRepository(session).search_eligible_pool(
            _request(), CONTEXT_A
        )

    assert {p.product_id: p.price_amount for p in pool} == {
        i: Decimal(PRICES[i]) for i in PRICES
    }


async def test_the_pool_honours_an_explicit_sort(
    database: Database, catalog: None
) -> None:
    """Ranking may be skipped, and then this order is what the customer sees."""
    ascending = await _pool_ids(
        database, _request(sort=ProductSort.PRICE_ASC), CONTEXT_A
    )
    descending = await _pool_ids(
        database, _request(sort=ProductSort.PRICE_DESC), CONTEXT_A
    )

    assert ascending == [1, 2, 3, 4, 5]
    assert descending == [5, 4, 2, 3, 1]


async def test_the_pool_agrees_with_both_existing_eligibility_paths(
    database: Database, catalog: None
) -> None:
    """One definition of eligibility, three projections of it."""
    request = _request(price=PriceConstraint.below(Decimal("3000"), "SAR"))
    async with database.session() as session:
        repo = ProductRepository(session)
        ids = await repo.search_eligible_ids(request, CONTEXT_A)
        pool = await repo.search_eligible_pool(request, CONTEXT_A)
        shown = await repo.search(request, CONTEXT_A, limit=500)

    assert sorted(ids) == sorted(p.product_id for p in pool)
    assert sorted(ids) == sorted(r.id for r in shown)


# ── style tokens, across the language boundary ──────────────────────────────


async def test_python_tokens_match_what_the_sql_matcher_sees(
    database: Database, catalog: None
) -> None:
    """The real parity check: one behavioural definition, two languages.

    Compares the projection's tokens against PostgreSQL's own evaluation of
    the matcher expression, over rows whose spacing deliberately varies.
    """
    sql_tokens = type_coerce(
        func.string_to_array(func.replace(core_product.c.styles, " ", ""), ","),
        ARRAY(Text),
    )
    async with database.session() as session:
        rows = (
            await session.execute(
                select(core_product.c.id, core_product.c.styles, sql_tokens).where(
                    core_product.c.store_id == STORE_A
                )
            )
        ).all()

    assert rows
    for row in rows:
        database_tokens = tuple(t for t in (row[2] or ()) if t)
        assert parse_style_tokens(row.styles) == database_tokens, row.id


async def test_the_projection_reports_colour_and_styles(
    database: Database, catalog: None
) -> None:
    async with database.session() as session:
        rows = await ProductRepository(session).get_by_ids([1, 4], CONTEXT_A)

    by_id = {r.id: r for r in rows}
    assert by_id[1].main_color == "Beige"
    assert by_id[1].styles == ("Modern", "Minimalist")
    assert by_id[4].styles == ()


# ── exclusion and exclusivity through real relaxation ───────────────────────


async def _relaxed(
    database: Database, resolved: Any, context: RetailerContext
) -> Any:
    from app.core.config import DiscoverySettings, RelaxationSettings
    from app.services.controlled_search import ControlledRelaxationService
    from app.services.discovery import ProductDiscoveryService
    from app.services.relaxation import RelaxationPlanner
    from app.taxonomy.attributes import load_catalog_attributes
    from app.taxonomy.dimensions import load_dimension_semantics
    from app.taxonomy.registry import load_taxonomy

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
        return await service.search(resolved, context)


def _approximate_price(max_amount: str, **request_kwargs: Any) -> Any:
    from app.schemas.query import (
        ConstraintSemantics,
        ConstraintStrength,
        ResolvedSearch,
    )

    return ResolvedSearch(
        request=_request(
            price=PriceConstraint(currency="SAR", max_amount=Decimal(max_amount)),
            **request_kwargs,
        ),
        semantics=ConstraintSemantics(price_max=ConstraintStrength.APPROXIMATE),
    )


async def test_an_excluded_product_never_returns_at_any_relaxation_depth(
    database: Database, catalog: None
) -> None:
    """The similar-alternative guarantee, end to end.

    Product 4 costs 3000 and is excluded. Widening from 2500 to 2750 and then
    3000 would make it eligible on price at the final depth - which is exactly
    where a dropped exclusion used to reappear.
    """
    result = await _relaxed(
        database, _approximate_price("2500", exclude_product_ids=(4,)), CONTEXT_A
    )

    assert result.relaxation_attempt_count > 0
    assert all(a.request.exclude_product_ids == (4,) for a in result.attempts)
    assert 4 not in {c.product.product_id for c in result.candidates}


async def test_the_same_product_is_returned_when_it_is_not_excluded(
    database: Database, catalog: None
) -> None:
    """Proves the test above is not passing because 4 was unreachable anyway."""
    result = await _relaxed(database, _approximate_price("2500"), CONTEXT_A)

    assert 4 in {c.product.product_id for c in result.candidates}


async def test_every_attempt_reads_the_complete_pool(
    database: Database, catalog: None
) -> None:
    """`eligible_count` is the catalog's answer, not a page of it."""
    result = await _relaxed(database, _approximate_price("2000"), CONTEXT_A)

    assert result.exact_candidate_count == 3
    assert all(a.eligible_count >= 3 for a in result.attempts)
