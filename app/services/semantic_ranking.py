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

from collections import deque
from collections.abc import Callable, Sequence
from decimal import Decimal
from itertools import groupby

from app.core.config import SizeSettings
from app.core.exceptions import (
    EmbeddingUnavailableError,
    SemanticIndexUnavailableError,
)
from app.core.logging import get_logger
from app.integrations.embeddings import QueryEmbedder
from app.integrations.pinecone import SemanticIndex
from app.schemas.discovery import ProductSort
from app.schemas.query import RankingLean, ResolvedSearch
from app.schemas.relaxation import RelaxedCandidate
from app.schemas.retailer import RetailerContext
from app.schemas.semantic import (
    QUERY_REPRESENTATION_VERSION,
    SemanticRankedCandidate,
    SemanticRankingResult,
    SemanticSkipReason,
)
from app.services.product_size import made_to_fit, sides_of, size_distance
from app.services.query_document import build_query_document, has_semantic_intent
from app.taxonomy.attributes import AttributeFamily

logger = get_logger(__name__)

_SORT_KEYS: dict[ProductSort, bool] = {
    ProductSort.PRICE_ASC: False,
    ProductSort.PRICE_DESC: True,
}


_MatchRank = Callable[[RelaxedCandidate], tuple[int, ...]]


def _preference_match(resolved: ResolvedSearch, size: SizeSettings) -> _MatchRank:
    """How well a product matches what they leaned towards, as a sort key.

    First whether it fits the space they said it goes in: pieces that fit
    first, pieces of unknown size next, wider ones last - in view, never
    hidden (CLAUDE.md 10.10). Then its stored colour or style - 0 when it is
    one they are drawn to.
    Then the seats, closest to how many usually sit there first: exactly that
    many, then the fewest extra seats, then pieces whose seats were never
    recorded, then pieces that seat fewer - a 2-seater for two before a
    4-seater, and unverified pieces still in view (CLAUDE.md 10.5, 13.5).
    Either key is 0 for everyone when they said nothing of it, which leaves
    the order untouched.

    Then what they said they would rather avoid, then what they liked, picked
    or asked more like of (phase 5), and then a
    designer's direction, below everything the customer said: its colours,
    its styles, what it would avoid, and the size it would sit near
    (docs/designer-led-shopping-plan.md, 4.2)."""
    colour_or_style = _colour_or_style_match(resolved)
    lean = resolved.lean
    said_avoided = _avoided(
        lean.said_avoid_colours if lean else (), lean.said_avoid_styles if lean else ()
    )
    learned_colour = _matches(lean.learned_colours if lean else (), colour=True)
    learned_style = _matches(lean.learned_styles if lean else (), colour=False)
    direction_colour = _matches(lean.colours if lean else (), colour=True)
    direction_style = _matches(lean.styles if lean else (), colour=False)
    avoided = _avoided(lean.avoid_colours if lean else (), lean.avoid_styles if lean else ())
    fits = _fits(lean)
    sized = _size_band(lean, size)
    in_the_pick = _fits_the_pick(lean, size)
    people = resolved.seat_preference

    def seats(candidate: RelaxedCandidate) -> tuple[int, int]:
        capacity = candidate.product.seating_capacity
        if people is None:
            return (0, 0)
        if capacity is None:
            return (1, 0)
        if capacity >= people:
            return (0, capacity - people)
        return (2, people - capacity)

    return lambda candidate: (
        in_the_pick(candidate),
        fits(candidate),
        colour_or_style(candidate),
        *seats(candidate),
        said_avoided(candidate),
        learned_colour(candidate),
        learned_style(candidate),
        direction_colour(candidate),
        direction_style(candidate),
        avoided(candidate),
        sized(candidate),
    )


def _matches(values: tuple[str, ...], *, colour: bool) -> Callable[[RelaxedCandidate], int]:
    """0 for a product in one of these colours (or styles), 1 otherwise - 0
    for everyone when there are none."""
    if not values:
        return lambda _: 0
    wanted = set(values)
    if colour:
        return lambda c: int(c.product.main_color not in wanted)
    return lambda c: int(not any(s in wanted for s in c.product.styles))


def _avoided(
    colours: tuple[str, ...], styles: tuple[str, ...]
) -> Callable[[RelaxedCandidate], int]:
    """1 for a product in a colour or style to avoid - after the rest, never
    out."""
    if not (colours or styles):
        return lambda _: 0
    avoid_colours, avoid_styles = set(colours), set(styles)
    return lambda c: int(
        c.product.main_color in avoid_colours or any(s in avoid_styles for s in c.product.styles)
    )


def _fits(lean: RankingLean | None) -> Callable[[RelaxedCandidate], int]:
    """0 for a piece whose longer side fits the space they gave, 1 for one
    with no usable size, 2 for one wider - 0 for everyone with no space."""
    space = lean.space_cm if lean is not None else None
    if space is None:
        return lambda _: 0

    def fit(candidate: RelaxedCandidate) -> int:
        long_side = candidate.product.long_side_cm
        if long_side is None:
            return 1
        return 0 if long_side <= space else 2

    return fit


def _fits_the_pick(
    lean: RankingLean | None, size: SizeSettings
) -> Callable[[RelaxedCandidate], int]:
    """0 for a piece in the size that goes in the pick - a mattress in this
    bed's size - 1 for any other size, 2 for one with no usable size; 0 for
    everyone when there is no such size. First, before anything else: a
    mattress that does not fit the bed is no choice at all."""
    target = lean.fit_side_cm if lean is not None else None
    if target is None:
        return lambda _: 0

    def fit(candidate: RelaxedCandidate) -> int:
        side = candidate.product.short_side_cm
        if side is None:
            return 2
        return 0 if made_to_fit(side, target, size) else 1

    return fit


def _size_band(lean: RankingLean | None, size: SizeSettings) -> Callable[[RelaxedCandidate], int]:
    """0 near the size asked for, 1 close, 2 further, 3 with no usable size -
    an implausible stored side is no size at all (`SizeSettings`)."""
    target = lean.size_target_cm if lean is not None else None
    if target is None:
        return lambda _: 0

    def band(candidate: RelaxedCandidate) -> int:
        product = candidate.product
        sides = (
            sides_of(product.long_side_cm, product.short_side_cm, size)
            if product.long_side_cm is not None and product.short_side_cm is not None
            else None
        )
        distance = size_distance(sides.long_cm if sides else None, target)
        if distance is None:
            return 3
        return 0 if distance <= size.near_share else 1 if distance <= size.close_share else 2

    return band


def _taking_turns(
    ordered: Sequence[RelaxedCandidate], resolved: ResolvedSearch, matches: _MatchRank
) -> list[RelaxedCandidate]:
    """A search covering several types, its types taken in turn - the type
    asked for first, then each type beside it - so a page of sofas also shows
    sofa sets and sectionals.

    Only among products equally good for what the customer asked: the same
    depth and the same match on their seats, colours, styles and space. A
    better match is never moved below a worse one, and each type keeps its own
    order. Never with an explicit sort, which callers apply instead: "the
    cheapest" means the cheapest, whatever its type (CLAUDE.md 16.1). Nor when
    the order is the customer's own description - their wording, a wish or a
    strict colour no approved value names - which only similarity can rank:
    there the closest stays first, whatever its type.
    """
    request = resolved.request
    if not request.alongside_subcategories or _ordered_by_their_words(resolved):
        return list(ordered)
    types = request.subcategories
    taken: list[RelaxedCandidate] = []
    for _, run in groupby(ordered, key=lambda c: (c.relaxation_depth, matches(c))):
        queues: dict[str | None, deque[RelaxedCandidate]] = {t: deque() for t in types}
        unlisted: list[RelaxedCandidate] = []
        for candidate in run:
            queue = queues.get(candidate.product.subcategory)
            if queue is None:
                unlisted.append(candidate)
            else:
                queue.append(candidate)
        while any(queues.values()):
            for queue in queues.values():
                if queue:
                    taken.append(queue.popleft())
        taken.extend(unlisted)
    return taken


def _ordered_by_their_words(resolved: ResolvedSearch) -> bool:
    """Whether similarity carries something the customer described that no
    approved value expresses."""
    return bool(
        resolved.semantic_text
        or resolved.unmatched_strict
        or any(p.canonical_value is None for p in resolved.semantic_preferences)
    )


def _colour_or_style_match(resolved: ResolvedSearch) -> Callable[[RelaxedCandidate], int]:
    """0 for a product whose stored colour or style is one the customer is
    drawn to, 1 otherwise - or 0 for everyone when no preference names an
    approved value."""
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
        self,
        embedder: QueryEmbedder | None,
        index: SemanticIndex | None,
        size: SizeSettings | None = None,
    ) -> None:
        # Both None when semantic ranking is not configured. That is a running
        # state, not a broken one.
        self._embedder = embedder
        self._index = index
        self._size = size or SizeSettings()

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
        matches = _preference_match(resolved, self._size)
        if sort in _SORT_KEYS:
            ordered = self._by_explicit_sort(candidates, scores, matches, reverse=_SORT_KEYS[sort])
        else:
            ordered = _taking_turns(
                self._by_depth_then_similarity(candidates, scores, matches), resolved, matches
            )
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

    def _deterministic(
        self,
        resolved: ResolvedSearch,
        candidates: Sequence[RelaxedCandidate],
        reason: SemanticSkipReason,
    ) -> SemanticRankingResult:
        """The eligible set in the order M6/M8 already produced. Nothing dropped.

        Preference matches still lead, stably, so "dark grey" puts the grey
        sofas first even when the index cannot be reached.
        """
        matches = _preference_match(resolved, self._size)
        if resolved.request.sort in _SORT_KEYS:
            # The pool already arrives in the customer's price order; keep it
            # within each match group, exactly as the scored path orders an
            # explicit sort, so both paths agree on the same pool.
            ordered = sorted(candidates, key=matches)
        else:
            ordered = _taking_turns(
                sorted(candidates, key=lambda c: (c.relaxation_depth, matches(c))),
                resolved,
                matches,
            )
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
