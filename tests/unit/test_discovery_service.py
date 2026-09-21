"""Discovery orchestration: taxonomy gate, bounding, and safe mapping."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from app.core.config import DiscoverySettings
from app.core.exceptions import (
    InvalidRequestError,
    UnknownCommerceCategoryError,
    UnknownCommerceSubcategoryError,
)
from app.repositories.products import ProductRepository
from app.schemas.dimensions import DimensionStatus, RawDimensions
from app.schemas.discovery import (
    PriceConstraint,
    ProductSearchRequest,
    ProductSort,
    SeatingCapacityConstraint,
)
from app.schemas.product import CommerceClassification, ProductRow
from app.schemas.retailer import RetailerContext
from app.services.discovery import ProductDiscoveryService
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy

CONTEXT = RetailerContext(store_id=50)


def _row(product_id: int, *, unit: str | None = "cm") -> ProductRow:
    return ProductRow(
        id=product_id,
        uuid=uuid4(),
        store_id=50,
        name_english=f"Sofa {product_id}",
        name_arabic=f"كنبة {product_id}",
        price_amount=Decimal("2450.00"),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        visual_category="3-seater-sofa",
        commerce=CommerceClassification(
            category="seating", subcategory="sofa", seating_capacity=3
        ),
        dimensions=RawDimensions(
            length=Decimal("90"), width=Decimal("220"), height=Decimal("85"), unit=unit
        ),
        main_color="Beige",
        styles=("Modern", "Minimalist"),
        is_active=True,
    )


class RecordingRepository:
    """Captures what the service asked for, and returns canned rows."""

    def __init__(self, rows: list[ProductRow] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.calls: list[dict[str, Any]] = []

    async def search(
        self,
        request: ProductSearchRequest,
        context: RetailerContext,
        *,
        limit: int,
        **resolved: Any,
    ) -> list[ProductRow]:
        self.calls.append(
            {"request": request, "context": context, "limit": limit, **resolved}
        )
        return self.rows[:limit]


def _service(
    repository: RecordingRepository, **settings: int
) -> ProductDiscoveryService:
    return ProductDiscoveryService(
        cast(ProductRepository, repository),
        load_taxonomy(),
        DiscoverySettings(**settings),
        load_catalog_attributes(),
        load_dimension_semantics(taxonomy=load_taxonomy()),
    )


# ── taxonomy gate ───────────────────────────────────────────────────────────


async def test_an_unapproved_category_fails_before_any_query() -> None:
    repository = RecordingRepository()

    with pytest.raises(UnknownCommerceCategoryError):
        await _service(repository).search(
            ProductSearchRequest(commerce_category="living-room-furniture"), CONTEXT
        )

    assert repository.calls == []


async def test_an_unapproved_subcategory_fails_before_any_query() -> None:
    repository = RecordingRepository()

    with pytest.raises(UnknownCommerceSubcategoryError):
        await _service(repository).search(
            ProductSearchRequest(
                commerce_category="seating", commerce_subcategory="luxury-couch"
            ),
            CONTEXT,
        )

    assert repository.calls == []


async def test_a_subcategory_under_the_wrong_category_fails_before_any_query() -> None:
    repository = RecordingRepository()

    with pytest.raises(UnknownCommerceSubcategoryError):
        await _service(repository).search(
            ProductSearchRequest(
                commerce_category="lighting", commerce_subcategory="sofa"
            ),
            CONTEXT,
        )

    assert repository.calls == []


@pytest.mark.parametrize("superseded", ["l-shape-sofa", "side-table", "lampshade"])
async def test_superseded_tokens_never_reach_sql(superseded: str) -> None:
    repository = RecordingRepository()

    with pytest.raises(UnknownCommerceSubcategoryError):
        await _service(repository).search(
            ProductSearchRequest(
                commerce_category="seating", commerce_subcategory=superseded
            ),
            CONTEXT,
        )

    assert repository.calls == []


async def test_an_approved_category_without_a_subcategory_is_accepted() -> None:
    repository = RecordingRepository([_row(1)])

    result = await _service(repository).search(
        ProductSearchRequest(commerce_category="seating"), CONTEXT
    )

    assert len(result.candidates) == 1


# ── bounding ────────────────────────────────────────────────────────────────


async def test_an_unset_limit_uses_the_configured_default() -> None:
    repository = RecordingRepository()

    result = await _service(repository, default_candidate_limit=25).search(
        ProductSearchRequest(commerce_category="seating"), CONTEXT
    )

    assert result.limit == 25
    # One extra row is fetched to detect truncation without a second query.
    assert repository.calls[0]["limit"] == 26


async def test_a_requested_limit_is_honoured() -> None:
    repository = RecordingRepository()

    result = await _service(repository).search(
        ProductSearchRequest(commerce_category="seating", limit=5), CONTEXT
    )

    assert result.limit == 5


async def test_a_limit_above_the_configured_maximum_is_rejected() -> None:
    repository = RecordingRepository()

    with pytest.raises(InvalidRequestError):
        await _service(repository, max_candidate_limit=100).search(
            ProductSearchRequest(commerce_category="seating", limit=5000), CONTEXT
        )

    assert repository.calls == []


async def test_the_limit_rejection_reveals_no_internals() -> None:
    repository = RecordingRepository()

    with pytest.raises(InvalidRequestError) as caught:
        await _service(repository, max_candidate_limit=100).search(
            ProductSearchRequest(commerce_category="seating", limit=5000), CONTEXT
        )

    assert "5000" not in caught.value.public_message
    assert caught.value.context["max_candidate_limit"] == 100


async def test_results_are_capped_and_truncation_is_reported() -> None:
    repository = RecordingRepository([_row(i) for i in range(1, 11)])

    result = await _service(repository, default_candidate_limit=3).search(
        ProductSearchRequest(commerce_category="seating"), CONTEXT
    )

    assert len(result.candidates) == 3
    assert result.truncated is True


async def test_an_exact_fit_is_not_reported_as_truncated() -> None:
    repository = RecordingRepository([_row(i) for i in range(1, 4)])

    result = await _service(repository, default_candidate_limit=3).search(
        ProductSearchRequest(commerce_category="seating"), CONTEXT
    )

    assert len(result.candidates) == 3
    assert result.truncated is False


# ── no relaxation, no ranking ───────────────────────────────────────────────


async def test_no_results_returns_an_empty_result_without_widening() -> None:
    """Relaxation is a later milestone: one query, and that is the answer."""
    repository = RecordingRepository([])

    result = await _service(repository).search(
        ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="recliner",
            price=PriceConstraint.at_most(Decimal("1"), "SAR"),
            seating_capacity=SeatingCapacityConstraint.exactly(9),
        ),
        CONTEXT,
    )

    assert result.candidates == ()
    assert result.truncated is False
    assert len(repository.calls) == 1
    # The constraints reached the repository exactly as supplied.
    issued = repository.calls[0]["request"]
    assert issued.price is not None
    assert issued.seating_capacity is not None


async def test_candidates_carry_no_relevance_score() -> None:
    repository = RecordingRepository([_row(1)])

    result = await _service(repository).search(
        ProductSearchRequest(commerce_category="seating"), CONTEXT
    )

    assert "score" not in result.candidates[0].model_dump()


# ── safe mapping ────────────────────────────────────────────────────────────


async def test_candidates_expose_no_internal_fields() -> None:
    repository = RecordingRepository([_row(1)])

    result = await _service(repository).search(
        ProductSearchRequest(commerce_category="seating"), CONTEXT
    )
    payload = result.candidates[0].model_dump()

    for forbidden in (
        "store_id", "pinecone_id", "file_id", "is_active",
        "visual_category", "category", "uuid",
    ):
        assert forbidden not in payload, forbidden


async def test_candidates_carry_the_reviewed_commerce_facts() -> None:
    repository = RecordingRepository([_row(7)])

    result = await _service(repository).search(
        ProductSearchRequest(commerce_category="seating"), CONTEXT
    )
    candidate = result.candidates[0]

    assert candidate.product_id == 7
    assert candidate.commerce.category == "seating"
    assert candidate.commerce.subcategory == "sofa"
    assert candidate.commerce.seating_capacity == 3
    assert candidate.price_amount == Decimal("2450.00")
    assert candidate.price_unit == "SAR"


async def test_candidate_dimensions_are_normalised() -> None:
    repository = RecordingRepository([_row(1)])

    result = await _service(repository).search(
        ProductSearchRequest(commerce_category="seating"), CONTEXT
    )

    dimensions = result.candidates[0].dimensions
    assert dimensions.status is DimensionStatus.NORMALISED
    assert dimensions.width_cm == Decimal("220")


async def test_an_unusable_catalog_unit_is_reported_not_guessed() -> None:
    repository = RecordingRepository([_row(1, unit=None)])

    result = await _service(repository).search(
        ProductSearchRequest(commerce_category="seating"), CONTEXT
    )

    dimensions = result.candidates[0].dimensions
    assert dimensions.status is DimensionStatus.UNKNOWN_UNIT
    assert dimensions.width_cm is None


# ── boundaries ──────────────────────────────────────────────────────────────


async def test_the_service_passes_context_through_untouched() -> None:
    repository = RecordingRepository()

    await _service(repository).search(
        ProductSearchRequest(commerce_category="seating", sort=ProductSort.PRICE_ASC),
        CONTEXT,
    )

    assert repository.calls[0]["context"] is CONTEXT


async def test_the_service_owns_no_sql_and_no_vocabulary() -> None:
    """Two retrieval methods and nothing else; taxonomy and SQL belong elsewhere.

    `search` is the bounded presentation query M6 has always exposed;
    `eligible_pool` is the unbounded one the conversational path ranks over.
    Any third public name means a responsibility arrived here that belongs
    somewhere else.
    """
    surface = {n for n in dir(ProductDiscoveryService) if not n.startswith("_")}
    assert surface == {"search", "eligible_pool"}
