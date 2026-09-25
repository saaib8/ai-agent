"""Semantic ranking: order, never eligibility.

PostgreSQL has already decided which products a customer may see. This service
decides only the sequence, and it is built so that it cannot do more: the
candidate list it receives is the candidate list it returns, reordered.

Three rules carry the design.

* **Exact fidelity outranks similarity absolutely.** A product that satisfied
  the customer's own request is never displaced by one that needed a widened
  bound, however much an embedding prefers it. Depths are ranked in separate
  buckets rather than blended, so no weighting can be tuned into existence.
* **An explicit sort is the customer's instruction, not a hint.** "Cheapest"
  means cheapest; similarity only orders products the sort leaves tied.
* **Every failure degrades to deterministic order.** Ranking is an
  enhancement, so an unreachable provider costs the ordering, never the
  results (CLAUDE.md 21).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal

from app.core.exceptions import (
    EmbeddingUnavailableError,
    SemanticIndexUnavailableError,
)
from app.core.logging import get_logger
from app.integrations.embeddings import QueryEmbedder
from app.integrations.pinecone import SemanticIndex
from app.schemas.discovery import ProductSort
from app.schemas.query import ResolvedSearch
from app.schemas.relaxation import RelaxedCandidate
from app.schemas.retailer import RetailerContext
from app.schemas.semantic import (
    QUERY_REPRESENTATION_VERSION,
    SemanticRankedCandidate,
    SemanticRankingResult,
    SemanticSkipReason,
)
from app.services.query_document import build_query_document, has_semantic_intent
from app.taxonomy.attributes import AttributeFamily

logger = get_logger(__name__)

_SORT_KEYS: dict[ProductSort, bool] = {
    ProductSort.PRICE_ASC: False,
    ProductSort.PRICE_DESC: True,
}


_MatchRank = Callable[[RelaxedCandidate], int]


def _preference_match(resolved: ResolvedSearch) -> _MatchRank:
    """0 for a product whose stored colour or style is one the customer is
    drawn to, 1 otherwise - or 0 for everyone when no preference names an
    approved value, which leaves the order untouched."""
    colors = {
        p.canonical_value
        for p in resolved.semantic_preferences
        if p.family is AttributeFamily.COLOR and p.canonical_value
    }
    styles = {
        p.canonical_value
        for p in resolved.semantic_preferences
        if p.family is AttributeFamily.STYLE and p.canonical_value
    }
    if not colors and not styles:
        return lambda _: 0

    def rank(candidate: RelaxedCandidate) -> int:
        product = candidate.product
        hit = product.main_color in colors or any(s in styles for s in product.styles)
        return 0 if hit else 1

    return rank


class SemanticRankingService:
    def __init__(
        self, embedder: QueryEmbedder | None, index: SemanticIndex | None
    ) -> None:
        # Both None when semantic ranking is not configured. That is a running
        # state, not a broken one.
        self._embedder = embedder
        self._index = index

    async def rank(
        self,
        resolved: ResolvedSearch,
        candidates: Sequence[RelaxedCandidate],
        context: RetailerContext,
        *,
        namespace: str,
    ) -> SemanticRankingResult:
        """Order the eligible candidates. Returns them all, every time."""
        if not candidates:
            # Nothing to order, so no provider is called to order it.
            return self._deterministic(resolved, candidates, SemanticSkipReason.NOTHING_TO_RANK)
        if self._embedder is None or self._index is None:
            return self._deterministic(resolved, candidates, SemanticSkipReason.NOT_CONFIGURED)
        if not has_semantic_intent(resolved):
            # A purely structural request. An embedding would impose an order,
            # not discover one, at the cost of two network calls.
            return self._deterministic(resolved, candidates, SemanticSkipReason.NO_SEMANTIC_INTENT)

        document = build_query_document(resolved)
        try:
            vector = await self._embedder.embed_query(document)
        except EmbeddingUnavailableError as exc:
            logger.warning("semantic_ranking_skipped", reason=exc.code)
            return self._deterministic(
                resolved, candidates, SemanticSkipReason.EMBEDDING_UNAVAILABLE
            )

        eligible = [c.product.product_id for c in candidates]
        try:
            scores = await self._index.score(
                vector=vector,
                namespace=namespace,
                store_id=context.store_id,
                product_ids=eligible,
            )
        except SemanticIndexUnavailableError as exc:
            logger.warning("semantic_ranking_skipped", reason=exc.code)
            return self._deterministic(resolved, candidates, SemanticSkipReason.INDEX_UNAVAILABLE)

        unexpected = set(scores) - set(eligible)
        if unexpected:
            # One id outside the eligible set means the scope cannot be
            # trusted, so the whole ranking is discarded rather than filtered
            # down to the part that happens to look right.
            logger.error(
                "semantic_ranking_integrity_failure",
                store_id=context.store_id,
                namespace=namespace,
                unexpected_count=len(unexpected),
            )
            return self._deterministic(resolved, candidates, SemanticSkipReason.INTEGRITY_FAILURE)

        ordered = self._order(resolved, candidates, scores)
        logger.info(
            "semantic_ranking_completed",
            store_id=context.store_id,
            eligible_count=len(eligible),
            scored_count=len(scores),
            unvectorised_count=len(eligible) - len(scores),
            query_representation_version=QUERY_REPRESENTATION_VERSION,
            embedding_model=self._embedder.model,
            sort=resolved.request.sort.value,
        )
        return SemanticRankingResult(
            candidates=ordered,
            semantic_used=True,
            embedding_model=self._embedder.model,
            ranked_count=len(scores),
        )

    # ── ordering ────────────────────────────────────────────────────────────

    def _order(
        self,
        resolved: ResolvedSearch,
        candidates: Sequence[RelaxedCandidate],
        scores: dict[int, float],
    ) -> tuple[SemanticRankedCandidate, ...]:
        sort = resolved.request.sort
        matches = _preference_match(resolved)
        if sort in _SORT_KEYS:
            ordered = self._by_explicit_sort(candidates, scores, matches, reverse=_SORT_KEYS[sort])
        else:
            ordered = self._by_depth_then_similarity(candidates, scores, matches)
        return tuple(
            SemanticRankedCandidate(
                product_id=c.product.product_id,
                relaxation_depth=c.relaxation_depth,
                semantic_similarity=scores.get(c.product.product_id),
                semantic_rank=position,
            )
            for position, c in enumerate(ordered)
        )

    @staticmethod
    def _by_depth_then_similarity(
        candidates: Sequence[RelaxedCandidate],
        scores: dict[int, float],
        matches: _MatchRank,
    ) -> list[RelaxedCandidate]:
        """Depth buckets, preference matches first, similarity, id to settle.

        Matching first is deterministic on the stored colour and style: an
        embedding alone can rank a beige sofa above a grey one for "dark grey",
        because to a vector charcoal and warm stone are near neighbours.

        A product with no vector sorts below the scored ones in its own bucket
        and keeps its deterministic place among them: a missing vector is an
        indexing gap, and treating it as similarity zero would read as a
        judgement nobody made.
        """
        return sorted(
            candidates,
            key=lambda c: (
                c.relaxation_depth,
                matches(c),
                0 if c.product.product_id in scores else 1,
                -scores.get(c.product.product_id, 0.0),
                c.product.product_id,
            ),
        )

    @staticmethod
    def _by_explicit_sort(
        candidates: Sequence[RelaxedCandidate],
        scores: dict[int, float],
        matches: _MatchRank,
        *,
        reverse: bool,
    ) -> list[RelaxedCandidate]:
        """Preference matches first, then the customer's sort; similarity
        only settles equal prices.

        "The cheapest beige one" means the cheapest of the beige ones - a
        cheaper grey sofa ahead of them would answer a different question. The
        rest still follow in price order, so nothing is hidden.

        Deliberately not a semantic shortlist followed by a price sort: that
        can hide a genuinely cheaper eligible product behind a less similar
        one, which answers a question they did not ask.
        """
        sign = Decimal(-1) if reverse else Decimal(1)
        return sorted(
            candidates,
            key=lambda c: (
                matches(c),
                sign * c.product.price_amount,
                0 if c.product.product_id in scores else 1,
                -scores.get(c.product.product_id, 0.0),
                c.product.product_id,
            ),
        )

    @staticmethod
    def _deterministic(
        resolved: ResolvedSearch, candidates: Sequence[RelaxedCandidate], reason: SemanticSkipReason
    ) -> SemanticRankingResult:
        """The eligible set in the order M6/M8 already produced. Nothing dropped.

        Preference matches still lead, stably, so "dark grey" puts the grey
        sofas first even when the index cannot be reached.
        """
        matches = _preference_match(resolved)
        if resolved.request.sort in _SORT_KEYS:
            # The pool already arrives in the customer's price order; keep it
            # within each match group, exactly as the scored path orders an
            # explicit sort, so both paths agree on the same pool.
            ordered = sorted(candidates, key=matches)
        else:
            ordered = sorted(candidates, key=lambda c: (c.relaxation_depth, matches(c)))
        return SemanticRankingResult(
            candidates=tuple(
                SemanticRankedCandidate(
                    product_id=c.product.product_id,
                    relaxation_depth=c.relaxation_depth,
                    semantic_similarity=None,
                    semantic_rank=position,
                )
                for position, c in enumerate(ordered)
            ),
            semantic_used=False,
            skip_reason=reason,
        )
