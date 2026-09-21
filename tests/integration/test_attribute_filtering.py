"""Exact colour and style filtering against real PostgreSQL.

The collision cases matter most: `Modern` must never match `Modern_Classic` or
`Rustic_Modern`, which a substring filter would get wrong.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.core.config import DiscoverySettings, RelaxationSettings
from app.core.exceptions import UnknownCatalogAttributeError
from app.db.tables import core_product, core_store
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.schemas.discovery import (
    PriceConstraint,
    ProductSearchRequest,
    ProductSearchResult,
    ProductSort,
)
from app.schemas.query import ConstraintSemantics, ConstraintStrength, ResolvedSearch
from app.schemas.retailer import RetailerContext
from app.services.controlled_search import ControlledRelaxationService
from app.services.discovery import ProductDiscoveryService
from app.services.relaxation import RelaxationPlanner
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

STORE_A = 1
STORE_B = 2
CONTEXT_A = RetailerContext(store_id=STORE_A)
CONTEXT_B = RetailerContext(store_id=STORE_B)
SAR = "SAR"


def _product(
    product_id: int,
    store_id: int,
    *,
    color: str | None,
    styles: str | None,
    price: str = "2000.00",
) -> dict[str, Any]:
    return {
        "id": product_id,
        "uuid": uuid4(),
        "store_id": store_id,
        "name_english": f"Product {product_id}",
        "name_arabic": f"منتج {product_id}",
        "price_amount": Decimal(price),
        "price_unit": SAR,
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "3-seater-sofa",
        "commerce_category": "seating",
        "commerce_subcategory": "sofa",
        "seating_capacity": 3,
        "main_color": color,
        "styles": styles,
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
                    "id": store_id,
                    "uuid": uuid4(),
                    "name_english": f"Store {store_id}",
                    "name_arabic": None,
                    "active_status": True,
                }
                for store_id in (STORE_A, STORE_B)
            ],
        )
        await connection.execute(
            core_product.insert(),
            [
                # The collision set: only id 1 is genuinely `Modern`.
                _product(1, STORE_A, color="Beige", styles="Modern, Minimalist"),
                _product(2, STORE_A, color="Taupe", styles="Modern_Classic"),
                _product(3, STORE_A, color="Grey", styles="Rustic_Modern"),
                # Both required styles present, among others.
                _product(4, STORE_A, color="Beige", styles="Modern, Contemporary, Zen"),
                # One of the two required styles only.
                _product(5, STORE_A, color="Ivory", styles="Contemporary"),
                # Missing attributes entirely.
                _product(6, STORE_A, color=None, styles=None),
                _product(7, STORE_A, color="Beige", styles=""),
                # No space after the comma: merchant formatting varies.
                _product(8, STORE_A, color="Navy", styles="Modern,Coastal"),
                _product(9, STORE_A, color="Beige", styles="Boho", price="8000.00"),
                # Another retailer with identical attributes.
                _product(20, STORE_B, color="Beige", styles="Modern, Minimalist"),
            ],
        )


async def _search(
    database: Database, request: ProductSearchRequest, context: RetailerContext
) -> ProductSearchResult:
    async with database.session() as session:
        service = ProductDiscoveryService(
            ProductRepository(session),
            load_taxonomy(),
            DiscoverySettings(),
            load_catalog_attributes(),
            load_dimension_semantics(taxonomy=load_taxonomy()),
        )
        return await service.search(request, context)


def _ids(result: ProductSearchResult) -> list[int]:
    return [c.product_id for c in result.candidates]


def _request(**kwargs: Any) -> ProductSearchRequest:
    return ProductSearchRequest(
        commerce_category="seating", commerce_subcategory="sofa", **kwargs
    )


# ── colour ──────────────────────────────────────────────────────────────────


async def test_an_exact_colour_filter_matches_only_that_colour(
    database: Database, catalog: None
) -> None:
    result = await _search(database, _request(colors_any_of=("Beige",)), CONTEXT_A)

    assert _ids(result) == [1, 4, 7, 9]


async def test_a_null_colour_never_satisfies_a_colour_requirement(
    database: Database, catalog: None
) -> None:
    result = await _search(database, _request(colors_any_of=("Beige",)), CONTEXT_A)

    assert 6 not in _ids(result)


async def test_several_colours_are_alternatives(
    database: Database, catalog: None
) -> None:
    result = await _search(
        database, _request(colors_any_of=("Taupe", "Navy")), CONTEXT_A
    )

    assert _ids(result) == [2, 8]


# ── style token membership ──────────────────────────────────────────────────


async def test_an_exact_style_matches_by_token(
    database: Database, catalog: None
) -> None:
    result = await _search(database, _request(styles_all_of=("Modern",)), CONTEXT_A)

    assert _ids(result) == [1, 4, 8]


async def test_modern_does_not_match_modern_classic(
    database: Database, catalog: None
) -> None:
    """A substring filter would wrongly include product 2."""
    result = await _search(database, _request(styles_all_of=("Modern",)), CONTEXT_A)

    assert 2 not in _ids(result)


async def test_modern_does_not_match_rustic_modern(
    database: Database, catalog: None
) -> None:
    """A substring filter would wrongly include product 3."""
    result = await _search(database, _request(styles_all_of=("Modern",)), CONTEXT_A)

    assert 3 not in _ids(result)


async def test_the_colliding_styles_are_each_reachable_exactly(
    database: Database, catalog: None
) -> None:
    for style, expected in (
        ("Modern_Classic", [2]),
        ("Rustic_Modern", [3]),
    ):
        result = await _search(database, _request(styles_all_of=(style,)), CONTEXT_A)
        assert _ids(result) == expected, style


async def test_style_matching_survives_missing_comma_spacing(
    database: Database, catalog: None
) -> None:
    """Merchants store "Modern, Coastal" and "Modern,Coastal" alike."""
    result = await _search(database, _request(styles_all_of=("Coastal",)), CONTEXT_A)

    assert _ids(result) == [8]


async def test_several_styles_are_all_required(
    database: Database, catalog: None
) -> None:
    result = await _search(
        database, _request(styles_all_of=("Modern", "Contemporary")), CONTEXT_A
    )

    assert _ids(result) == [4]
    assert 1 not in _ids(result)  # Modern but not Contemporary
    assert 5 not in _ids(result)  # Contemporary but not Modern


@pytest.mark.parametrize("missing_id", [6, 7])
async def test_null_or_empty_styles_never_satisfy_a_style_requirement(
    database: Database, catalog: None, missing_id: int
) -> None:
    result = await _search(database, _request(styles_all_of=("Modern",)), CONTEXT_A)

    assert missing_id not in _ids(result)


# ── combination and scope ───────────────────────────────────────────────────


async def test_attributes_combine_with_the_other_structured_filters(
    database: Database, catalog: None
) -> None:
    result = await _search(
        database,
        _request(
            colors_any_of=("Beige",),
            styles_all_of=("Modern",),
            price=PriceConstraint.at_most(Decimal("3000"), SAR),
        ),
        CONTEXT_A,
    )

    assert _ids(result) == [1, 4]


async def test_attribute_filtering_respects_retailer_scope(
    database: Database, catalog: None
) -> None:
    from_a = await _search(
        database, _request(colors_any_of=("Beige",), styles_all_of=("Modern",)), CONTEXT_A
    )
    from_b = await _search(
        database, _request(colors_any_of=("Beige",), styles_all_of=("Modern",)), CONTEXT_B
    )

    assert 20 not in _ids(from_a)
    assert _ids(from_b) == [20]


async def test_sorting_is_unaffected_by_attribute_filters(
    database: Database, catalog: None
) -> None:
    result = await _search(
        database,
        _request(colors_any_of=("Beige",), sort=ProductSort.PRICE_DESC),
        CONTEXT_A,
    )

    prices = [c.price_amount for c in result.candidates]
    assert prices == sorted(prices, reverse=True)
    assert _ids(result)[0] == 9


async def test_an_unapproved_attribute_never_reaches_sql(
    database: Database, catalog: None
) -> None:
    with pytest.raises(UnknownCatalogAttributeError):
        await _search(database, _request(colors_any_of=("Crimson",)), CONTEXT_A)
    with pytest.raises(UnknownCatalogAttributeError):
        await _search(database, _request(styles_all_of=("Brutalist",)), CONTEXT_A)


async def test_a_colour_offered_as_a_style_is_rejected(
    database: Database, catalog: None
) -> None:
    with pytest.raises(UnknownCatalogAttributeError):
        await _search(database, _request(styles_all_of=("Beige",)), CONTEXT_A)
    with pytest.raises(UnknownCatalogAttributeError):
        await _search(database, _request(colors_any_of=("Modern",)), CONTEXT_A)


# ── relaxation preserves them ───────────────────────────────────────────────


async def _relaxed(
    database: Database, resolved: ResolvedSearch, context: RetailerContext
) -> Any:
    async with database.session() as session:
        policy = RelaxationSettings()
        service = ControlledRelaxationService(
            ProductDiscoveryService(
                ProductRepository(session),
                load_taxonomy(),
                DiscoverySettings(),
                load_catalog_attributes(),
                load_dimension_semantics(taxonomy=load_taxonomy()),
            ),
            RelaxationPlanner(policy),
            policy,
        )
        return await service.search(resolved, context)


async def test_a_strict_colour_survives_every_price_widening(
    database: Database, catalog: None
) -> None:
    result = await _relaxed(
        database,
        ResolvedSearch(
            request=_request(
                colors_any_of=("Beige",),
                price=PriceConstraint.at_most(Decimal("2000"), SAR),
            ),
            semantics=ConstraintSemantics(price_max=ConstraintStrength.APPROXIMATE),
        ),
        CONTEXT_A,
    )

    assert result.relaxation_attempt_count > 0
    assert all(a.request.colors_any_of == ("Beige",) for a in result.attempts)
    # Beige products within reach of the widened ceiling, and nothing else:
    # 9 is Beige but costs 8000, and 2, 3, 5, 8 are other colours that a wider
    # price bound must never let in.
    assert {c.product.product_id for c in result.candidates} == {1, 4, 7}


async def test_a_strict_style_survives_every_widening(
    database: Database, catalog: None
) -> None:
    result = await _relaxed(
        database,
        ResolvedSearch(
            request=_request(
                styles_all_of=("Modern",),
                price=PriceConstraint.at_most(Decimal("2000"), SAR),
            ),
            semantics=ConstraintSemantics(price_max=ConstraintStrength.APPROXIMATE),
        ),
        CONTEXT_A,
    )

    assert all(a.request.styles_all_of == ("Modern",) for a in result.attempts)
    assert 2 not in [c.product.product_id for c in result.candidates]
    assert 3 not in [c.product.product_id for c in result.candidates]


async def test_relaxation_never_adds_an_attribute_requirement(
    database: Database, catalog: None
) -> None:
    result = await _relaxed(
        database,
        ResolvedSearch(
            request=_request(price=PriceConstraint.at_most(Decimal("2000"), SAR)),
            semantics=ConstraintSemantics(price_max=ConstraintStrength.APPROXIMATE),
        ),
        CONTEXT_A,
    )

    assert all(a.request.colors_any_of == () for a in result.attempts)
    assert all(a.request.styles_all_of == () for a in result.attempts)
