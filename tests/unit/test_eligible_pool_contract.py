"""`eligible_pool`: complete by construction, and validated like `search`.

The bug this contract exists to prevent is subtle because everything looks
fine: a bounded page of 50 arrives, ranking orders it correctly, and the
customer is shown the best three of an arbitrary fifty. So the tests here are
mostly about what the method *cannot* do - accept a limit, skip validation, or
diverge from the eligibility `search` applies.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from typing import Any, cast

import pytest
from app.core.config import DiscoverySettings
from app.core.exceptions import (
    UnknownCatalogAttributeError,
    UnknownCommerceCategoryError,
    UnsupportedDimensionRoleError,
)
from app.repositories.products import ProductRepository
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    ProductSearchRequest,
)
from app.schemas.product import EligibleProduct
from app.schemas.retailer import RetailerContext
from app.services.discovery import ProductDiscoveryService
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import DimensionRole, load_dimension_semantics
from app.taxonomy.registry import load_taxonomy

CONTEXT = RetailerContext(store_id=50)


class RecordingRepository:
    """Records exactly what the pool query was asked for."""

    def __init__(self, pool: list[EligibleProduct] | None = None) -> None:
        self.pool = pool if pool is not None else []
        self.calls: list[dict[str, Any]] = []

    async def search_eligible_pool(
        self,
        request: ProductSearchRequest,
        context: RetailerContext,
        **resolved: Any,
    ) -> list[EligibleProduct]:
        self.calls.append({"request": request, "context": context, **resolved})
        return list(self.pool)


def _service(repository: RecordingRepository, **settings: int) -> ProductDiscoveryService:
    taxonomy = load_taxonomy()
    return ProductDiscoveryService(
        cast(ProductRepository, repository),
        taxonomy,
        DiscoverySettings(**settings),
        load_catalog_attributes(),
        load_dimension_semantics(taxonomy=taxonomy),
    )


def _pool(*ids: int) -> list[EligibleProduct]:
    return [EligibleProduct(product_id=i, price_amount=Decimal("1000")) for i in ids]


# ── no bound, anywhere ──────────────────────────────────────────────────────


def test_the_method_accepts_no_limit_argument() -> None:
    """A signature that cannot express a bound cannot acquire one by accident."""
    parameters = inspect.signature(ProductDiscoveryService.eligible_pool).parameters

    assert set(parameters) == {"self", "request", "context"}


async def test_the_configured_limits_are_never_consulted() -> None:
    """A pool of 60 must survive a default candidate limit of 1."""
    repository = RecordingRepository(_pool(*range(1, 61)))

    pool = await _service(
        repository, default_candidate_limit=1, max_candidate_limit=1
    ).eligible_pool(ProductSearchRequest(commerce_category="seating"), CONTEXT)

    assert len(pool) == 60
    assert "limit" not in repository.calls[0]


async def test_a_request_limit_is_not_applied_to_the_pool() -> None:
    """`limit` is presentation; the pool it is ranked from is not presentation."""
    repository = RecordingRepository(_pool(1, 2, 3, 4, 5))

    pool = await _service(repository).eligible_pool(
        ProductSearchRequest(commerce_category="seating", limit=2), CONTEXT
    )

    assert len(pool) == 5


# ── the same gate as `search` ───────────────────────────────────────────────


async def test_an_unapproved_category_fails_before_any_query() -> None:
    repository = RecordingRepository()

    with pytest.raises(UnknownCommerceCategoryError):
        await _service(repository).eligible_pool(
            ProductSearchRequest(commerce_category="living-room-furniture"), CONTEXT
        )

    assert repository.calls == []


async def test_an_unapproved_colour_fails_before_any_query() -> None:
    repository = RecordingRepository()

    with pytest.raises(UnknownCatalogAttributeError):
        await _service(repository).eligible_pool(
            ProductSearchRequest(
                commerce_category="seating", colors_any_of=("Neon",)
            ),
            CONTEXT,
        )

    assert repository.calls == []


async def test_an_unsupported_dimension_role_fails_before_any_query() -> None:
    repository = RecordingRepository()
    request = ProductSearchRequest(
        commerce_category="bedroom",
        commerce_subcategory="bed",
        dimensions=(
            DimensionConstraint(
                role=DimensionRole.OVERALL_WIDTH,
                kind=DimensionConstraintKind.MAX,
                max_cm=Decimal("200"),
            ),
        ),
    )

    with pytest.raises(UnsupportedDimensionRoleError):
        await _service(repository).eligible_pool(request, CONTEXT)

    assert repository.calls == []


async def test_resolved_axes_reach_the_repository() -> None:
    """Roles become columns through the registry here too, not only in `search`."""
    repository = RecordingRepository()
    request = ProductSearchRequest(
        commerce_category="seating",
        commerce_subcategory="sofa",
        dimensions=(
            DimensionConstraint(
                role=DimensionRole.OVERALL_WIDTH,
                kind=DimensionConstraintKind.MAX,
                max_cm=Decimal("220"),
            ),
        ),
    )

    await _service(repository).eligible_pool(request, CONTEXT)

    assert repository.calls[0]["axis_constraints"]


# ── scope ───────────────────────────────────────────────────────────────────


async def test_the_context_is_passed_through_untouched() -> None:
    repository = RecordingRepository()

    await _service(repository).eligible_pool(
        ProductSearchRequest(commerce_category="seating"), CONTEXT
    )

    assert repository.calls[0]["context"] is CONTEXT


# ── the existing M9B-2 contract is untouched ────────────────────────────────


def test_search_eligible_ids_still_exists_and_takes_no_limit() -> None:
    """Renaming or repurposing it would break the M9B-2 contract."""
    parameters = inspect.signature(ProductRepository.search_eligible_ids).parameters

    assert "limit" not in parameters
    assert set(parameters) >= {"self", "request", "context"}


def test_the_pool_query_also_takes_no_limit() -> None:
    parameters = inspect.signature(ProductRepository.search_eligible_pool).parameters

    assert "limit" not in parameters
