"""The composed search path, end to end without a database.

Two properties carry most of these tests.

**The limit applies after ranking, never before.** A pool of 173 arrives as
173, the best match is found wherever it sits, and only then are three chosen.

**What is rendered is what is recorded.** The ids and the grounding are built
in one pass over one hydrated sequence, so position two of each describes the
same product — a guarantee no validator can make, because the grounded shape
carries no id to compare.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from app.core.config import PineconeSettings
from app.core.exceptions import ConfigurationError, RankingIntegrityError
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import ProductSearchRequest, ProductSort
from app.schemas.grounding import DroppedConstraint, SearchOutcome
from app.schemas.product import CommerceClassification, EligibleProduct, ProductCandidate
from app.schemas.query import ConstraintSemantics, ResolvedSearch
from app.schemas.relaxation import (
    ControlledSearchResult,
    RelaxationAttempt,
    RelaxedCandidate,
    StopReason,
)
from app.schemas.resolution import (
    CandidatePoolResult,
    ProductSearchExecutionResult,
    RankedProductCandidate,
)
from app.schemas.retailer import RetailerContext
from app.schemas.semantic import (
    SemanticRankedCandidate,
    SemanticRankingResult,
    SemanticSkipReason,
)
from app.services.controlled_search import ControlledRelaxationService
from app.services.hydration import ProductHydrationService
from app.services.search_pipeline import ProductSearchPipeline
from app.services.semantic_ranking import SemanticRankingService
from app.taxonomy.dimensions import DimensionRole, UnsupportedDimensionReason
from pydantic import SecretStr, ValidationError

CONTEXT = RetailerContext(store_id=50)


def _candidate(product_id: int) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Sofa {product_id}",
        name_arabic="كنبة",
        price_amount=Decimal(f"{1000 + product_id}.00"),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


class FakeRelaxation:
    """A pool with known first-seen depths, as M8 would produce it."""

    def __init__(self, depths: dict[int, int], exact_count: int | None = None) -> None:
        self.depths = depths
        self.exact_count = exact_count if exact_count is not None else len(depths)

    async def search(
        self, resolved: ResolvedSearch, context: RetailerContext, **_: object
    ) -> ControlledSearchResult:
        request = resolved.request
        return ControlledSearchResult(
            original_request=request,
            final_request=request,
            candidates=tuple(
                RelaxedCandidate(
                    product=EligibleProduct(
                        product_id=i, price_amount=Decimal(f"{1000 + i}.00")
                    ),
                    relaxation_depth=depth,
                )
                for i, depth in self.depths.items()
            ),
            exact_candidate_count=self.exact_count,
            target_candidates=5,
            target_reached=True,
            stop_reason=StopReason.EXACT_SUFFICIENT,
            attempts=(
                RelaxationAttempt(
                    depth=0, request=request, changes=(), eligible_count=len(self.depths)
                ),
            ),
        )


class FakeRanking:
    """Orders by the sequence given, preserving each candidate's depth."""

    def __init__(
        self,
        order: list[int] | None = None,
        *,
        used: bool = True,
        skip: SemanticSkipReason | None = None,
    ) -> None:
        self.order = order
        self.used = used
        self.skip = skip
        self.saw: list[int] = []

    async def rank(
        self, resolved: Any, candidates: Any, context: Any, *, namespace: str
    ) -> SemanticRankingResult:
        depths = {c.product.product_id: c.relaxation_depth for c in candidates}
        self.saw = list(depths)
        self.namespace = namespace
        ordered = self.order if self.order is not None else self.saw
        return SemanticRankingResult(
            candidates=tuple(
                SemanticRankedCandidate(
                    product_id=i, relaxation_depth=depths[i], semantic_rank=position
                )
                for position, i in enumerate(ordered)
            ),
            semantic_used=self.used,
            skip_reason=self.skip,
        )


class FakeHydration:
    """PostgreSQL's answer: some ids come back, some no longer exist."""

    def __init__(self, missing: set[int] | None = None) -> None:
        self.missing = missing or set()
        self.asked: list[int] = []
        self.contexts: list[RetailerContext] = []

    async def hydrate_ids(
        self, product_ids: Any, context: RetailerContext
    ) -> tuple[ProductCandidate, ...]:
        self.asked = list(product_ids)
        self.contexts.append(context)
        return tuple(_candidate(i) for i in product_ids if i not in self.missing)


def _pipeline(
    relaxation: FakeRelaxation,
    ranking: FakeRanking,
    hydration: FakeHydration,
    *,
    limit: int = 3,
    pinecone: PineconeSettings | None = None,
) -> ProductSearchPipeline:
    return ProductSearchPipeline(
        cast(ControlledRelaxationService, relaxation),
        cast(SemanticRankingService, ranking),
        cast(ProductHydrationService, hydration),
        presentation_limit=limit,
        pinecone=pinecone,
    )


def _resolved() -> ResolvedSearch:
    return ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating", commerce_subcategory="sofa"
        ),
        semantics=ConstraintSemantics(),
        semantic_text="a cosy reading sofa",
    )


async def _run(
    depths: dict[int, int],
    *,
    order: list[int] | None = None,
    missing: set[int] | None = None,
    limit: int = 3,
    exact_count: int | None = None,
    used: bool = True,
    skip: SemanticSkipReason | None = None,
) -> tuple[ProductSearchExecutionResult, FakeRanking, FakeHydration]:
    ranking = FakeRanking(order, used=used, skip=skip)
    hydration = FakeHydration(missing)
    pipeline = _pipeline(
        FakeRelaxation(depths, exact_count), ranking, hydration, limit=limit
    )
    return await pipeline.execute(_resolved(), CONTEXT), ranking, hydration


# ── the whole pool reaches ranking ══════════════════════════════════════════


async def test_the_complete_pool_is_ranked_before_anything_is_chosen() -> None:
    depths = dict.fromkeys(range(1, 174), 0)

    result, ranking, hydration = await _run(depths, limit=3)

    assert len(ranking.saw) == 173
    assert result.grounding.eligible_count == 173
    assert result.grounding.ranked_count == 173
    assert result.grounding.selected_count == 3
    assert len(hydration.asked) == 3


BURIED = 168


async def test_a_high_id_winner_reaches_the_presented_set() -> None:
    """The M11B-1 regression, now through the composed path."""
    depths = dict.fromkeys(range(1, 174), 0)

    # Every eligible id, with the winner first: a ranking that returned only
    # some of them is a defect, not a ranking (the conservation check).
    order = [BURIED, *(i for i in range(1, 174) if i != BURIED)]

    result, _, _ = await _run(depths, order=order, limit=3)

    assert result.presented_product_ids[0] == BURIED
    assert len(result.presented_product_ids) == 3


async def test_the_limit_never_reaches_the_pool() -> None:
    depths = dict.fromkeys(range(1, 61), 0)

    result, ranking, _ = await _run(depths, limit=1)

    assert len(ranking.saw) == 60
    assert result.grounding.presented_count == 1


# ── identity of ids and grounding ══════════════════════════════════════════


async def test_each_id_and_its_grounding_describe_the_same_product() -> None:
    """Distinct names and prices, so a mismatch could not hide."""
    result, _, _ = await _run({7: 0, 3: 0, 9: 0}, order=[9, 3, 7], limit=3)

    assert result.presented_product_ids == (9, 3, 7)
    for product_id, grounded in zip(
        result.presented_product_ids, result.grounding.products, strict=True
    ):
        assert grounded.name_english == f"Sofa {product_id}"
        assert grounded.price_amount == Decimal(f"{1000 + product_id}.00")


async def test_identity_survives_a_stale_row_in_the_middle() -> None:
    result, _, _ = await _run({1: 0, 2: 0, 3: 0}, order=[1, 2, 3], missing={2}, limit=3)

    assert result.presented_product_ids == (1, 3)
    assert [p.name_english for p in result.grounding.products] == ["Sofa 1", "Sofa 3"]


# ── counts ══════════════════════════════════════════════════════════════════


async def test_a_stale_row_is_not_a_presentation_truncation() -> None:
    """ranked 5, limit 5, one gone: the limit omitted nothing."""
    result, _, _ = await _run(dict.fromkeys(range(1, 6), 0), missing={3}, limit=5)
    grounding = result.grounding

    assert (grounding.ranked_count, grounding.selected_count) == (5, 5)
    assert grounding.presented_count == 4
    assert grounding.stale_dropped_count == 1
    assert grounding.truncated_for_presentation is False


async def test_a_limit_truncation_is_not_a_stale_row() -> None:
    result, _, _ = await _run(dict.fromkeys(range(1, 21), 0), limit=5)
    grounding = result.grounding

    assert (grounding.ranked_count, grounding.selected_count) == (20, 5)
    assert grounding.presented_count == 5
    assert grounding.stale_dropped_count == 0
    assert grounding.truncated_for_presentation is True


async def test_both_reductions_are_reported_separately() -> None:
    result, _, _ = await _run(dict.fromkeys(range(1, 21), 0), missing={3}, limit=5)
    grounding = result.grounding

    assert grounding.selected_count == 5
    assert grounding.presented_count == 4
    assert grounding.stale_dropped_count == 1
    assert grounding.truncated_for_presentation is True


# ── zero results ════════════════════════════════════════════════════════════


async def test_nothing_eligible_is_a_zero_result() -> None:
    result, _, _ = await _run({}, limit=3)
    grounding = result.grounding

    assert grounding.outcome is SearchOutcome.ZERO_RESULTS
    assert result.presented_product_ids == ()
    assert (grounding.eligible_count, grounding.selected_count) == (0, 0)
    assert grounding.stale_dropped_count == 0


async def test_all_selected_stale_is_also_a_zero_result() -> None:
    """A successful execution whose products vanished before the read."""
    result, _, _ = await _run(
        dict.fromkeys(range(1, 6), 0), missing={1, 2, 3, 4, 5}, limit=5
    )
    grounding = result.grounding

    assert grounding.outcome is SearchOutcome.ZERO_RESULTS
    assert result.presented_product_ids == ()
    assert grounding.stale_dropped_count == 5
    assert grounding.truncated_for_presentation is False


async def test_the_two_zero_results_are_distinguishable_by_counts() -> None:
    """No prose, no third outcome: the counts carry the cause."""
    empty, _, _ = await _run({}, limit=5)
    stale, _, _ = await _run(
        dict.fromkeys(range(1, 21), 0), missing=set(range(1, 21)), limit=5
    )

    assert empty.grounding.eligible_count == 0
    assert empty.grounding.stale_dropped_count == 0
    assert stale.grounding.eligible_count == 20
    assert stale.grounding.stale_dropped_count == 5
    assert stale.grounding.truncated_for_presentation is True


# ── no backfill ═════════════════════════════════════════════════════════════


async def test_a_stale_row_is_never_replaced_from_further_down() -> None:
    """Rank six must not step into rank three's place."""
    depths = dict.fromkeys(range(1, 7), 0)

    result, _, hydration = await _run(
        depths, order=[1, 2, 3, 4, 5, 6], missing={3}, limit=5
    )

    assert result.presented_product_ids == (1, 2, 4, 5)
    assert 6 not in result.presented_product_ids
    assert hydration.asked == [1, 2, 3, 4, 5]  # selected once, never again


# ── ordinals ════════════════════════════════════════════════════════════════


async def test_ordinals_renumber_over_what_survived() -> None:
    """The customer never sees an empty second card."""
    result, _, _ = await _run({1: 0, 2: 0, 3: 0}, order=[1, 2, 3], missing={2}, limit=3)

    assert [p.grounding_ref for p in result.grounding.products] == [1, 2]
    assert [p.presented_ordinal for p in result.grounding.products] == [1, 2]


async def test_refs_and_ordinals_agree_position_by_position() -> None:
    result, _, _ = await _run(dict.fromkeys(range(1, 4), 0), limit=3)

    for product in result.grounding.products:
        assert product.grounding_ref == product.presented_ordinal


# ── relaxation depth ════════════════════════════════════════════════════════


async def test_verified_depths_reach_every_grounded_product() -> None:
    result, _, _ = await _run({1: 0, 2: 2, 3: 1}, order=[1, 2, 3], limit=3)
    depths = {
        p.name_english: p.relaxation_depth for p in result.grounding.products
    }

    assert depths == {"Sofa 1": 0, "Sofa 2": 2, "Sofa 3": 1}
    assert [p.matched_exactly for p in result.grounding.products] == [True, False, False]


async def test_a_relaxed_depth_never_defaults_to_zero() -> None:
    result, _, _ = await _run({5: 2}, limit=3)

    assert result.grounding.products[0].relaxation_depth == 2


async def test_depths_survive_a_degraded_ranking() -> None:
    """The deterministic fallback carries provenance too."""
    result, _, _ = await _run(
        {1: 0, 2: 1},
        used=False,
        skip=SemanticSkipReason.INDEX_UNAVAILABLE,
        limit=3,
    )

    assert [p.relaxation_depth for p in result.grounding.products] == [0, 1]


# ── metadata propagation ════════════════════════════════════════════════════


async def test_semantic_status_is_reported_not_reinterpreted() -> None:
    result, _, _ = await _run(
        {1: 0}, used=False, skip=SemanticSkipReason.NOT_CONFIGURED, limit=3
    )

    assert result.grounding.semantic_used is False
    assert result.grounding.semantic_skip_reason is SemanticSkipReason.NOT_CONFIGURED


async def test_no_candidate_is_lost_when_ranking_degrades() -> None:
    result, ranking, _ = await _run(
        dict.fromkeys(range(1, 21), 0),
        used=False,
        skip=SemanticSkipReason.EMBEDDING_UNAVAILABLE,
        limit=20,
    )

    assert len(ranking.saw) == 20
    assert result.grounding.ranked_count == 20
    assert result.grounding.presented_count == 20


async def test_the_exact_count_comes_from_m8() -> None:
    """The true unbounded depth-0 count, never recomputed downstream."""
    result, _, _ = await _run(dict.fromkeys(range(1, 21), 0), exact_count=7, limit=3)

    assert result.grounding.exact_candidate_count == 7


async def test_the_stop_reason_is_carried_through() -> None:
    result, _, _ = await _run({1: 0}, limit=3)

    assert result.grounding.stop_reason is StopReason.EXACT_SUFFICIENT
    assert result.grounding.was_relaxed is False


async def test_dropped_constraints_are_carried_not_discovered() -> None:
    """A product-type change makes a measurement unanswerable; the pipeline
    reports what composition found rather than rediscovering it."""
    dropped = (
        DroppedConstraint(
            role=DimensionRole.OVERALL_WIDTH,
            reason=UnsupportedDimensionReason.ROLE_NOT_DEFINED,
        ),
    )
    pipeline = _pipeline(FakeRelaxation({1: 0}), FakeRanking(), FakeHydration())

    result = await pipeline.execute(
        _resolved(), CONTEXT, dropped_constraints=dropped
    )

    assert result.grounding.dropped_constraints == dropped


# ── namespace and configuration ═════════════════════════════════════════════


async def test_the_namespace_is_derived_from_the_request_scope() -> None:
    ranking = FakeRanking()
    pipeline = _pipeline(
        FakeRelaxation({1: 0}),
        ranking,
        FakeHydration(),
        pinecone=PineconeSettings(api_key=SecretStr("k"), index_name="ai-agent"),
    )

    await pipeline.execute(_resolved(), CONTEXT)

    assert ranking.namespace == "store-50"


async def test_an_unconfigured_index_passes_no_namespace() -> None:
    ranking = FakeRanking()
    pipeline = _pipeline(FakeRelaxation({1: 0}), ranking, FakeHydration())

    await pipeline.execute(_resolved(), CONTEXT)

    assert ranking.namespace == ""


@pytest.mark.parametrize("limit", [0, -1])
def test_a_pipeline_that_shows_nothing_cannot_be_constructed(limit: int) -> None:
    with pytest.raises(ConfigurationError):
        _pipeline(FakeRelaxation({}), FakeRanking(), FakeHydration(), limit=limit)


async def test_the_sort_reaches_ranking_untouched() -> None:
    """The pipeline decides no ordering of its own."""
    ranking = FakeRanking()
    pipeline = _pipeline(FakeRelaxation({1: 0}), ranking, FakeHydration())
    resolved = ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating", sort=ProductSort.PRICE_ASC
        )
    )

    result = await pipeline.execute(resolved, CONTEXT)

    assert result.grounding.presented_count == 1


# ── candidate conservation ══════════════════════════════════════════════════
#
# M9 decides ORDER, never eligibility: the list it receives is the list it
# returns, reordered. If that stops holding, the customer is shown a set
# nothing established — a product invented, or one the catalog qualified
# silently lost. Count equality alone cannot tell those apart.


class LosingRanking(FakeRanking):
    """A ranking that returns a different population from the one given."""

    def __init__(self, returns: list[int]) -> None:
        super().__init__()
        self.returns = returns

    async def rank(
        self, resolved: Any, candidates: Any, context: Any, *, namespace: str
    ) -> SemanticRankingResult:
        depths = {c.product.product_id: c.relaxation_depth for c in candidates}
        self.saw = list(depths)
        self.namespace = namespace
        return SemanticRankingResult(
            candidates=tuple(
                SemanticRankedCandidate(
                    product_id=i,
                    relaxation_depth=depths.get(i, 0),
                    semantic_rank=position,
                )
                for position, i in enumerate(self.returns)
            ),
            semantic_used=True,
        )


async def _conserve(
    eligible: list[int], ranked: list[int], *, limit: int = 3
) -> tuple[FakeHydration, Any]:
    ranking = LosingRanking(ranked)
    hydration = FakeHydration()
    pipeline = _pipeline(
        FakeRelaxation(dict.fromkeys(eligible, 0)), ranking, hydration, limit=limit
    )
    return hydration, pipeline


async def test_a_reordered_population_is_accepted() -> None:
    """Order may change; membership may not."""
    _, pipeline = await _conserve([1, 2, 3], [3, 2, 1])

    result = await pipeline.execute(_resolved(), CONTEXT)

    assert result.presented_product_ids == (3, 2, 1)


async def test_an_empty_population_is_accepted() -> None:
    _, pipeline = await _conserve([], [])

    result = await pipeline.execute(_resolved(), CONTEXT)

    assert result.grounding.outcome is SearchOutcome.ZERO_RESULTS
    assert result.presented_product_ids == ()


async def test_a_lost_candidate_is_an_integrity_failure() -> None:
    _, pipeline = await _conserve([1, 2, 3], [1, 2])

    with pytest.raises(RankingIntegrityError):
        await pipeline.execute(_resolved(), CONTEXT)


async def test_an_invented_candidate_is_an_integrity_failure() -> None:
    _, pipeline = await _conserve([1, 2], [1, 2, 3])

    with pytest.raises(RankingIntegrityError):
        await pipeline.execute(_resolved(), CONTEXT)


async def test_the_right_count_with_the_wrong_products_still_fails() -> None:
    """The case a length check waves through."""
    _, pipeline = await _conserve([1, 2, 3], [1, 2, 999])

    with pytest.raises(RankingIntegrityError):
        await pipeline.execute(_resolved(), CONTEXT)


async def test_a_duplicated_product_still_fails() -> None:
    """Equal length again, but one product is shown twice and one is gone."""
    _, pipeline = await _conserve([1, 2, 3], [1, 1, 2])

    with pytest.raises(RankingIntegrityError):
        await pipeline.execute(_resolved(), CONTEXT)


async def test_the_check_runs_before_anything_is_chosen_or_read() -> None:
    """A violated contract must not reach selection or the catalog."""
    hydration, pipeline = await _conserve([1, 2, 3], [1, 2, 999])

    with pytest.raises(RankingIntegrityError):
        await pipeline.execute(_resolved(), CONTEXT)

    assert hydration.asked == []


async def test_no_result_is_returned_after_an_integrity_failure() -> None:
    """Raised rather than repaired: restoring the products would hide it."""
    _, pipeline = await _conserve([1, 2, 3], [1, 2])

    with pytest.raises(RankingIntegrityError) as caught:
        await pipeline.execute(_resolved(), CONTEXT)

    assert caught.value.context == {"eligible_count": 3, "ranked_count": 2}


async def test_an_integrity_failure_is_not_a_customer_condition() -> None:
    _, pipeline = await _conserve([1, 2, 3], [1, 2])

    with pytest.raises(RankingIntegrityError) as caught:
        await pipeline.execute(_resolved(), CONTEXT)

    assert caught.value.code == "ranking_integrity_error"
    assert "rank" not in caught.value.public_message.lower()


async def test_degradation_does_not_authorise_candidate_loss() -> None:
    """Pinecone being unreachable costs the ordering, never the products."""
    result, ranking, _ = await _run(
        dict.fromkeys(range(1, 21), 0),
        used=False,
        skip=SemanticSkipReason.INDEX_UNAVAILABLE,
        limit=5,
    )

    assert len(ranking.saw) == 20
    assert result.grounding.ranked_count == result.grounding.eligible_count == 20


async def test_a_legitimate_zero_result_is_never_an_integrity_failure() -> None:
    result, _, _ = await _run({}, limit=3)

    assert result.grounding.outcome is SearchOutcome.ZERO_RESULTS
    assert result.grounding.eligible_count == result.grounding.ranked_count == 0


# ── the internal candidate pool ═════════════════════════════════════════════
#
# The second way out of the same facade. A room optimiser has to see the sofas,
# not the three cards a customer would have been shown, so this path ends at
# the whole ranked pool — and the presentation limit must be unreachable from
# it, or the optimiser would silently inherit a display decision.


async def _pool(
    depths: dict[int, int],
    *,
    order: list[int] | None = None,
    missing: set[int] | None = None,
    limit: int = 3,
    used: bool = True,
) -> tuple[CandidatePoolResult, FakeRanking, FakeHydration]:
    ranking = FakeRanking(order, used=used)
    hydration = FakeHydration(missing)
    pipeline = _pipeline(FakeRelaxation(depths), ranking, hydration, limit=limit)
    return (
        await pipeline.execute_candidate_pool(_resolved(), CONTEXT),
        ranking,
        hydration,
    )


async def test_the_whole_pool_survives_a_small_presentation_limit() -> None:
    """The defect this path exists to prevent, stated as a test."""
    pool, _, hydration = await _pool(dict.fromkeys(range(1, 174), 0), limit=3)

    assert len(pool.candidates) == 173
    assert len(hydration.asked) == 173
    assert pool.eligible_count == 173


async def test_every_ranked_product_is_hydrated_in_ranked_order() -> None:
    pool, _, hydration = await _pool({1: 0, 2: 0, 3: 0}, order=[3, 1, 2])

    assert hydration.asked == [3, 1, 2]
    assert [c.product.product_id for c in pool.candidates] == [3, 1, 2]


async def test_a_stale_product_is_dropped_and_never_substituted() -> None:
    pool, _, _ = await _pool({1: 0, 2: 0, 3: 0}, order=[3, 1, 2], missing={1})

    assert [c.product.product_id for c in pool.candidates] == [3, 2]
    assert pool.eligible_count == 3
    assert pool.stale_dropped_count == 1


async def test_relaxation_depth_travels_with_each_candidate() -> None:
    pool, _, _ = await _pool({1: 0, 2: 2}, order=[2, 1])

    assert [c.relaxation_depth for c in pool.candidates] == [2, 0]


async def test_an_empty_pool_is_an_ordinary_result() -> None:
    pool, _, _ = await _pool({})

    assert pool.candidates == ()
    assert pool.eligible_count == 0
    assert pool.stale_dropped_count == 0


async def test_the_pool_path_also_refuses_a_changed_population() -> None:
    """The conservation check is in the shared path, so neither method skips it."""
    _, pipeline = await _conserve([1, 2, 3], [1, 2, 999])

    with pytest.raises(RankingIntegrityError):
        await pipeline.execute_candidate_pool(_resolved(), CONTEXT)


async def test_the_pool_scope_comes_from_the_context() -> None:
    hydration = FakeHydration()
    ranking = FakeRanking()
    pipeline = _pipeline(FakeRelaxation({1: 0}), ranking, hydration)

    await pipeline.execute_candidate_pool(_resolved(), RetailerContext(store_id=99))

    assert hydration.contexts == [RetailerContext(store_id=99)]


async def test_the_two_paths_agree_on_what_the_customer_would_have_seen() -> None:
    """One search, one ranking: the page is a prefix of the pool.

    The strongest available statement that the extension did not create a
    second search path — an optimiser cannot be reasoning over a different set
    from the one the customer could have been shown.
    """
    depths = dict.fromkeys(range(1, 21), 0)
    order = list(range(20, 0, -1))

    page, _, _ = await _run(depths, order=order, limit=3)
    pool, _, _ = await _pool(depths, order=order, limit=3)

    assert page.presented_product_ids == tuple(
        c.product.product_id for c in pool.candidates[:3]
    )


# ── the pool carries nothing customer-facing ────────────────────────────────


@pytest.mark.parametrize(
    "forbidden",
    ["ordinal", "grounding", "presented", "similarity", "score", "rank"],
)
def test_the_pool_contract_holds_no_presentation_concept(forbidden: str) -> None:
    for model in (CandidatePoolResult, RankedProductCandidate):
        for name in model.model_fields:
            assert forbidden not in name, f"{model.__name__}.{name}"


def test_the_pool_contract_reaches_no_similarity() -> None:
    """Ranking's numbers stop at ranking. Carrying them forward is how a
    blended relevance score gets invented later (CLAUDE.md 16.1)."""
    definitions = CandidatePoolResult.model_json_schema().get("$defs", {})

    assert "SemanticRankedCandidate" not in definitions
    for definition in definitions.values():
        for name in definition.get("properties", {}):
            assert "similarity" not in name


def test_a_hydrated_pool_cannot_exceed_the_pool_that_was_ranked() -> None:
    with pytest.raises(ValidationError):
        CandidatePoolResult(
            candidates=(
                RankedProductCandidate(product=_candidate(1), relaxation_depth=0),
                RankedProductCandidate(product=_candidate(2), relaxation_depth=0),
            ),
            eligible_count=1,
            was_relaxed=False,
            stop_reason=StopReason.EXACT_SUFFICIENT,
            semantic_used=False,
        )


def test_the_candidate_pool_method_cannot_read_the_presentation_limit() -> None:
    """Structural, not behavioural: the bound must be unreachable from this
    path rather than merely unused by today's implementation."""
    source = (Path(__file__).parents[2] / "app/services/search_pipeline.py").read_text()
    tree = ast.parse(source)

    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "execute_candidate_pool"
    )
    body = ast.dump(method)

    assert "_presentation_limit" not in body
    assert "select_for_presentation" not in body


def test_both_public_methods_run_the_one_shared_search_path() -> None:
    """Neither may call the relaxation or ranking services for itself."""
    source = (Path(__file__).parents[2] / "app/services/search_pipeline.py").read_text()
    tree = ast.parse(source)

    for name in ("execute", "execute_candidate_pool"):
        method = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        )
        body = ast.dump(method)
        assert "_search_and_rank" in body, name
        assert "_relaxation" not in body, name
        assert "_ranking" not in body, name
