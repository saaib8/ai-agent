"""Turning a verified room plan into verified retailer products.

Deterministic application code, and the seam between two milestones that must
not leak into each other. The specialist has already decided *what the room
needs*; discovery already knows *how to find products*. This bridges them and
adds no judgement of its own: no model is called, no language is read, and
nothing here re-interprets the plan it was given.

What that rules out is the whole point. A design brief saying "pet friendly" or
"keep the centre open" is prose the specialist already turned into
machine-actionable needs; re-parsing it here would invent constraints nobody
approved (CLAUDE.md 4, 8). A whole-room budget is not a per-item ceiling, and
applying it to every need would constrain nothing while quietly hiding the
products a room actually needs. Room measurements are not product bounds, since
a dimension's meaning is not a product's placement (CLAUDE.md 15.1). None of
those three becomes a filter here, and each has a guard test saying so.

Every product lookup goes through `ProductSearchPipeline.execute_candidate_pool`
- one need, one search - so M6, M8 and M9 keep their single implementation and
this service holds no repository, no index and no SQL.

The needs are independent, so they may be searched side by side
(`agents.room_search_concurrency`). A database session serves one task at a
time, so each concurrent search runs on a pipeline of its own (`fresh`), and
the results keep the plan's order: the room is the same either way.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from typing import Protocol

from app.core.exceptions import (
    EmbeddingUnavailableError,
    InvalidRequestError,
    UnknownCommerceCategoryError,
)
from app.core.logging import get_logger
from app.integrations.embeddings import BatchQueryEmbedder
from app.schemas.design import (
    DesignCategoryNeed,
    DesignTask,
    InteriorDesignRequest,
    InteriorDesignResult,
)
from app.schemas.design_discovery import (
    DesignDiscoveryResult,
    DesignNeedCandidates,
    DesignNeedSkipReason,
)
from app.schemas.design_override import DesignNeedSearchOverride
from app.schemas.discovery import ProductSearchRequest, SeatingCapacityConstraint
from app.schemas.product import ProductCandidate
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    RankingLean,
    ResolvedSearch,
)
from app.schemas.refinement import SemanticIntentOp
from app.schemas.resolution import CandidatePoolResult, RankedProductCandidate
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerContext
from app.services.embedding_prefetch import prefetched
from app.services.query_document import build_query_document, has_semantic_intent
from app.services.search_pipeline import ProductSearchPipeline
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.registry import CommerceTaxonomy
from app.taxonomy.seating import SeatingSemantics

logger = get_logger(__name__)


class PoolSource(Protocol):
    """What discovery reads products through: the search pipeline's pools."""

    async def execute_candidate_pool(
        self, resolved: ResolvedSearch, context: RetailerContext
    ) -> CandidatePoolResult: ...

    async def execute_forced_pool(
        self, product_id: int, context: RetailerContext
    ) -> CandidatePoolResult: ...


FreshPools = Callable[[], AbstractAsyncContextManager[PoolSource]]
"""A pool source on a database session of its own, closed on exit."""


class DesignDiscoveryService:
    """A room plan in, verified candidate products per need out."""

    def __init__(
        self,
        pipeline: ProductSearchPipeline,
        taxonomy: CommerceTaxonomy,
        seating: SeatingSemantics | None = None,
        attributes: CatalogAttributes | None = None,
        *,
        fresh: FreshPools | None = None,
        concurrency: int = 1,
        embedder: BatchQueryEmbedder | None = None,
    ) -> None:
        self._pipeline: PoolSource = pipeline
        self._fresh = fresh
        self._concurrency = max(1, concurrency)
        """How many needs are searched at once; 1, or no `fresh`, is one after
        another on the request's own pipeline."""
        self._embedder = embedder
        """Embeds every piece's ranking text in one request before the searches
        (`app/services/embedding_prefetch.py`); None, each embeds its own."""
        self._taxonomy = taxonomy
        self._seating = seating
        """Reviewed seat counts. A one-seat type never carries a seat filter:
        the catalog records no capacity for chairs, so one could only hide them
        all (CLAUDE.md 7)."""
        self._attributes = attributes
        """Reads a product's stored colour and styles as approved values, for
        what a designed piece should avoid."""

    async def discover(
        self,
        request: InteriorDesignRequest,
        result: InteriorDesignResult,
        context: RetailerContext,
        *,
        overrides: Mapping[int, DesignNeedSearchOverride] | None = None,
    ) -> DesignDiscoveryResult:
        """One candidate pool per need, in the plan's own order.

        Takes the request as well as the result because only the request knows
        what was asked for: the task, the retailer's capabilities, and the
        design preferences that the specialist is deliberately forbidden to
        echo back (CLAUDE.md 17.2).

        Needs are searched one at a time. That is the simplest strategy that is
        correct, and no evidence yet justifies a concurrency policy
        (CLAUDE.md 19).

        `overrides` adjust individual searches during a refinement - excluding
        a product the customer turned down, bounding a price against the one
        they are replacing, or running with different wording. Keyed by
        position in this plan, so a need without one searches exactly as the
        plan describes it.
        """
        if request.task is not DesignTask.ROOM_PLAN:
            # Advice is not a shopping request. Searching because guidance came
            # back would turn "what goes with walnut?" into a product listing.
            raise InvalidRequestError(
                reason="product discovery runs for a room plan, not for design advice"
            )
        capabilities = request.catalog_capabilities
        assert capabilities is not None, "a room plan carries capabilities"

        started = time.perf_counter()

        async def piece(
            pipeline: PoolSource, index: int, need: DesignCategoryNeed
        ) -> tuple[DesignNeedCandidates, float]:
            piece_started = time.perf_counter()
            entry = await self._for_need(
                pipeline, need, index, capabilities, request, context, (overrides or {}).get(index)
            )
            ms = round((time.perf_counter() - piece_started) * 1000, 1)
            logger.info(
                "room_piece_searched",
                store_id=context.store_id,
                need_index=index,
                commerce_subcategory=need.commerce_subcategory,
                candidate_count=entry.candidate_count,
                elapsed_ms=ms,
            )
            return entry, ms

        fresh = self._fresh
        side_by_side_used = fresh is not None and self._concurrency > 1 and len(result.needs) > 1
        vectors, batch_failed = await self._vectors_ahead(
            result, capabilities, request, overrides or {}, context
        )
        with prefetched(vectors, failed=batch_failed):
            if fresh is None or not side_by_side_used:
                found = [
                    await piece(self._pipeline, index, need)
                    for index, need in enumerate(result.needs)
                ]
            else:
                found = await self._side_by_side(fresh, result.needs, piece)
        entries = [entry for entry, _ in found]
        piece_ms = [ms for _, ms in found]

        discovered = DesignDiscoveryResult(needs=tuple(entries))
        logger.info(
            "design_discovery_completed",
            store_id=context.store_id,
            need_count=len(discovered.needs),
            pipeline_execution_count=discovered.searched_count,
            skipped_count=len(discovered.needs) - discovered.searched_count,
            candidate_counts=[entry.candidate_count for entry in discovered.needs],
            empty_need_count=sum(1 for entry in discovered.needs if entry.candidate_count == 0),
            # Wall time beside the sum of the pieces': equal while the pieces
            # are searched one after another, less when side by side.
            concurrency=self._concurrency if side_by_side_used else 1,
            pieces_ms=round(sum(piece_ms), 1),
            slowest_piece_ms=max(piece_ms, default=0.0),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return discovered

    async def _vectors_ahead(
        self,
        result: InteriorDesignResult,
        capabilities: RetailerCatalogCapabilities,
        request: InteriorDesignRequest,
        overrides: Mapping[int, DesignNeedSearchOverride],
        context: RetailerContext,
    ) -> tuple[dict[str, tuple[float, ...]], bool]:
        """Every piece's ranking text embedded in one request, ahead of the
        searches - or nothing, when there is one text or no batch embedder -
        and whether that request failed.

        A failed batch is not a failed room: the provider is down, so every
        search keeps deterministic order at once (CLAUDE.md 16.1) instead of
        each waiting out the same timeout.
        """
        if self._embedder is None:
            return {}, False
        documents: list[str] = []
        for index, need in enumerate(result.needs):
            override = overrides.get(index)
            if override is not None and override.forced_product_id is not None:
                continue
            if not capabilities.supports(need.commerce_category, need.commerce_subcategory):
                continue
            self._require_approved(need)
            resolved = self._resolve(need, request, override)
            if has_semantic_intent(resolved):
                documents.append(build_query_document(resolved))
        documents = list(dict.fromkeys(documents))
        if len(documents) < 2:
            return {}, False
        try:
            vectors = await self._embedder.embed_queries(documents)
        except EmbeddingUnavailableError as exc:
            logger.warning(
                "room_vectors_ahead_skipped",
                store_id=context.store_id,
                document_count=len(documents),
                reason=exc.code,
            )
            return {}, True
        return dict(zip(documents, vectors, strict=True)), False

    async def _side_by_side(
        self,
        fresh: FreshPools,
        needs: Sequence[DesignCategoryNeed],
        piece: Callable[
            [PoolSource, int, DesignCategoryNeed],
            Awaitable[tuple[DesignNeedCandidates, float]],
        ],
    ) -> list[tuple[DesignNeedCandidates, float]]:
        """Every need searched, at most `concurrency` at once.

        One search runs on the request's own pipeline, whose connection the
        request already holds, and the rest each on a fresh session - so a
        room holds `concurrency` connections, never one more. One failed search
        stops the room with its own error, the rest cancelled; a turn being
        stopped stays a stop, whatever a cancelled search raised on the way out.
        """
        gate = asyncio.Semaphore(self._concurrency)
        home_free = True

        async def one(index: int, need: DesignCategoryNeed) -> tuple[DesignNeedCandidates, float]:
            nonlocal home_free
            async with gate:
                if home_free:
                    home_free = False
                    try:
                        return await piece(self._pipeline, index, need)
                    finally:
                        home_free = True
                try:
                    async with fresh() as pipeline:
                        return await piece(pipeline, index, need)
                except Exception:
                    # A search being stopped - a sibling failed, or the turn ran
                    # out of time - ends as a stop, whatever its session raised
                    # on the way out, so it never hides the real failure.
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise asyncio.CancelledError from None
                    raise

        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(one(index, need)) for index, need in enumerate(needs)]
        except ExceptionGroup as failed:
            first, *rest = failed.exceptions
            if rest:
                logger.warning(
                    "room_piece_searches_failed",
                    error_types=sorted({type(e).__name__ for e in failed.exceptions}),
                )
            raise first from None
        return [task.result() for task in tasks]

    async def _for_need(
        self,
        pipeline: PoolSource,
        need: DesignCategoryNeed,
        index: int,
        capabilities: RetailerCatalogCapabilities,
        request: InteriorDesignRequest,
        context: RetailerContext,
        override: DesignNeedSearchOverride | None = None,
    ) -> DesignNeedCandidates:
        """Validate the need, then search for it exactly as stated.

        Two checks, and they fail differently on purpose. An **unapproved
        taxonomy value** is an invariant violation: the specialist validates
        its own output, so one arriving here means that validation was bypassed,
        and a value outside the vocabulary must never reach SQL or be repaired
        to the nearest approved one (CLAUDE.md 14.3). It raises.

        A type the retailer **cannot supply** is not a violation. The
        specialist drops those, so one arriving here means the catalog changed
        after capabilities were read. It is recorded as a skipped need and the
        rest of the plan proceeds.
        """
        self._require_approved(need)

        if not capabilities.supports(need.commerce_category, need.commerce_subcategory):
            logger.info(
                "design_need_unsupported_by_retailer",
                store_id=context.store_id,
                commerce_category=need.commerce_category,
                commerce_subcategory=need.commerce_subcategory,
            )
            return DesignNeedCandidates(
                need_index=index,
                need=need,
                skipped=DesignNeedSkipReason.RETAILER_CANNOT_SUPPLY,
            )

        if override is not None and override.forced_product_id is not None:
            # The customer chose a specific product for this role from its own
            # alternatives, so there is nothing to search: the pool is that one
            # product, or empty if the catalog no longer carries it. Verifying it
            # belongs to this need happened before the override was built.
            pool = await pipeline.execute_forced_pool(override.forced_product_id, context)
            return DesignNeedCandidates(need_index=index, need=need, pool=pool)

        pool = await pipeline.execute_candidate_pool(
            self._resolve(need, request, override), context
        )
        if need.design is None:
            return DesignNeedCandidates(need_index=index, need=need, pool=pool)
        return self._designed(index, need, pool, request)

    def _designed(
        self,
        index: int,
        need: DesignCategoryNeed,
        pool: CandidatePoolResult,
        request: InteriorDesignRequest,
    ) -> DesignNeedCandidates:
        """A designed piece's candidates, in the order the room is chosen from.

        Nothing is left out here: whether a piece suits the room is Nora's
        judgement, made when she reviews it (CLAUDE.md 10.1). The tier holds
        only facts about what the piece was designed to be - a type that seats
        where its types seat differently, its size in her range, nothing she
        said to avoid, then the type she listed first - and the pool's own
        order (relaxation depth, then the look by meaning) decides within it.
        Colour and style are never compared by stored name here: they reach
        the ranking as wording, so a Sage rug can serve an olive palette.
        """
        design = need.design
        assert design is not None
        types = (need.commerce_subcategory, *design.alternatives)
        mixed_seats = self._seating is not None and (
            len({self._seating.counts_as_seats(t) for t in types}) > 1
        )
        kept: list[tuple[tuple[int, ...], int, RankedProductCandidate]] = []
        for position, candidate in enumerate(pool.candidates):
            product = candidate.product
            colour = self._canonical(AttributeFamily.COLOR, product.main_color)
            own_styles = {
                s for s in (self._canonical(AttributeFamily.STYLE, v) for v in product.styles) if s
            }
            kind = product.commerce.subcategory
            # The first three are the piece's standing: share spending never
            # moves a piece out of it (`bundle_optimizer._ATTRIBUTES`).
            tier = (
                int(mixed_seats and not self._counts(kind)),
                max(
                    _size_tier(_longer_side(product), design.longer_min_cm, design.longer_max_cm),
                    _size_tier(
                        _shorter_side(product), design.shorter_min_cm, design.shorter_max_cm
                    ),
                ),
                int(
                    (colour is not None and colour in design.avoid_colours)
                    or bool(own_styles & set(design.avoid_styles))
                ),
                types.index(kind) if kind in types else len(types),
                candidate.relaxation_depth,
            )
            kept.append((tier, position, candidate))
        kept.sort(key=lambda entry: (entry[0], entry[1]))
        return DesignNeedCandidates(
            need_index=index,
            need=need,
            pool=pool.model_copy(update={"candidates": tuple(c for _, _, c in kept)}),
            tiers=tuple(t for t, _, _ in kept),
        )

    def _counts(self, kind: str | None) -> bool:
        return self._seating is None or self._seating.counts_as_seats(kind)

    def _palette(self, request: InteriorDesignRequest, family: AttributeFamily) -> tuple[str, ...]:
        return tuple(
            p.canonical_value
            for p in request.design_preferences
            if p.family is family and p.canonical_value is not None
        )

    def _canonical(self, family: AttributeFamily, value: str | None) -> str | None:
        if value is None:
            return None
        if self._attributes is None:
            return value
        return self._attributes.canonical(family, value)

    def _capacity(self, need: DesignCategoryNeed) -> SeatingCapacityConstraint | None:
        """The need's seat filter - none for a type that seats one by nature,
        whose catalog rows record no capacity for a filter to match."""
        if self._seating is not None and self._seating.seats_one(need.commerce_subcategory):
            return None
        return need.seating_capacity

    def _require_approved(self, need: DesignCategoryNeed) -> None:
        if need.commerce_subcategory is None:
            if not self._taxonomy.is_category(need.commerce_category):
                raise UnknownCommerceCategoryError(category=need.commerce_category)
            return
        self._taxonomy.validate_pair(need.commerce_category, need.commerce_subcategory)

    def resolve_need(
        self, need: DesignCategoryNeed, request: InteriorDesignRequest
    ) -> ResolvedSearch:
        """One need as a search, for a caller that is not building a room.

        The same conversion the room path uses, exposed because a complementary
        recommendation runs a single need through the ordinary search pipeline
        rather than through the optimiser - one rug to look at, not a package
        to price (M14 21).
        """
        return self._resolve(need, request)

    def _resolve(
        self,
        need: DesignCategoryNeed,
        request: InteriorDesignRequest,
        override: DesignNeedSearchOverride | None = None,
    ) -> ResolvedSearch:
        """The need, copied into a search. Nothing added, nothing derived.

        Three deliberate absences, each of which would be a defect:

        * **No price.** The room's budget constrains the room, not every item
          in it. Copying SAR 15,000 onto the sofa, the rug and the lamp alike
          would bound nothing and would look as though it had. Allocation is
          the optimiser's, with the whole plan in view (CLAUDE.md 27).
        * **No dimensions.** Room geometry is not a product bound, and no
          targeted horizontal applicability contract exists (CLAUDE.md 15.1).
        * **No sort, limit or exclusions.** The pool is unbounded and ranked;
          inventing an order or a ceiling here would decide the room's options
          before the optimiser saw them.

        Two separate things reach ranking, and neither is derived from the
        other. The room's colour and style leanings arrive as preferences; this
        need's own qualitative character arrives as `semantic_text`, straight
        from the structured field the specialist filled in. Nothing is composed
        out of the brief, the guidance, a measurement or an anchor - the
        specialist already did that reasoning, and re-reading its prose here
        would be this service interpreting language.
        """
        design = need.design
        preferences = request.design_preferences
        wording = _wording(need, override)
        lean: RankingLean | None = None
        if design is not None:
            # A designed piece's look - its own colours and styles, else the
            # room's palette - is wording, ranked by meaning; never a stored
            # name matched first (CLAUDE.md 10.1). Sara's searches keep 16.1.
            colours = design.colours or self._palette(request, AttributeFamily.COLOR)
            styles = design.styles or self._palette(request, AttributeFamily.STYLE)
            wording = _look_words(colours, styles, wording)
            preferences = ()
            if design.avoid_colours or design.avoid_styles or design.size_target_cm():
                target = design.size_target_cm()
                lean = RankingLean(
                    avoid_colours=design.avoid_colours,
                    avoid_styles=design.avoid_styles,
                    size_target_cm=Decimal(target) if target is not None else None,
                )
        return ResolvedSearch(
            request=ProductSearchRequest(
                commerce_category=need.commerce_category,
                commerce_subcategory=need.commerce_subcategory,
                # Every type the piece may be, searched together - the same
                # seat rule applies to them alike (checked when designed).
                alongside_subcategories=design.alternatives if design is not None else (),
                seating_capacity=self._capacity(need),
                # Both come from the application: a price derived from the
                # product being replaced, and products the customer already
                # turned down for this role. Neither was authored by a model,
                # and neither is a design fact (CLAUDE.md 12.1).
                price=override.price if override else None,
                exclude_product_ids=(override.exclude_product_ids if override else ()),
            ),
            semantics=_semantics_for(need, self._capacity(need)),
            # Colour and style the customer leaned towards, reaching ranking
            # and never SQL. Filtering on them would hide products they never
            # ruled out (CLAUDE.md 12.4).
            semantic_preferences=preferences,
            lean=lean,
            # This piece's design character, for the query embedding alone. It
            # reaches no filter, no bound and no relaxation policy.
            semantic_text=wording,
        )


def _look_words(colours: tuple[str, ...], styles: tuple[str, ...], feel: str | None) -> str | None:
    """A designed piece's look as search wording: its colours, its styles and
    its feel, in plain words ("Olive Beige Modern Classic, soft and low")."""
    words = [*colours, *(s.replace("_", " ") for s in styles)]
    look = " ".join(words)
    joined = ", ".join(part for part in (look, feel) if part)
    return joined or None


def _longer_side(product: ProductCandidate) -> Decimal | None:
    sides = [v for v in (product.dimensions.length_cm, product.dimensions.width_cm) if v]
    return max(sides) if sides else None


def _shorter_side(product: ProductCandidate) -> Decimal | None:
    sides = [v for v in (product.dimensions.length_cm, product.dimensions.width_cm) if v]
    return min(sides) if len(sides) == 2 else None


def _size_tier(longer: Decimal | None, low: int | None, high: int | None) -> int:
    """In the designed range, then not listed, then outside it."""
    if low is None and high is None:
        return 0
    if longer is None:
        return 1
    if (low is not None and longer < low) or (high is not None and longer > high):
        return 2
    return 0


def _semantics_for(
    need: DesignCategoryNeed, capacity: SeatingCapacityConstraint | None
) -> ConstraintSemantics:
    """Everything a design plan states is LOCKED.

    A design-derived constraint is a conclusion, not a turn of phrase. "This
    room needs seating for four" was reasoned about; there is no softening
    language to read, and nothing entitles a relaxation policy to widen it
    because too few products matched. A three-seat sofa does not satisfy a plan
    that called for four, and returning one because the catalog was thin would
    quietly answer a different question (CLAUDE.md 13.1).

    The category carries no strength at all and is never relaxable. The
    subcategory's is recorded as LOCKED and, for now, no policy widens a
    subcategory anyway (CLAUDE.md 13.5).
    """
    return ConstraintSemantics(
        subcategory=(ConstraintStrength.LOCKED if need.commerce_subcategory is not None else None),
        seating_min=(
            ConstraintStrength.LOCKED
            if capacity is not None and capacity.min_capacity is not None
            else None
        ),
        seating_max=(
            ConstraintStrength.LOCKED
            if capacity is not None and capacity.max_capacity is not None
            else None
        ),
    )


def _wording(need: DesignCategoryNeed, override: DesignNeedSearchOverride | None) -> str | None:
    """This search's qualitative wording: the plan's, or the one staged over it.

    Replaced rather than combined. A customer refining "visually light" to
    "more minimal" has changed their mind about the direction, and running both
    at once would search for a thing they never described.
    """
    if override is None or override.semantic_intent is None:
        return need.semantic_intent
    refinement = override.semantic_intent
    return refinement.value if refinement.op is SemanticIntentOp.SET else None
