"""Hydration: PostgreSQL is product truth, the index only supplied an order."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

from app.repositories.products import ProductRepository
from app.schemas.dimensions import RawDimensions
from app.schemas.product import CommerceClassification, ProductRow
from app.schemas.retailer import RetailerContext
from app.schemas.semantic import SemanticRankedCandidate, SemanticRankingResult
from app.services.hydration import ProductHydrationService

CONTEXT = RetailerContext(store_id=50)


def _row(product_id: int, name: str = "Fresh name", price: str = "1000") -> ProductRow:
    return ProductRow(
        id=product_id, uuid=uuid4(), store_id=50, name_english=name,
        name_arabic="اسم", price_amount=Decimal(price), price_unit="SAR",
        image_url=f"https://db.test/{product_id}.jpg",
        product_url=f"https://db.test/{product_id}",
        visual_category="3-seater-sofa",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=RawDimensions(unit="cm"),
        main_color="Beige", styles=("Modern",), is_active=True,
    )


class FakeRepository:
    def __init__(self, rows: list[ProductRow]) -> None:
        self.rows = rows
        self.calls: list[Any] = []

    async def get_by_ids(self, ids: Any, context: RetailerContext) -> list[ProductRow]:
        self.calls.append((list(ids), context))
        wanted = set(ids)
        return [r for r in self.rows if r.id in wanted]


def _ranking(*ids: int) -> SemanticRankingResult:
    return SemanticRankingResult(
        candidates=tuple(
            SemanticRankedCandidate(product_id=i, relaxation_depth=0,
                                    semantic_similarity=0.5, semantic_rank=n)
            for n, i in enumerate(ids)
        ),
        semantic_used=True,
    )


def _service(rows: list[ProductRow]) -> tuple[ProductHydrationService, FakeRepository]:
    repo = FakeRepository(rows)
    return ProductHydrationService(cast(ProductRepository, repo)), repo


async def test_hydration_preserves_the_ranked_order() -> None:
    """The repository returns ids ascending; the ranking order must survive."""
    service, _ = _service([_row(1), _row(2), _row(3)])

    products = await service.hydrate(_ranking(3, 1, 2), CONTEXT)

    assert [p.product_id for p in products] == [3, 1, 2]


async def test_a_stale_ranked_id_is_dropped() -> None:
    """A vector outlived its product. The catalog is right, the index is behind."""
    service, _ = _service([_row(1), _row(3)])

    products = await service.hydrate(_ranking(3, 2, 1), CONTEXT)

    assert [p.product_id for p in products] == [3, 1]


async def test_values_come_from_postgresql_not_the_index() -> None:
    service, _ = _service([_row(1, name="Renamed in the catalog", price="2500")])

    products = await service.hydrate(_ranking(1), CONTEXT)

    assert products[0].name_english == "Renamed in the catalog"
    assert products[0].price_amount == Decimal("2500")
    assert products[0].image_url == "https://db.test/1.jpg"


async def test_hydration_is_scoped_to_the_request_context() -> None:
    service, repo = _service([_row(1)])

    await service.hydrate(_ranking(1), CONTEXT)

    assert repo.calls[0][1] is CONTEXT


async def test_an_empty_ranking_touches_no_database() -> None:
    service, repo = _service([_row(1)])

    products = await service.hydrate(
        SemanticRankingResult(candidates=(), semantic_used=False), CONTEXT
    )

    assert products == () and repo.calls == []
