"""The regression that motivated the whole phase.

Before this repair the conversational path was:

    ControlledRelaxationService -> discovery.search -> limit 50 -> ORDER BY id

so semantic ranking saw the fifty LOWEST-ID eligible products and the customer
was shown the best three of an arbitrary fifty. The best match in the catalog
was unreachable whenever it happened to have a high id - which is a property of
when a merchant uploaded it, not of whether the customer would like it.

M8 -> M9 -> selection is wired by hand here. The facade that does this in
production is M11B-3; the correctness it depends on is proved now.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from app.core.config import RelaxationSettings
from app.schemas.discovery import ProductSearchRequest, ProductSort
from app.schemas.product import EligibleProduct
from app.schemas.query import ConstraintSemantics, ResolvedSearch
from app.schemas.retailer import RetailerContext
from app.services.controlled_search import ControlledRelaxationService
from app.services.discovery import ProductDiscoveryService
from app.services.presentation import select_for_presentation
from app.services.relaxation import RelaxationPlanner
from app.services.semantic_ranking import SemanticRankingService

CONTEXT = RetailerContext(store_id=50)
NAMESPACE = "store-50"
POOL_SIZE = 173  # the real store-50 sofa pool
BURIED = 168  # well beyond the first 50 by id ascending


class FakeDiscovery:
    """An eligible pool of `size`, in id order, as the repository returns it."""

    def __init__(self, size: int, prices: dict[int, str] | None = None) -> None:
        self.size = size
        self.prices = prices or {}
        self.calls = 0

    async def eligible_pool(
        self, request: ProductSearchRequest, context: RetailerContext
    ) -> tuple[EligibleProduct, ...]:
        self.calls += 1
        products = [
            EligibleProduct(
                product_id=i, price_amount=Decimal(self.prices.get(i, "1000"))
            )
            for i in range(1, self.size + 1)
        ]
        if request.sort is ProductSort.PRICE_ASC:
            products.sort(key=lambda p: (p.price_amount, p.product_id))
        elif request.sort is ProductSort.PRICE_DESC:
            products.sort(key=lambda p: (-p.price_amount, p.product_id))
        return tuple(products)


class FakeEmbedder:
    model = "test-embedding-model"

    async def embed_query(self, text: str) -> tuple[float, ...]:
        return (0.1, 0.2, 0.3)


class FakeIndex:
    """Scores whatever it is given, and remembers how much that was."""

    def __init__(self, scores: dict[int, float]) -> None:
        self.scores = scores
        self.scored_ids: list[int] = []

    async def score(self, **kwargs: Any) -> dict[int, float]:
        ids = list(kwargs["product_ids"])
        self.scored_ids.extend(ids)
        return {i: self.scores.get(i, 0.1) for i in ids}


def _resolved(sort: ProductSort = ProductSort.DEFAULT) -> ResolvedSearch:
    return ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating", commerce_subcategory="sofa", sort=sort
        ),
        semantics=ConstraintSemantics(),
        semantic_text="a cosy reading sofa",
    )


async def _run(
    discovery: FakeDiscovery, index: FakeIndex, *, sort: ProductSort = ProductSort.DEFAULT
) -> Any:
    settings = RelaxationSettings()
    relaxation = ControlledRelaxationService(
        cast(ProductDiscoveryService, discovery), RelaxationPlanner(settings), settings
    )
    resolved = _resolved(sort)
    result = await relaxation.search(resolved, CONTEXT)
    ranking = SemanticRankingService(
        cast(Any, FakeEmbedder()), cast(Any, index)
    )
    return result, await ranking.rank(
        resolved, result.candidates, CONTEXT, namespace=NAMESPACE
    )


# ── the regression ──────────────────────────────────────────────────────────


async def test_the_whole_eligible_pool_reaches_semantic_ranking() -> None:
    discovery = FakeDiscovery(POOL_SIZE)
    index = FakeIndex({BURIED: 0.99})

    result, ranking = await _run(discovery, index)

    assert result.exact_candidate_count == POOL_SIZE
    assert len(result.candidates) == POOL_SIZE
    assert len(index.scored_ids) == POOL_SIZE
    assert BURIED in index.scored_ids
    assert len(ranking.candidates) == POOL_SIZE


async def test_a_high_id_winner_reaches_the_presented_products() -> None:
    """The exact failure: best match at id 168, only three products shown."""
    discovery = FakeDiscovery(POOL_SIZE)
    index = FakeIndex({BURIED: 0.99})

    _, ranking = await _run(discovery, index)
    presented = select_for_presentation(ranking.product_ids, limit=3)

    assert ranking.semantic_used is True
    assert presented[0] == BURIED
    assert len(presented) == 3


async def test_the_pool_is_not_silently_capped_at_fifty() -> None:
    """Pins the specific number the old path produced."""
    discovery = FakeDiscovery(POOL_SIZE)
    index = FakeIndex({})

    result, _ = await _run(discovery, index)

    assert len(result.candidates) != 50
    assert len(result.candidates) == POOL_SIZE


# ── presentation is strictly downstream ─────────────────────────────────────


async def test_selection_happens_after_ranking_not_before() -> None:
    """Ranking sees 173 whatever the presentation limit turns out to be."""
    discovery = FakeDiscovery(POOL_SIZE)
    index = FakeIndex({BURIED: 0.99})

    _, ranking = await _run(discovery, index)

    for limit in (1, 3, 10):
        presented = select_for_presentation(ranking.product_ids, limit=limit)
        assert len(index.scored_ids) == POOL_SIZE
        assert len(presented) == limit
        assert presented[0] == BURIED


async def test_a_ranked_but_unpresented_product_is_simply_absent() -> None:
    discovery = FakeDiscovery(POOL_SIZE)
    index = FakeIndex({BURIED: 0.99})

    _, ranking = await _run(discovery, index)
    presented = select_for_presentation(ranking.product_ids, limit=3)

    assert len(ranking.candidates) == POOL_SIZE
    assert set(presented) < set(ranking.product_ids)


# ── an explicit sort still wins, over the whole pool ────────────────────────


async def test_the_cheapest_product_is_found_beyond_the_first_fifty() -> None:
    """"Cheapest" over the lowest 50 ids is a different, wrong answer."""
    discovery = FakeDiscovery(POOL_SIZE, prices={BURIED: "99"})
    index = FakeIndex({1: 0.99})

    _, ranking = await _run(discovery, index, sort=ProductSort.PRICE_ASC)
    presented = select_for_presentation(ranking.product_ids, limit=1)

    assert presented == (BURIED,)


async def test_a_deterministic_fallback_still_honours_the_sort() -> None:
    """With ranking unavailable the pool order is what the customer sees."""
    discovery = FakeDiscovery(POOL_SIZE, prices={BURIED: "99"})
    settings = RelaxationSettings()
    relaxation = ControlledRelaxationService(
        cast(ProductDiscoveryService, discovery), RelaxationPlanner(settings), settings
    )
    resolved = _resolved(ProductSort.PRICE_ASC)

    result = await relaxation.search(resolved, CONTEXT)
    ranking = await SemanticRankingService(None, None).rank(
        resolved, result.candidates, CONTEXT, namespace=NAMESPACE
    )

    assert ranking.semantic_used is False
    assert select_for_presentation(ranking.product_ids, limit=1) == (BURIED,)
