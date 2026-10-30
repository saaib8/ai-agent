"""Running a product search for an agent, and committing what it showed.

One path for every search an agent starts - a new one, the next page, one
card left out, more like a card - so each commits the same way: the criteria
through the reducer, then the results it really showed, atomically with the
revision they belong to. A search that cannot run leaves the state as it was.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.exceptions import IntegrationUnavailableError, LLMResponseInvalidError
from app.core.logging import get_logger
from app.schemas.agent_state import ActiveSearchState, AgentStateV1
from app.schemas.agent_updates import (
    ActiveSearchUpdate,
    AgentStateUpdate,
    ClearSemanticIntent,
    ReplaceItems,
    SetSemanticIntent,
)
from app.schemas.composition import ComposedSearch
from app.schemas.discovery import MAX_EXCLUDED_PRODUCT_IDS
from app.schemas.grounding import SearchExecutionGrounding
from app.schemas.query import RankingLean, ResolvedSearch
from app.schemas.retailer import RetailerContext
from app.services.agent_state import apply_update, commit_search_results, remember_measurements
from app.services.refinement_composer import SearchRefinementComposer
from app.services.search_pipeline import ProductSearchPipeline
from app.taxonomy.seating import SeatingSemantics

logger = get_logger(__name__)

NO_RESULTS_REVISION = 0

_UNAVAILABLE = (IntegrationUnavailableError, LLMResponseInvalidError)


@dataclass(frozen=True, slots=True)
class SearchRun:
    state: AgentStateV1
    grounding: SearchExecutionGrounding | None
    """None when the search could not run now."""


class SearchRunner:
    def __init__(
        self,
        pipeline: ProductSearchPipeline,
        composer: SearchRefinementComposer,
        seating: SeatingSemantics | None,
    ) -> None:
        self._pipeline = pipeline
        self._composer = composer
        self._seating = seating

    async def new(
        self,
        resolved: ResolvedSearch,
        state: AgentStateV1,
        context: RetailerContext,
        *,
        alongside: bool,
        earlier_sizes: bool = True,
        asked: bool = True,
    ) -> SearchRun:
        """A search the agent stated whole, seeded with the taste on record.

        `alongside` also shows the types reviewed to stand beside the asked
        one - sofa sets and sectionals beside sofas - when no size is asked,
        which those listings cannot answer. `earlier_sizes` brings back the
        sizes they gave for this type before, when this search states none
        (CLAUDE.md 13.5); false when they said any size is fine. `asked` is
        false for a search that is ours, not theirs - what goes with a pick -
        which neither brings back nor saves anyone's sizes.
        """
        room = state.room_project
        subcategory = resolved.request.commerce_subcategory
        saved = next(
            (
                m
                for m in state.customer_preferences.measurements_by_type
                if m.commerce_subcategory == subcategory
            ),
            None,
        )
        composed = self._composer.seed_new_task(
            resolved,
            room_preferences=room.design_preferences if room else (),
            customer_defaults=state.customer_preferences.semantic_preferences,
            saved_measurements=saved if earlier_sizes and asked else None,
            revision=state.active_search.revision if state.active_search else NO_RESULTS_REVISION,
        )
        if alongside:
            composed = self._with_family(composed)
        if resolved.customer_words:
            # The customer's own words travel with this search and its pages.
            words = {"customer_words": resolved.customer_words}
            composed = composed.model_copy(
                update={
                    "resolved": composed.resolved.model_copy(update=words),
                    "candidate": composed.candidate.model_copy(update=words),
                }
            )
        return await self.run(_with_avoids(composed, state), state, context, save_sizes=asked)

    async def again_excluding(
        self, excluded: tuple[int, ...], state: AgentStateV1, context: RetailerContext
    ) -> SearchRun | None:
        """The search on screen again, leaving more products out - the next
        page, or one card turned down. None with no search on screen."""
        active = state.active_search
        if active is None:
            return None
        merged = tuple(dict.fromkeys((*active.request.exclude_product_ids, *excluded)))
        request = active.request.model_copy(
            update={"exclude_product_ids": merged[-MAX_EXCLUDED_PRODUCT_IDS:]}
        )
        composed = ComposedSearch(
            candidate=active.model_copy(update={"request": request}),
            resolved=ResolvedSearch(
                request=request,
                semantics=active.semantics,
                semantic_preferences=active.semantic_preferences,
                seat_preference=active.seat_preference,
                lean=active.lean,
                semantic_text=active.semantic_intent,
                customer_words=active.customer_words,
            ),
        )
        return await self.run(_with_avoids(composed, state), state, context)

    async def run(
        self,
        composed: ComposedSearch,
        state: AgentStateV1,
        context: RetailerContext,
        *,
        save_sizes: bool = False,
    ) -> SearchRun:
        try:
            execution = await self._pipeline.execute(
                composed.resolved,
                context,
                dropped_constraints=composed.dropped_constraints,
                earlier_sizes_applied=composed.earlier_sizes_applied,
            )
        except _UNAVAILABLE:
            logger.warning("agent_search_unavailable", store_id=context.store_id)
            return SearchRun(state=state, grounding=None)
        candidate = composed.candidate
        promoted = apply_update(
            state,
            AgentStateUpdate(
                active_search=ActiveSearchUpdate(
                    request=candidate.request,
                    semantics=candidate.semantics,
                    semantic_preferences=ReplaceItems(items=candidate.semantic_preferences),
                    semantic_intent=(
                        ClearSemanticIntent()
                        if candidate.semantic_intent is None
                        else SetSemanticIntent(value=candidate.semantic_intent)
                    ),
                )
            ),
        )
        committed = _with_orderings(
            commit_search_results(promoted, execution.presented_product_ids), candidate
        )
        return SearchRun(
            state=remember_measurements(committed) if save_sizes else committed,
            grounding=execution.grounding,
        )

    def _with_family(self, composed: ComposedSearch) -> ComposedSearch:
        request = composed.resolved.request
        if (
            self._seating is None
            or request.single_type
            or request.dimensions
            or request.planar_dimensions is not None
        ):
            return composed
        family = self._seating.shown_with(request.commerce_subcategory)
        if not family:
            return composed
        widened = request.model_copy(update={"alongside_subcategories": family})
        return composed.model_copy(
            update={"resolved": composed.resolved.model_copy(update={"request": widened})}
        )


def _with_avoids(composed: ComposedSearch, state: AgentStateV1) -> ComposedSearch:
    """The search leaning away from what they said they would rather avoid -
    pushed down, never hidden (CLAUDE.md 12.4) - as it stands now, so an avoid
    said since the search began reaches its next page too."""
    said = state.customer_preferences
    lean = composed.resolved.lean or RankingLean()
    if (lean.said_avoid_colours, lean.said_avoid_styles) == (
        said.avoid_colours,
        said.avoid_styles,
    ):
        return composed
    leaning = lean.model_copy(
        update={"said_avoid_colours": said.avoid_colours, "said_avoid_styles": said.avoid_styles}
    )
    return composed.model_copy(
        update={
            "resolved": composed.resolved.model_copy(update={"lean": leaning}),
            "candidate": composed.candidate.model_copy(update={"lean": leaning}),
        }
    )


def _with_orderings(state: AgentStateV1, executed: ActiveSearchState) -> AgentStateV1:
    """The committed search, keeping how the executed one ordered its cards,
    so the next page is ordered the same way."""
    search = state.active_search
    if search is None:
        return state
    return state.model_copy(
        update={
            "active_search": search.model_copy(
                update={
                    "ordered_by_pick": executed.ordered_by_pick,
                    "from_product": executed.from_product,
                    "seat_preference": executed.seat_preference,
                    "lean": executed.lean,
                    "customer_words": executed.customer_words,
                }
            )
        }
    )
