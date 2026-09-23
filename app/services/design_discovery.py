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
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from app.core.exceptions import InvalidRequestError, UnknownCommerceCategoryError
from app.core.logging import get_logger
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
from app.schemas.discovery import ProductSearchRequest
from app.schemas.query import ConstraintSemantics, ConstraintStrength, ResolvedSearch
from app.schemas.refinement import SemanticIntentOp
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerContext
from app.services.search_pipeline import ProductSearchPipeline
from app.taxonomy.registry import CommerceTaxonomy

logger = get_logger(__name__)


class DesignDiscoveryService:
    """A room plan in, verified candidate products per need out."""

    def __init__(self, pipeline: ProductSearchPipeline, taxonomy: CommerceTaxonomy) -> None:
        self._pipeline = pipeline
        self._taxonomy = taxonomy

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
        entries: list[DesignNeedCandidates] = []
        for index, need in enumerate(result.needs):
            entries.append(
                await self._for_need(
                    need,
                    index,
                    capabilities,
                    request,
                    context,
                    (overrides or {}).get(index),
                )
            )

        discovered = DesignDiscoveryResult(needs=tuple(entries))
        logger.info(
            "design_discovery_completed",
            store_id=context.store_id,
            need_count=len(discovered.needs),
            pipeline_execution_count=discovered.searched_count,
            skipped_count=len(discovered.needs) - discovered.searched_count,
            candidate_counts=[entry.candidate_count for entry in discovered.needs],
            empty_need_count=sum(1 for entry in discovered.needs if entry.candidate_count == 0),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return discovered

    async def _for_need(
        self,
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
            pool = await self._pipeline.execute_forced_pool(
                override.forced_product_id, context
            )
            return DesignNeedCandidates(need_index=index, need=need, pool=pool)

        pool = await self._pipeline.execute_candidate_pool(
            self._resolve(need, request, override), context
        )
        return DesignNeedCandidates(need_index=index, need=need, pool=pool)

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
        return ResolvedSearch(
            request=ProductSearchRequest(
                commerce_category=need.commerce_category,
                commerce_subcategory=need.commerce_subcategory,
                seating_capacity=need.seating_capacity,
                # Both come from the application: a price derived from the
                # product being replaced, and products the customer already
                # turned down for this role. Neither was authored by a model,
                # and neither is a design fact (CLAUDE.md 12.1).
                price=override.price if override else None,
                exclude_product_ids=(override.exclude_product_ids if override else ()),
            ),
            semantics=_semantics_for(need),
            # Colour and style the customer leaned towards, reaching ranking
            # and never SQL. Filtering on them would hide products they never
            # ruled out (CLAUDE.md 12.4).
            semantic_preferences=request.design_preferences,
            # This piece's design character, for the query embedding alone. It
            # reaches no filter, no bound and no relaxation policy.
            semantic_text=_wording(need, override),
        )


def _semantics_for(need: DesignCategoryNeed) -> ConstraintSemantics:
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
    capacity = need.seating_capacity
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
