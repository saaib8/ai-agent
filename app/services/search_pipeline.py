"""The one place M6, M8, M9, selection and hydration are composed.

A facade, and deliberately nothing more. It owns no policy: it does not decide
what the customer meant, which bounds may widen, how candidates are ordered, or
what to say. Each of those already has an owner, and a facade that started
deciding would become a second one.

What it does own is the **order**, and one invariant that only exists here:

    eligible  ->  ranked  ->  selected  ->  presented

Every product the catalog holds is ranked; only then does the presentation
limit choose; only then is anything read for display. A limit applied earlier
would pick the customer's options before anything had judged them, which is the
defect M11B-1 removed (CLAUDE.md 16.1).

Two things a careless composition would get wrong, and are structural here:

* **The ids and the grounding come from one pass over one hydrated sequence.**
  The grounded shape carries no product id, so no validator can prove the two
  lists describe the same products - building them together is the guarantee.
* **A stale row is dropped, never replaced.** Nothing re-enters selection after
  hydration, so the products rendered are exactly the products committed.

Two ways out, sharing one way in. `execute` is the customer path and ends at a
page with ordinals and grounding; `execute_candidate_pool` is the internal path
and ends at the whole ranked pool, for deterministic services that choose
across products rather than display them. Both run the same controlled search,
the same ranking and the same conservation check, so the set an optimiser
reasons over can never be a different set from the one a customer could have
been shown. Only the last two steps differ - what is selected, and what is
built from it - and the presentation limit belongs to exactly one of them.
"""

from __future__ import annotations

import time
from decimal import Decimal

from app.core.config import PineconeSettings
from app.core.exceptions import ConfigurationError, RankingIntegrityError
from app.core.logging import get_logger
from app.schemas.grounding import (
    DroppedConstraint,
    GroundedProduct,
    RelaxationSummaryItem,
    SearchExecutionGrounding,
    SearchOutcome,
)
from app.schemas.query import ResolvedSearch
from app.schemas.relaxation import (
    AppliedRelaxation,
    ControlledSearchResult,
    DimensionRelaxationChange,
    RelaxableField,
)
from app.schemas.resolution import (
    CandidatePoolResult,
    ProductSearchExecutionResult,
    RankedProductCandidate,
)
from app.schemas.retailer import RetailerContext
from app.schemas.semantic import SemanticRankingResult
from app.services.controlled_search import ControlledRelaxationService
from app.services.grounding_builder import to_grounded_product
from app.services.hydration import ProductHydrationService
from app.services.presentation import select_for_presentation
from app.services.semantic_ranking import SemanticRankingService
from app.taxonomy.dimensions import DimensionRole

logger = get_logger(__name__)

_NO_NAMESPACE = ""
"""Passed when semantic ranking is unconfigured. `rank` reports
`NOT_CONFIGURED` before reading it, so no namespace is invented."""


class ProductSearchPipeline:
    """A resolved search in, verified products out.

    The only search facade. Everything that needs products goes through one of
    its two methods, so no caller reconstructs M6, M8 or M9 for itself.
    """

    def __init__(
        self,
        relaxation: ControlledRelaxationService,
        ranking: SemanticRankingService,
        hydration: ProductHydrationService,
        *,
        presentation_limit: int,
        pinecone: PineconeSettings | None = None,
    ) -> None:
        if presentation_limit < 1:
            # A pipeline that shows nothing cannot exist, so this is refused
            # at construction rather than on a request.
            raise ConfigurationError(
                detail="presentation_limit must allow at least one product",
                public_message="Search is not configured correctly.",
            )
        self._relaxation = relaxation
        self._ranking = ranking
        self._hydration = hydration
        self._presentation_limit = presentation_limit
        self._pinecone = pinecone

    async def execute(
        self,
        resolved: ResolvedSearch,
        context: RetailerContext,
        *,
        dropped_constraints: tuple[DroppedConstraint, ...] = (),
    ) -> ProductSearchExecutionResult:
        """Run one search and return what may be committed and explained.

        `dropped_constraints` is carried through from composition: a product
        type change can make a measurement unanswerable, and the reply has to
        say so. The pipeline does not discover them.
        """
        started = time.perf_counter()

        searched, ranked = await self._search_and_rank(resolved, context)

        selected = select_for_presentation(
            ranked.product_ids, limit=self._presentation_limit
        )
        hydrated = await self._hydration.hydrate_ids(selected, context)

        depth_by_id = {c.product_id: c.relaxation_depth for c in ranked.candidates}
        presented_ids: list[int] = []
        grounded: list[GroundedProduct] = []
        # One pass, one sequence: the id and the grounding at each position
        # come from the same hydrated product, which is the only guarantee
        # available once the grounded shape has dropped the id.
        for ordinal, product in enumerate(hydrated, start=1):
            presented_ids.append(product.product_id)
            grounded.append(
                to_grounded_product(
                    product,
                    grounding_ref=ordinal,
                    # Renumbered over what survived: a stale row leaves no gap,
                    # because the customer never sees an empty second card.
                    presented_ordinal=ordinal,
                    relaxation_depth=depth_by_id[product.product_id],
                )
            )

        grounding = SearchExecutionGrounding(
            outcome=SearchOutcome.RESULTS if grounded else SearchOutcome.ZERO_RESULTS,
            products=tuple(grounded),
            eligible_count=len(searched.candidates),
            ranked_count=len(ranked.candidates),
            selected_count=len(selected),
            presented_count=len(grounded),
            exact_candidate_count=searched.exact_candidate_count,
            was_relaxed=searched.was_relaxed,
            relaxations=_summarise(searched),
            stop_reason=searched.stop_reason,
            dropped_constraints=dropped_constraints,
            semantic_used=ranked.semantic_used,
            semantic_skip_reason=ranked.skip_reason,
        )
        logger.info(
            "product_search_pipeline_completed",
            store_id=context.store_id,
            commerce_category=resolved.request.commerce_category,
            commerce_subcategory=resolved.request.commerce_subcategory,
            eligible_count=grounding.eligible_count,
            ranked_count=grounding.ranked_count,
            selected_count=grounding.selected_count,
            presented_count=grounding.presented_count,
            stale_dropped_count=grounding.stale_dropped_count,
            truncated_for_presentation=grounding.truncated_for_presentation,
            exact_candidate_count=grounding.exact_candidate_count,
            was_relaxed=grounding.was_relaxed,
            stop_reason=str(grounding.stop_reason),
            semantic_used=grounding.semantic_used,
            semantic_skip_reason=(
                str(ranked.skip_reason) if ranked.skip_reason else None
            ),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return ProductSearchExecutionResult(
            presented_product_ids=tuple(presented_ids), grounding=grounding
        )

    async def _search_and_rank(
        self, resolved: ResolvedSearch, context: RetailerContext
    ) -> tuple[ControlledSearchResult, SemanticRankingResult]:
        """Eligibility, then order, then the proof that they are the same set.

        The whole of the search path both public methods share, and the reason
        neither can drift from the other: there is one controlled search, one
        ranking and one conservation check, not a customer copy and an internal
        copy that could answer differently.

        What it deliberately does not do is select or hydrate. Those are the
        two steps where the customer path and the optimiser path genuinely
        differ, so they stay with the callers rather than being hidden behind a
        flag here.
        """
        searched = await self._relaxation.search(resolved, context)
        eligible_ids = tuple(c.product.product_id for c in searched.candidates)
        ranked = await self._ranking.rank(
            resolved, searched.candidates, context, namespace=self._namespace(context)
        )
        # Checked before anything is chosen or read, so a violated contract
        # cannot reach the customer as a plausible-looking page of products,
        # nor an optimiser as a room built from products nothing qualified.
        _require_conserved(eligible_ids, ranked.product_ids, context)
        return searched, ranked

    async def execute_candidate_pool(
        self, resolved: ResolvedSearch, context: RetailerContext
    ) -> CandidatePoolResult:
        """One search, and every eligible product it found, ranked and verified.

        The internal counterpart to :meth:`execute`, for deterministic services
        that reason over a whole pool rather than showing a page of it - a
        room optimiser choosing a sofa has to see the sofas, not the three
        cards a customer would have been shown.

        **Unbounded, deliberately.** M8 and M9 already produce and order the
        complete eligible pool; truncating here would be a new selection
        policy choosing a room's options before anything had judged them.
        `presentation_limit` is customer display policy and is never read on
        this path.

        Nothing customer-facing is produced: no ordinal, no grounding, no
        presented ids, and no state is written. A stale row is dropped by the
        same hydration path `execute` uses, never substituted or backfilled.
        """
        started = time.perf_counter()

        searched, ranked = await self._search_and_rank(resolved, context)
        hydrated = await self._hydration.hydrate_ids(ranked.product_ids, context)

        depth_by_id = {c.product_id: c.relaxation_depth for c in ranked.candidates}
        pool = CandidatePoolResult(
            candidates=tuple(
                RankedProductCandidate(
                    product=product,
                    relaxation_depth=depth_by_id[product.product_id],
                )
                for product in hydrated
            ),
            eligible_count=len(searched.candidates),
            was_relaxed=searched.was_relaxed,
            stop_reason=searched.stop_reason,
            semantic_used=ranked.semantic_used,
        )
        logger.info(
            "candidate_pool_completed",
            store_id=context.store_id,
            commerce_category=resolved.request.commerce_category,
            commerce_subcategory=resolved.request.commerce_subcategory,
            eligible_count=pool.eligible_count,
            candidate_count=len(pool.candidates),
            stale_dropped_count=pool.stale_dropped_count,
            was_relaxed=pool.was_relaxed,
            stop_reason=str(pool.stop_reason),
            semantic_used=pool.semantic_used,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return pool

    def _namespace(self, context: RetailerContext) -> str:
        """Derived from the request's scope, never from anything a caller said."""
        if self._pinecone is None:
            return _NO_NAMESPACE
        return self._pinecone.namespace_for(context.store_id)


def _require_conserved(
    eligible_ids: tuple[int, ...],
    ranked_ids: tuple[int, ...],
    context: RetailerContext,
) -> None:
    """Ranking returned the same products, in some order.

    Count equality is not enough. A ranking of `[1, 2, 999]` over an eligible
    `[1, 2, 3]` has the right length and the wrong products, and `[1, 1, 2]`
    has duplicated one while losing another - both would be invisible to a
    length check and both would show the customer something nothing qualified.

    Set equality plus no duplicates is what actually says "reordered".
    """
    if (
        len(ranked_ids) == len(eligible_ids)
        and len(set(ranked_ids)) == len(ranked_ids)
        and set(ranked_ids) == set(eligible_ids)
    ):
        return
    logger.error(
        "ranking_candidate_population_changed",
        store_id=context.store_id,
        eligible_count=len(eligible_ids),
        ranked_count=len(ranked_ids),
        duplicate_ranked=len(ranked_ids) - len(set(ranked_ids)),
        missing_count=len(set(eligible_ids) - set(ranked_ids)),
        unexpected_count=len(set(ranked_ids) - set(eligible_ids)),
    )
    raise RankingIntegrityError(
        eligible_count=len(eligible_ids), ranked_count=len(ranked_ids)
    )


def _summarise(
    searched: ControlledSearchResult,
) -> tuple[RelaxationSummaryItem, ...]:
    """The widenings that ended up applied, one per bound that moved.

    Later steps supersede earlier ones - a budget widened twice ended at the
    second figure - so the last change for each bound is the one a reply
    should quote. Reading the recorded changes rather than recomputing keeps
    M8 the only place a percentage is applied.
    """
    latest: dict[tuple[RelaxableField, DimensionRole | None], AppliedRelaxation] = {}
    for attempt in searched.attempts:
        for change in attempt.changes:
            role = change.role if isinstance(change, DimensionRelaxationChange) else None
            latest[(change.field, role)] = change
    return tuple(_summary_item(change) for change in latest.values())


def _summary_item(change: AppliedRelaxation) -> RelaxationSummaryItem:
    if isinstance(change, DimensionRelaxationChange):
        return RelaxationSummaryItem(
            field=RelaxableField.DIMENSION,
            role=change.role,
            strength=change.strength,
            original_value=_render(
                change.original_min_cm, change.original_max_cm, change.original_target_cm
            ),
            applied_value=_render(change.applied_min_cm, change.applied_max_cm, None),
        )
    return RelaxationSummaryItem(
        field=change.field,
        strength=change.strength,
        original_value=str(change.from_value),
        applied_value=str(change.to_value),
    )


def _render(
    minimum: Decimal | None, maximum: Decimal | None, target: Decimal | None
) -> str:
    """A measurement as the customer would recognise it.

    A widened target executes as a band, so both edges are shown; a single
    bound shows its one figure. Nothing is recomputed - every value here was
    recorded by the policy that applied it.
    """
    if target is not None:
        return str(target)
    if minimum is not None and maximum is not None:
        return f"{minimum}-{maximum}"
    return str(maximum if maximum is not None else minimum)
