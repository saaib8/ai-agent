"""One customer turn, orchestrated deterministically.

The reasoning happened before this: the decision service said what the turn
should do. Everything here is ordinary Python composing services that already
exist, in an order the locked rules fix. No model is consulted after the
decision except M7, which interprets the customer's words into a search.

Three properties carry the design, and each is a rule that would be easy to
lose by accident.

**Every selector resolves against the pre-turn state.** "The second one" means
the second of what the customer is looking at *now*, not of results this turn
is about to produce. So the state the resolvers see is snapshotted before
anything changes.

**A search promotes only after it runs.** The candidate criteria stay local
until the pipeline returns. Promoting first would leave new criteria beside the
previous result set - a state that validates structurally and is a lie - so the
promotion and the commit happen together and nothing between them escapes.

**A handled failure still returns a state.** There is no persistence, so an
immutable state that is not returned is lost. A search that fails must still
hand back the selection the customer made and the preferences they stated
before it failed. Only an internal defect raises.

The turn asks at most one question. Several can be discovered - an ambiguous
selection, a budget with no currency - and the highest-priority one is chosen
here, not by whichever branch happened to run last.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from app.core.exceptions import (
    IntegrationUnavailableError,
    LLMResponseInvalidError,
)
from app.core.logging import get_logger
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarificationReason,
    CustomerAgentDecision,
    FollowUpPolicy,
    ProductInteractionOp,
)
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import (
    CustomerTurnInput,
    CustomerTurnResult,
    DecisionInput,
    TurnGrounding,
)
from app.schemas.agent_updates import (
    ActiveSearchUpdate,
    AddItems,
    AgentStateUpdate,
    ClearSemanticIntent,
    ProductInteractionUpdate,
    RemoveItems,
    ReplaceItems,
    SetSemanticIntent,
)
from app.schemas.comparison import ProductComparisonResult
from app.schemas.composition import (
    ComposedSearch,
    CompositionFailed,
    CompositionNeedsClarification,
    CompositionOutcome,
    NewTaskRequired,
)
from app.schemas.grounding import (
    GroundedProduct,
    SearchExecutionGrounding,
    TurnFailure,
    TurnFailureCode,
)
from app.schemas.product_reference import ProductReferenceSelector
from app.schemas.query import (
    ClarificationReason,
    ClarificationRequired,
    QueryInterpretation,
    ResolvedSearch,
    UnresolvedStrictRequirement,
    UnsupportedDimensionRequirement,
    UnsupportedRequirement,
)
from app.schemas.refinement import PriceRefinementOp, SearchRefinementDelta
from app.schemas.resolution import (
    _UNAVAILABILITY_REASONS,
    ComparisonFailureReason,
    ComparisonUnavailable,
    DeterministicClarification,
    ReferenceFailureReason,
    ReferenceUnresolved,
    RelativePriceFailureReason,
    RelativePriceUnresolved,
    SearchRequirementClarificationReason,
    SimilarSearchUnavailable,
)
from app.schemas.retailer import RetailerContext
from app.services.agent_state import (
    NO_RESULTS_REVISION,
    apply_update,
    commit_search_results,
)
from app.services.agent_view import project_state
from app.services.comparison import ProductComparisonService
from app.services.customer_decision import CustomerAgentDecisionService
from app.services.grounding_builder import to_grounded_product
from app.services.hydration import ProductHydrationService
from app.services.proposal_mapping import map_proposals
from app.services.query_understanding import QueryUnderstandingService
from app.services.reference_resolver import ProductReferenceResolver
from app.services.refinement_composer import SearchRefinementComposer
from app.services.relative_price import RelativePriceResolver
from app.services.search_pipeline import ProductSearchPipeline
from app.services.similar_search import SimilarSearchBuilder

logger = get_logger(__name__)

_HANDLED_SEARCH_FAILURES = (IntegrationUnavailableError, LLMResponseInvalidError)
"""Operational failures a turn reports and survives.

An unreachable catalog, index or provider, and a provider answer that does not
satisfy its schema: in every case the search could not run *now*, and the
customer's other work this turn must not be thrown away with it.

Deliberately not here: `RankingIntegrityError`, taxonomy errors, state
invariant violations and anything unexpected. Those mean the system is wrong
rather than unavailable, and dressing one as "search unavailable" would hide a
defect behind a retry-later (CLAUDE.md 21).
"""

_M7_CLARIFICATION = {
    ClarificationReason.NO_COMMERCE_CATEGORY: (
        BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE
    ),
    ClarificationReason.MULTIPLE_PRODUCT_TYPES: (
        BlockingClarificationReason.MULTIPLE_PRODUCT_TYPES
    ),
    ClarificationReason.MISSING_PRICE_CURRENCY: (
        BlockingClarificationReason.MISSING_PRICE_CURRENCY
    ),
    ClarificationReason.MISSING_DIMENSION_ROLE: (
        BlockingClarificationReason.MISSING_DIMENSION_ROLE
    ),
    ClarificationReason.MISSING_DIMENSION_UNIT: (
        BlockingClarificationReason.MISSING_DIMENSION_UNIT
    ),
}
"""Query understanding's reasons in the conversational vocabulary.

Two enums rather than one because they answer to different owners: M7 says why
a *search request* could not be built, and this says what the *customer* is
being asked. The mapping is total over `ClarificationReason`.
"""

_M7_UNSUPPORTED: dict[type, SearchRequirementClarificationReason] = {
    UnsupportedRequirement: (
        SearchRequirementClarificationReason.UNSUPPORTED_REQUIREMENT
    ),
    UnsupportedDimensionRequirement: (
        SearchRequirementClarificationReason.UNSUPPORTED_DIMENSION_REQUIREMENT
    ),
    UnresolvedStrictRequirement: (
        SearchRequirementClarificationReason.UNRESOLVED_STRICT_REQUIREMENT
    ),
}
"""M7 outcomes that are executable but would not answer what was asked.

Each keeps its own reason. They look alike from here - all three stop a search
that could technically run - but they are different things to say: one filter
does not exist, one product type's stored axes cannot be trusted, and one value
is outside the vocabulary. The reply has to distinguish them, so this layer
must not.

Keyed on the outcome type, never on the customer's words: the typed result is
what M7 decided, and re-reading the message here could disagree with it.

The reasons come from the application-only enum. A decision model cannot name
any of them, so it cannot claim a requirement is unsupported before the search
that would establish it has run.
"""


@dataclass(frozen=True, slots=True)
class _Primary:
    """What the turn's primary action produced.

    `state` is always the next state, whatever else happened, because a handled
    failure still has to return one.
    """

    state: AgentStateV1
    search: SearchExecutionGrounding | None = None
    product_detail: GroundedProduct | None = None
    comparison: ProductComparisonResult | None = None
    failure: TurnFailure | None = None
    clarification: DeterministicClarification | None = None
    design_handoff: bool = False


@dataclass(frozen=True, slots=True)
class _Interaction:
    """The optional side effect, and why it may not have happened."""

    state: AgentStateV1
    applied: bool = False
    clarification: DeterministicClarification | None = None
    failure: TurnFailure | None = None


class CustomerTurnCoordinator:
    """Composes one customer turn. Generates no prose."""

    def __init__(
        self,
        decisions: CustomerAgentDecisionService,
        query_understanding: QueryUnderstandingService,
        composer: SearchRefinementComposer,
        references: ProductReferenceResolver,
        relative_price: RelativePriceResolver,
        comparison: ProductComparisonService,
        pipeline: ProductSearchPipeline,
        hydration: ProductHydrationService,
        similar_search: SimilarSearchBuilder,
    ) -> None:
        self._decisions = decisions
        self._query_understanding = query_understanding
        self._composer = composer
        self._references = references
        self._relative_price = relative_price
        self._comparison = comparison
        self._pipeline = pipeline
        self._hydration = hydration
        self._similar_search = similar_search

    async def run(self, turn: CustomerTurnInput) -> CustomerTurnResult:
        """One turn in, the next state and what happened out.

        A decision failure propagates: there is no decision to report and
        nothing succeeded, so there is no result to build (CLAUDE.md 21).
        """
        started = time.perf_counter()
        pre_turn = turn.state

        decision = await self._decisions.decide(
            DecisionInput(
                message=turn.message,
                conversation=turn.conversation,
                state_view=project_state(pre_turn),
            )
        )

        interaction = await self._apply_interaction(decision, pre_turn, turn.context)
        primary = await self._execute(decision, interaction.state, pre_turn, turn)

        proposals = map_proposals(decision.state_proposal, decision.commerce_proposal)
        # Applied to what the primary action produced, not to the pre-turn
        # state: rebuilding from the start here would silently undo a
        # successful search or a resolved selection.
        final_state = apply_update(primary.state, proposals.update)

        grounding = self._ground(decision, primary, interaction, proposals.clarification)
        self._log(decision, pre_turn, final_state, primary, interaction, started)
        return CustomerTurnResult(
            state=final_state, decision=decision, grounding=grounding
        )

    # ── the optional interaction ────────────────────────────────────────────

    async def _apply_interaction(
        self,
        decision: CustomerAgentDecision,
        pre_turn: AgentStateV1,
        context: RetailerContext,
    ) -> _Interaction:
        """Resolve and apply the side effect, against the pre-turn universe.

        Applied before the primary action so that a selection made from the
        current results survives a search that replaces them.
        """
        if decision.interaction is None:
            return _Interaction(state=pre_turn)

        outcome = await self._references.resolve(
            decision.interaction.reference, pre_turn, context
        )
        if isinstance(outcome, ReferenceUnresolved):
            clarification, failure = _reference_outcome(
                outcome.reason, BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE
            )
            # Value first: the interaction is optional, so a primary action
            # that does not depend on it still runs (locked M11A).
            return _Interaction(
                state=pre_turn, clarification=clarification, failure=failure
            )

        update = _interaction_update(
            decision.interaction.op, outcome.product_id, pre_turn
        )
        return _Interaction(
            state=apply_update(pre_turn, AgentStateUpdate(product_interaction=update)),
            applied=True,
        )

    # ── the primary action ──────────────────────────────────────────────────

    async def _execute(
        self,
        decision: CustomerAgentDecision,
        working: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        match decision.action:
            case AgentAction.ANSWER | AgentAction.CLARIFY:
                return _Primary(state=working)
            case AgentAction.DESIGN_HANDOFF:
                # A marker only. M12 owns execution, and building half an
                # InteriorDesignRequest would invent that boundary early.
                return _Primary(state=working, design_handoff=True)
            case AgentAction.SEARCH:
                if decision.reference is None:
                    return await self._new_search(decision, working, turn)
                return await self._similar_search_turn(
                    decision.reference, working, pre_turn, turn
                )
            case AgentAction.REFINE_SEARCH:
                return await self._refine(decision, working, pre_turn, turn)
            case AgentAction.PRODUCT_DETAIL:
                return await self._product_detail(decision, working, pre_turn, turn)
            case AgentAction.COMPARE:
                return await self._compare(decision, working, pre_turn, turn)

    # ── search ──────────────────────────────────────────────────────────────

    async def _new_search(
        self,
        decision: CustomerAgentDecision,
        working: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """A genuinely new task. M7 reads the current message and nothing else."""
        interpretation = await self._interpret(turn.message)
        if isinstance(interpretation, TurnFailure):
            return _Primary(state=working, failure=interpretation)
        if not isinstance(interpretation, ResolvedSearch):
            return _Primary(
                state=working, clarification=_interpretation_clarification(interpretation)
            )
        return await self._seed_and_execute(interpretation, decision, working, turn)

    async def _seed_and_execute(
        self,
        resolved: ResolvedSearch,
        decision: CustomerAgentDecision,
        working: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """Seed a new task and run it.

        This turn's proposals are passed explicitly rather than persisted
        first: "I usually prefer Modern, show me sofas" must seed this search,
        and persisting the preference early would change what a failed turn
        leaves behind.
        """
        composed = self._composer.seed_new_task(
            resolved,
            proposal=decision.state_proposal,
            room_preferences=(
                working.room_project.design_preferences if working.room_project else ()
            ),
            customer_defaults=working.customer_preferences.semantic_preferences,
            semantic_intent=(
                decision.new_search.semantic_intent if decision.new_search else None
            ),
            revision=_current_revision(working),
        )
        return await self._run_search(composed, working, turn.context)

    async def _similar_search_turn(
        self,
        reference: ProductReferenceSelector,
        working: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """"Something like the second one" - a new task seeded from a product.

        Structural and built from reviewed catalog facts. M7 is never called:
        the reference's own classification says what to search for, and asking
        a model to re-derive it from the product's name is exactly what the
        agent service must not do (CLAUDE.md 6.1).
        """
        outcome = await self._references.resolve(reference, pre_turn, turn.context)
        if isinstance(outcome, ReferenceUnresolved):
            clarification, failure = _reference_outcome(
                outcome.reason, BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE
            )
            return _Primary(state=working, clarification=clarification, failure=failure)

        products = await self._hydration.hydrate_ids((outcome.product_id,), turn.context)
        if not products:
            return _Primary(
                state=working,
                failure=TurnFailure(code=TurnFailureCode.PRODUCT_UNAVAILABLE),
            )

        seed = self._similar_search.build(products[0])
        if isinstance(seed, SimilarSearchUnavailable):
            # The product is fine - it was just read from the catalog. What
            # cannot be done is build a search from it, because its reviewed
            # classification is absent or outside the approved vocabulary. So
            # this is the *search* that is unavailable, not the product, and
            # saying otherwise would tell the customer something untrue about
            # something they can still see. Falling back to a generic search
            # would answer a different question (CLAUDE.md 6.1).
            logger.warning("similar_search_unbuildable", reason=str(seed.reason))
            return _Primary(
                state=working,
                failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE),
            )

        composed = self._composer.seed_new_task(
            seed.resolved,
            room_preferences=(
                working.room_project.design_preferences if working.room_project else ()
            ),
            customer_defaults=working.customer_preferences.semantic_preferences,
            revision=_current_revision(working),
        )
        return await self._run_search(composed, working, turn.context)

    async def _run_search(
        self,
        composed: ComposedSearch,
        working: AgentStateV1,
        context: RetailerContext,
    ) -> _Primary:
        """Execute, then promote and commit in one step.

        The candidate is local until the pipeline returns. On success the
        criteria are promoted through the reducer - which carries the current
        revision forward - and `commit_search_results` advances it exactly
        once, atomically with the results it belongs to.
        """
        try:
            execution = await self._pipeline.execute(
                composed.resolved,
                context,
                dropped_constraints=composed.dropped_constraints,
            )
        except _HANDLED_SEARCH_FAILURES:
            logger.warning("turn_search_unavailable", store_id=context.store_id)
            return _Primary(
                state=working,
                failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE),
            )

        promoted = apply_update(
            working,
            AgentStateUpdate(
                active_search=ActiveSearchUpdate(
                    request=composed.candidate.request,
                    semantics=composed.candidate.semantics,
                    semantic_preferences=ReplaceItems(
                        items=composed.candidate.semantic_preferences
                    ),
                    semantic_intent=_intent_update(composed.candidate.semantic_intent),
                )
            ),
        )
        return _Primary(
            state=commit_search_results(promoted, execution.presented_product_ids),
            search=execution.grounding,
        )

    # ── refinement ──────────────────────────────────────────────────────────

    async def _refine(
        self,
        decision: CustomerAgentDecision,
        working: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        delta = decision.refinement or SearchRefinementDelta()
        if delta.price is not None and delta.price.op is PriceRefinementOp.SET_RELATIVE:
            rewritten = await self._resolve_relative_price(delta, pre_turn, turn.context)
            if isinstance(rewritten, _Primary):
                return replace(rewritten, state=working)
            delta = rewritten

        if decision.taxonomy_change_requested:
            return await self._refine_taxonomy(decision, delta, working, turn)

        return await self._compose_and_run(
            self._composer.refine(working.active_search, delta), working, turn
        )

    async def _refine_taxonomy(
        self,
        decision: CustomerAgentDecision,
        delta: SearchRefinementDelta,
        working: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """The product type changed. M7 names it; nothing else of M7 is used.

        Only the approved pair crosses over. Importing M7's price, measurements
        or - above all - its transient `semantic_text` would let a message read
        only to say "make them sectionals" replace the conversation's durable
        intent.
        """
        interpretation = await self._interpret(turn.message)
        if isinstance(interpretation, TurnFailure):
            return _Primary(state=working, failure=interpretation)
        if not isinstance(interpretation, ResolvedSearch):
            return _Primary(
                state=working,
                clarification=_interpretation_clarification(interpretation),
            )

        outcome = self._composer.refine_taxonomy(
            working.active_search,
            commerce_category=interpretation.request.commerce_category,
            commerce_subcategory=interpretation.request.commerce_subcategory,
            delta=delta,
        )
        if isinstance(outcome, NewTaskRequired):
            # A different product family is a different task, so nothing of the
            # old one carries across (CLAUDE.md 13.3).
            return await self._seed_and_execute(
                interpretation, decision, working, turn
            )
        return await self._compose_and_run(outcome, working, turn)

    async def _compose_and_run(
        self,
        outcome: CompositionOutcome,
        working: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        match outcome:
            case ComposedSearch():
                return await self._run_search(outcome, working, turn.context)
            case CompositionNeedsClarification():
                return _Primary(
                    state=working,
                    clarification=DeterministicClarification(reason=outcome.reason),
                )
            case CompositionFailed():
                # The composer was asked to do something it must refuse: a
                # refinement with nothing active, an unapproved attribute
                # value, an unresolved relative price. Each means the decision
                # did not match the state it was shown, which is not a question
                # anyone can put to a customer.
                logger.error("turn_composition_defect", defect=str(outcome.defect))
                raise LLMResponseInvalidError(
                    reason=f"composition refused: {outcome.defect}"
                )
            case NewTaskRequired():  # pragma: no cover - only from refine_taxonomy
                raise LLMResponseInvalidError(reason="unexpected new-task outcome")

    async def _resolve_relative_price(
        self,
        delta: SearchRefinementDelta,
        pre_turn: AgentStateV1,
        context: RetailerContext,
    ) -> SearchRefinementDelta | _Primary:
        """"Cheaper than the second one" becomes an ordinary bound.

        Resolved against the pre-turn state, so the ordinal still means what
        the customer meant. The arithmetic belongs to the resolver, which reads
        the reference's verified price; nothing is computed here.
        """
        assert delta.price is not None and delta.price.relative is not None
        outcome = await self._relative_price.resolve(
            delta.price.relative,
            pre_turn,
            context,
            active_currency=_active_currency(pre_turn),
        )
        if isinstance(outcome, RelativePriceUnresolved):
            clarification, failure = _relative_price_outcome(outcome)
            return _Primary(
                state=pre_turn, clarification=clarification, failure=failure
            )
        return delta.model_copy(update={"price": outcome.price})

    # ── product detail ──────────────────────────────────────────────────────

    async def _product_detail(
        self,
        decision: CustomerAgentDecision,
        working: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        assert decision.reference is not None
        outcome = await self._references.resolve(
            decision.reference, pre_turn, turn.context
        )
        if isinstance(outcome, ReferenceUnresolved):
            clarification, failure = _reference_outcome(
                outcome.reason, BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE
            )
            return _Primary(state=working, clarification=clarification, failure=failure)

        products = await self._hydration.hydrate_ids((outcome.product_id,), turn.context)
        if not products:
            # Read a moment ago and gone now. Focusing it would point the
            # conversation at something that no longer exists.
            return _Primary(
                state=working,
                failure=TurnFailure(code=TurnFailureCode.PRODUCT_UNAVAILABLE),
            )

        return _Primary(
            state=apply_update(
                working,
                AgentStateUpdate(
                    product_interaction=ProductInteractionUpdate(
                        focused_product_id=outcome.product_id
                    )
                ),
            ),
            product_detail=to_grounded_product(
                products[0],
                grounding_ref=1,
                # Neither is known: no search returned this product, and it
                # holds no position in this turn's result list.
                presented_ordinal=None,
                relaxation_depth=None,
            ),
        )

    # ── comparison ──────────────────────────────────────────────────────────

    async def _compare(
        self,
        decision: CustomerAgentDecision,
        working: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """Every reference against the same pre-turn state, in the order asked.

        One unresolved reference stops the whole comparison: continuing with
        the rest would answer a different question from the one put.
        """
        product_ids: list[int] = []
        for selector in decision.comparison_references:
            outcome = await self._references.resolve(selector, pre_turn, turn.context)
            if isinstance(outcome, ReferenceUnresolved):
                clarification, failure = _reference_outcome(
                    outcome.reason,
                    BlockingClarificationReason.AMBIGUOUS_COMPARATIVE_REFERENCE,
                    unavailable_code=TurnFailureCode.COMPARISON_TARGET_UNAVAILABLE,
                )
                return _Primary(
                    state=working, clarification=clarification, failure=failure
                )
            product_ids.append(outcome.product_id)

        comparison = await self._comparison.compare(product_ids, turn.context)
        if isinstance(comparison, ComparisonUnavailable):
            clarification, failure = _comparison_outcome(comparison)
            return _Primary(
                state=working, clarification=clarification, failure=failure
            )
        return _Primary(state=working, comparison=comparison)

    # ── query understanding ─────────────────────────────────────────────────

    async def _interpret(self, message: str) -> QueryInterpretation | TurnFailure:
        """M7 on the current message alone.

        No history and no state: it interprets what was just said, and giving
        it the conversation would make the same sentence mean different things
        on different turns.
        """
        try:
            return await self._query_understanding.interpret(message)
        except _HANDLED_SEARCH_FAILURES:
            logger.warning("turn_query_understanding_unavailable")
            return TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE)

    # ── grounding ───────────────────────────────────────────────────────────

    def _ground(
        self,
        decision: CustomerAgentDecision,
        primary: _Primary,
        interaction: _Interaction,
        proposal_clarification: DeterministicClarification | None,
    ) -> TurnGrounding:
        """One question, chosen by priority rather than by branch order.

        A model's own clarification outranks anything a service discovered;
        the primary action outranks a proposal; a proposal outranks an optional
        side effect. Whatever is not asked is not lost - the conversation
        continues, and a later turn can raise it again.

        A handled failure is not a question and does not become one, so it
        suppresses every clarification and any follow-up.
        """
        failure = primary.failure or interaction.failure
        deterministic: DeterministicClarification | None = None
        if decision.clarification is None and failure is None:
            deterministic = (
                primary.clarification
                or proposal_clarification
                or interaction.clarification
            )

        asking = decision.clarification is not None or deterministic is not None
        return TurnGrounding(
            search=primary.search,
            product_detail=primary.product_detail,
            comparison=primary.comparison,
            clarification=decision.clarification,
            deterministic_clarification=deterministic,
            failure=failure,
            design_handoff_requested=primary.design_handoff,
            follow_up_policy=(
                FollowUpPolicy.NONE
                if asking or failure is not None
                else decision.follow_up_policy
            ),
        )

    def _log(
        self,
        decision: CustomerAgentDecision,
        pre_turn: AgentStateV1,
        final_state: AgentStateV1,
        primary: _Primary,
        interaction: _Interaction,
        started: float,
    ) -> None:
        """Shape of the turn only: no message, no history, no ids (CLAUDE.md 22)."""
        logger.info(
            "customer_turn_completed",
            action=str(decision.action),
            had_interaction=decision.interaction is not None,
            interaction_applied=interaction.applied,
            search_outcome=(
                str(primary.search.outcome) if primary.search is not None else None
            ),
            product_detail=primary.product_detail is not None,
            comparison=primary.comparison is not None,
            design_handoff=primary.design_handoff,
            failure=str(primary.failure.code) if primary.failure else None,
            revision_before=_current_revision(pre_turn),
            revision_after=_current_revision(final_state),
            presented_before=len(pre_turn.product_interaction.presented_product_ids),
            presented_after=len(final_state.product_interaction.presented_product_ids),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )


# ── pure helpers ────────────────────────────────────────────────────────────


def _current_revision(state: AgentStateV1) -> int:
    return state.active_search.revision if state.active_search else NO_RESULTS_REVISION


def _active_currency(state: AgentStateV1) -> str | None:
    """The currency the running search is denominated in, if any.

    The retailer's currency is never inferred: an amount with no currency is a
    question, not a default (CLAUDE.md 15).
    """
    if state.active_search is None or state.active_search.request.price is None:
        return None
    return state.active_search.request.price.currency


def _intent_update(intent: str | None) -> SetSemanticIntent | ClearSemanticIntent:
    """The candidate's intent, set or cleared.

    Always one or the other, never omitted: omission means "leave it alone",
    which would carry a previous task's wording into a new search.
    """
    return ClearSemanticIntent() if intent is None else SetSemanticIntent(value=intent)


def _interaction_update(
    op: ProductInteractionOp, product_id: int, state: AgentStateV1
) -> ProductInteractionUpdate:
    """One atomic update per interaction.

    Deselecting needs care. If the product is focused and is *not* also
    presented, then removing the selection removes the only thing making the
    focus resolvable - so the focus must go in the same update. Doing it in two
    steps would build an intermediate state the reducer rejects, and clearing
    the focus unconditionally would drop a focus that was still perfectly valid
    because the product is still on screen.
    """
    interaction = state.product_interaction
    match op:
        case ProductInteractionOp.SELECT:
            return ProductInteractionUpdate(
                selected_product_ids=AddItems(items=(product_id,))
            )
        case ProductInteractionOp.FOCUS:
            return ProductInteractionUpdate(focused_product_id=product_id)
        case ProductInteractionOp.DESELECT:
            orphans_focus = (
                interaction.focused_product_id == product_id
                and product_id not in interaction.presented_product_ids
            )
            return ProductInteractionUpdate(
                selected_product_ids=RemoveItems(items=(product_id,)),
                clear_focus=orphans_focus,
            )


def _reference_outcome(
    reason: ReferenceFailureReason,
    clarification_reason: BlockingClarificationReason,
    *,
    unavailable_code: TurnFailureCode = TurnFailureCode.PRODUCT_UNAVAILABLE,
) -> tuple[DeterministicClarification | None, TurnFailure | None]:
    """A question, or a fact.

    "We could not tell which one you meant" is answerable. "That product is
    gone" is not, and asking about it would be pretending otherwise.
    """
    if reason in _UNAVAILABILITY_REASONS:
        return None, TurnFailure(code=unavailable_code)
    return (
        DeterministicClarification(
            reason=clarification_reason, reference_reason=reason
        ),
        None,
    )


def _relative_price_outcome(
    outcome: RelativePriceUnresolved,
) -> tuple[DeterministicClarification | None, TurnFailure | None]:
    match outcome.reason:
        case RelativePriceFailureReason.REFERENCE_UNRESOLVED:
            assert outcome.reference_reason is not None
            return _reference_outcome(
                outcome.reference_reason,
                BlockingClarificationReason.AMBIGUOUS_COMPARATIVE_REFERENCE,
            )
        case RelativePriceFailureReason.CURRENCY_CONFLICT:
            # Two currencies and no conversion. Which one they meant is theirs
            # to say; substituting one would change what they asked for.
            return (
                DeterministicClarification(
                    reason=BlockingClarificationReason.MISSING_REFINEMENT_CURRENCY,
                    relative_price_reason=outcome.reason,
                ),
                None,
            )
        case RelativePriceFailureReason.MALFORMED_PERCENT:
            raise LLMResponseInvalidError(reason="relative price percent malformed")


def _comparison_outcome(
    outcome: ComparisonUnavailable,
) -> tuple[DeterministicClarification | None, TurnFailure | None]:
    """Too many or repeated products is a question; an unreadable one is not."""
    if outcome.reason is ComparisonFailureReason.PRODUCT_UNAVAILABLE:
        return None, TurnFailure(code=TurnFailureCode.COMPARISON_TARGET_UNAVAILABLE)
    return (
        DeterministicClarification(
            reason=BlockingClarificationReason.COMPARISON_TARGETS
        ),
        None,
    )


def _interpretation_clarification(
    interpretation: QueryInterpretation,
) -> DeterministicClarification:
    """Query understanding's non-resolved outcomes, as one question.

    Every outcome keeps its own meaning. A clarification maps one-to-one, and
    so do the three unsupported kinds: they are executable but incomplete, and
    running them would present results as satisfying something that was never
    applied.
    """
    if isinstance(interpretation, ClarificationRequired):
        return DeterministicClarification(
            reason=_M7_CLARIFICATION[interpretation.reason]
        )
    return DeterministicClarification(reason=_M7_UNSUPPORTED[type(interpretation)])
