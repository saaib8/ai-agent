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
from decimal import Decimal

from app.core.exceptions import (
    IntegrationUnavailableError,
    LLMResponseInvalidError,
    TaxonomyValidationError,
)
from app.core.logging import get_logger
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarificationReason,
    BundleInteractionIntent,
    BundleInteractionOp,
    BundleReplacementMode,
    CustomerAgentDecision,
    DesignScope,
    FollowUpPolicy,
    ProductInteractionOp,
)
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    RoomProjectState,
)
from app.schemas.agent_turn import (
    CustomerTurnInput,
    CustomerTurnResult,
    DecisionInput,
    TurnGrounding,
)
from app.schemas.agent_updates import (
    ActiveSearchUpdate,
    AddBundleLine,
    AddItems,
    AgentStateUpdate,
    BundleLineSpec,
    BundleOperation,
    ClearSemanticIntent,
    DesignNeedRefinement,
    DesignNeedSpec,
    PlannedBundleLineSpec,
    PreservedBundleLine,
    ProductInteractionUpdate,
    RefineBundle,
    RemoveItems,
    ReplaceDesignPlan,
    ReplaceItems,
    RoomProjectUpdate,
    SetBundleLineAcquisition,
    SetBundleLineQuantity,
    SetBundleLineStatus,
    SetSemanticIntent,
)
from app.schemas.bundle import (
    BundleOptimizationOutcome,
    BundleOptimizationRequest,
    BundleStatus,
    LockedBundleProduct,
    RoomBundle,
    UnmetReason,
)
from app.schemas.comparison import ProductComparisonResult
from app.schemas.composition import (
    ComposedSearch,
    CompositionFailed,
    CompositionNeedsClarification,
    CompositionOutcome,
    NewTaskRequired,
)
from app.schemas.design import (
    MAX_DESIGN_BRIEF_CHARS,
    DesignCategoryNeed,
    DesignRevisionContext,
    DesignTask,
    ExcludedDesignRole,
    InteriorDesignRequest,
    InteriorDesignResult,
)
from app.schemas.design_discovery import DesignDiscoveryResult
from app.schemas.design_override import DesignNeedSearchOverride
from app.schemas.discovery import MAX_EXCLUDED_PRODUCT_IDS, PriceConstraint
from app.schemas.grounding import (
    GroundedProduct,
    SearchExecutionGrounding,
    TurnFailure,
    TurnFailureCode,
)
from app.schemas.product import ProductCandidate
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
from app.schemas.refinement import (
    PriceRefinementOp,
    SearchRefinementDelta,
    SemanticIntentOp,
)
from app.schemas.resolution import (
    _UNAVAILABILITY_REASONS,
    BundleReferenceFailureReason,
    BundleReferenceUnresolved,
    ComparisonFailureReason,
    ComparisonUnavailable,
    DesignNeedFailureReason,
    DesignNeedUnresolved,
    DeterministicClarification,
    ReferenceFailureReason,
    ReferenceUnresolved,
    RelativePriceFailureReason,
    RelativePriceUnresolved,
    ResolvedBundleReference,
    SearchRequirementClarificationReason,
    SimilarSearchUnavailable,
)
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerContext
from app.services.agent_state import (
    NO_RESULTS_REVISION,
    apply_update,
    commit_search_results,
)
from app.services.agent_view import project_state
from app.services.bundle_optimizer import BundleOptimizer
from app.services.bundle_reference import BundleReferenceResolver
from app.services.catalog_capability import CatalogCapabilityService
from app.services.comparison import ProductComparisonService
from app.services.customer_decision import CustomerAgentDecisionService
from app.services.design_discovery import DesignDiscoveryService
from app.services.design_facts import project_anchors
from app.services.design_revision import (
    conflicting_role,
    project_current_plan,
    unprovable_against_exclusions,
)
from app.services.grounding_builder import to_grounded_product
from app.services.hydration import ProductHydrationService
from app.services.interior_design import InteriorDesignAgent
from app.services.proposal_mapping import map_proposals
from app.services.query_understanding import QueryUnderstandingService
from app.services.reference_resolver import ProductReferenceResolver
from app.services.refinement_composer import SearchRefinementComposer
from app.services.relative_price import RelativePriceResolver
from app.services.search_pipeline import ProductSearchPipeline
from app.services.similar_search import SimilarSearchBuilder
from app.taxonomy.dimensions import DimensionSemantics

logger = get_logger(__name__)

_HANDLED_DESIGN_FAILURES = (
    IntegrationUnavailableError,
    LLMResponseInvalidError,
    TaxonomyValidationError,
)
"""Failures that stop a room plan without ending the turn.

An unreachable capability query or design provider, an answer that does not
satisfy its schema, and a plan naming a product type the vocabulary does not
contain. Every one means no authoritative plan exists *now* - which is a fact
about us, never about what the retailer stocks.
"""


def _design_brief(message: str) -> str | None:
    """The customer's own words about this room, or nothing.

    Their current message, trimmed, and only when it fits. Not truncated: a
    brief cut mid-phrase can invert what it said, and "no TV unit" clipped to
    "no TV" is worse than silence. Not summarised and not concatenated with
    history either - both would need a second model, and the room type, budget,
    measurements and preferences already travel as their own structured fields.
    """
    trimmed = message.strip()
    if not trimmed or len(trimmed) > MAX_DESIGN_BRIEF_CHARS:
        return None
    return trimmed


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
    UnsupportedRequirement: (SearchRequirementClarificationReason.UNSUPPORTED_REQUIREMENT),
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
    bundle_outcome: BundleOptimizationOutcome | None = None
    bundle_change: BundleInteractionOp | None = None
    """The room edit this turn made, for the deterministic acknowledgement.

    Set only when lines actually changed: an operation that asked for a state a
    piece was already in is a legal no-op, and saying "that will stay in the
    room" about something nobody changed would be reporting work that did not
    happen."""

    proposals_applied: bool = False
    """Whether this branch already folded the turn's customer facts in.

    Only the whole-room branch does. It has to: a room is planned around the
    budget and the room type stated in the very message that asked for it, and
    applying them afterwards would plan the room without them. Every other
    branch leaves them to `run`, where applying them after execution is what
    stops a successful search being undone.
    """


_BUNDLE_REFERENCE_FAILURES: dict[BundleReferenceFailureReason, TurnFailureCode] = {
    BundleReferenceFailureReason.TARGET_PRODUCT_UNAVAILABLE: (TurnFailureCode.PRODUCT_UNAVAILABLE),
    BundleReferenceFailureReason.BUNDLE_NOT_VERIFIABLE: (TurnFailureCode.BUNDLE_NOT_VERIFIABLE),
}
"""Which unresolved references are facts rather than questions.

The same line M11 already draws for products a search presented: "which one did
you mean?" is answerable and "that one is gone" is not. A customer cannot
resolve a piece the catalog stopped returning, nor a room we could not finish
reading, so neither becomes a question.
"""


def _unresolved_bundle(reason: BundleReferenceFailureReason, state: AgentStateV1) -> _Primary:
    """What a failed bundle reference produces: a question, or a fact.

    Either way the room is untouched - a reference that did not resolve cannot
    have been acted on.
    """
    code = _BUNDLE_REFERENCE_FAILURES.get(reason)
    if code is not None:
        return _Primary(state=state, failure=TurnFailure(code=code))
    return _Primary(
        state=state,
        clarification=DeterministicClarification(
            reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
            bundle_reason=reason,
        ),
    )


@dataclass(frozen=True, slots=True)
class _Reoptimised:
    """A room chosen again, and the state it was committed into."""

    state: AgentStateV1
    outcome: BundleOptimizationOutcome


_UNUSABLE_PRICE = PriceConstraint(currency="\x00", max_amount=Decimal(1))
"""Sentinel for "this product has no price a bound can be built from".

A distinct object rather than None, because None already means "no bound
wanted" - which is what an alternative asks for, and is a different thing from
a bound that could not be derived.
"""


def _stale_reference(state: AgentStateV1, resolved_revision: int) -> _Primary | None:
    """Whether the room moved between resolving a reference and acting on it."""
    room = state.room_project
    if room is not None and room.bundle_revision == resolved_revision:
        return None
    return _Primary(
        state=state,
        clarification=DeterministicClarification(
            reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
            bundle_reason=BundleReferenceFailureReason.STALE_BUNDLE_REFERENCE,
        ),
    )


def _contradicted(
    excluded: tuple[ExcludedDesignRole, ...],
    targets: list[ProductCandidate],
    turn: CustomerTurnInput,
) -> bool:
    """Whether the customer's hard instructions cannot all hold.

    Two ways they cannot. A piece they asked to keep - or to design around -
    may be exactly what an exclusion forbids; or its commerce role may be
    unknown, leaving the question unanswerable. Neither is resolved by
    choosing: honouring either instruction would discard the other silently,
    so the customer settles it (M12E-4D 16, M12F 2).
    """
    conflict = conflicting_role(excluded, targets)
    if conflict is not None:
        logger.info(
            "design_revision_conflict",
            store_id=turn.context.store_id,
            commerce_category=conflict.commerce_category,
        )
        return True
    if unprovable_against_exclusions(excluded, targets):
        logger.info("design_revision_role_unverifiable", store_id=turn.context.store_id)
        return True
    return False


def _revision_context(
    room: RoomProjectState | None, excluded: tuple[ExcludedDesignRole, ...]
) -> DesignRevisionContext | None:
    """The plan being revised, or None when there is nothing to revise.

    An empty durable plan is treated exactly as no plan at all. A committed
    empty plan is reachable - a specialist plan whose every role this retailer
    turned out not to stock commits as a complete room with no lines - and it
    leaves nothing to preserve, so replanning it from scratch is what a
    revision of it would do anyway (M12E-4D 8).
    """
    current = project_current_plan(room)
    if not current:
        return None
    return DesignRevisionContext(current_needs=current, excluded=excluded)


def _single_need(target: ResolvedBundleReference) -> int | None:
    """The one role this card fills, or None if that is not a single answer.

    A card filling two roles, or none, cannot be replaced without deciding
    which role the customer meant - and product identity cannot decide it.
    """
    return target.need_ids[0] if len(target.need_ids) == 1 else None


def _replacement_price(
    mode: BundleReplacementMode, product: ProductCandidate
) -> PriceConstraint | None:
    """A bound derived from the product being replaced, never from a model.

    Both bounds are **exclusive**: something cheaper than this one costs less
    than it, not the same. No percentage is invented and no epsilon is
    subtracted - the figure is the catalog's own.

    `MORE_EXPENSIVE` means exactly higher-priced. It is never a claim about
    quality, which is why no "premium" mode exists to derive it from.
    """
    if mode not in (BundleReplacementMode.CHEAPER, BundleReplacementMode.MORE_EXPENSIVE):
        return None
    if product.price_amount <= 0 or not product.price_unit.strip():
        return _UNUSABLE_PRICE
    if mode is BundleReplacementMode.CHEAPER:
        return PriceConstraint.below(product.price_amount, product.price_unit)
    return PriceConstraint.above(product.price_amount, product.price_unit)


def _with_rejection(room: RoomProjectState, need_id: int, product_id: int) -> tuple[int, ...]:
    """This role's rejections plus the product being replaced now.

    Insertion order, deduplicated, and bounded by the ceiling a search already
    applies - one answer to "how many", not two. Staged only: it becomes true
    of the plan when a replacement is actually found.
    """
    current = next(
        (need.rejected_product_ids for need in room.design_needs if need.need_id == need_id),
        (),
    )
    merged = tuple(dict.fromkeys((*current, product_id)))
    return merged[-MAX_EXCLUDED_PRODUCT_IDS:]


def _staged_refinements(
    override: DesignNeedSearchOverride | None,
) -> tuple[DesignNeedRefinement, ...]:
    """The role changes that go with a successful refinement, and only those.

    A price bound is not among them: it described one search, not the room.
    """
    if override is None:
        return ()
    wording = override.semantic_intent
    return (
        DesignNeedRefinement(
            need_id=override.need_id,
            rejected_product_ids=override.exclude_product_ids,
            semantic_intent=(
                wording.value
                if wording is not None and wording.op is SemanticIntentOp.SET
                else None
            ),
            clear_semantic_intent=(wording is not None and wording.op is SemanticIntentOp.CLEAR),
        ),
    )


def _plan_from(room: RoomProjectState) -> InteriorDesignResult:
    """The customer's existing plan, in the shape discovery already consumes.

    No specialist is consulted: substituting a product does not change what the
    room needs, and rebuilding the plan through a model would risk a different
    room coming back.
    """
    return InteriorDesignResult(
        needs=tuple(
            DesignCategoryNeed(
                commerce_category=need.commerce_category,
                commerce_subcategory=need.commerce_subcategory,
                priority=need.priority,
                quantity=need.quantity,
                seating_capacity=need.seating_capacity,
                semantic_intent=need.semantic_intent,
            )
            for need in room.design_needs
        )
    )


def _replaced(
    outcome: BundleOptimizationOutcome,
    need_id: int,
    rejected_product_id: int,
    state: AgentStateV1,
) -> bool:
    """Whether the customer actually got another product for that role.

    A complete room is not enough. If the role ended up unmet, or an unrelated
    lock happened to cover it, they asked for another sofa and did not get one
    - and reporting that as success would be describing a room they did not ask
    for.
    """
    if not isinstance(outcome, RoomBundle) or outcome.status is BundleStatus.INFEASIBLE:
        return False
    room = state.room_project
    if room is None:
        return False
    return any(
        line.need_id == need_id
        and line.status is BundleItemStatus.SUGGESTED
        and line.product_id != rejected_product_id
        for line in room.bundle_items
    )


def _no_replacement_reason(outcome: BundleOptimizationOutcome) -> TurnFailureCode:
    """Why no replacement happened: nothing to offer, or nothing that fits.

    Kept apart because they are different things to tell a customer, and a
    budget that would not stretch must never be reported as a catalog with
    nothing in it.
    """
    if isinstance(outcome, RoomBundle) and any(
        entry.reason is not UnmetReason.NO_CANDIDATES for entry in outcome.unmet
    ):
        return TurnFailureCode.REPLACEMENT_NOT_FEASIBLE
    return TurnFailureCode.NO_REPLACEMENT_CANDIDATE


@dataclass(frozen=True, slots=True)
class _Anchored:
    """A resolved design anchor, or the reason to stop.

    Carries the operations that *would* record it rather than a state that
    already has: the anchor's verified role must be checked against the
    revision's exclusions first, and a lock written before that check could not
    be taken back (M12F 2).
    """

    product: ProductCandidate | None = None
    operations: tuple[BundleOperation, ...] = ()
    stop: _Primary | None = None


@dataclass(frozen=True, slots=True)
class _Revision:
    """The hard constraints on recomposing a room, resolved but not yet applied.

    **This step mutates nothing.** It resolves every reference and reports what
    it found; the locks are applied later, once the anchor has been resolved
    too and the whole constraint set is known to be consistent. One bad
    reference among several must never leave half the customer's pieces
    preserved, and an anchor that contradicts an exclusion must not find a
    preserve lock already written (M12E-4D 17, M12F 2).
    """

    excluded: tuple[ExcludedDesignRole, ...] = ()
    preserved: tuple[ProductCandidate, ...] = ()
    operations: tuple[BundleOperation, ...] = ()
    stop: _Primary | None = None


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
        capabilities: CatalogCapabilityService,
        design: InteriorDesignAgent | None,
        design_discovery: DesignDiscoveryService,
        bundle_references: BundleReferenceResolver,
        optimizer: BundleOptimizer,
        dimensions: DimensionSemantics,
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
        self._capabilities = capabilities
        self._design = design
        """The specialist, or None where the capability is not configured.

        Optional because a retailer that has not set up room planning still
        sells furniture: every other action works, and only a request for a
        whole room finds it missing (M12B)."""
        self._design_discovery = design_discovery
        self._bundle_references = bundle_references
        self._optimizer = optimizer
        self._dimensions = dimensions

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

        proposals = map_proposals(decision.state_proposal, decision.commerce_proposal)
        interaction = await self._apply_interaction(decision, pre_turn, turn.context)
        primary = await self._execute(decision, interaction.state, pre_turn, turn, proposals.update)

        # Applied to what the primary action produced, not to the pre-turn
        # state: rebuilding from the start here would silently undo a
        # successful search or a resolved selection. A branch that already
        # applied them says so, because applying an add twice would duplicate
        # every preference in it.
        final_state = (
            primary.state
            if primary.proposals_applied
            else apply_update(primary.state, proposals.update)
        )

        grounding = self._ground(decision, primary, interaction, proposals.clarification)
        self._log(decision, pre_turn, final_state, primary, interaction, started)
        return CustomerTurnResult(
            state=final_state,
            decision=decision,
            grounding=grounding,
            bundle_outcome=primary.bundle_outcome,
            bundle_change=primary.bundle_change,
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

        outcome = await self._references.resolve(decision.interaction.reference, pre_turn, context)
        if isinstance(outcome, ReferenceUnresolved):
            clarification, failure = _reference_outcome(
                outcome.reason, BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE
            )
            # Value first: the interaction is optional, so a primary action
            # that does not depend on it still runs (locked M11A).
            return _Interaction(state=pre_turn, clarification=clarification, failure=failure)

        update = _interaction_update(decision.interaction.op, outcome.product_id, pre_turn)
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
        proposals: AgentStateUpdate,
    ) -> _Primary:
        match decision.action:
            case AgentAction.ANSWER | AgentAction.CLARIFY:
                return _Primary(state=working)
            case AgentAction.BUNDLE_REFINE:
                return await self._bundle_refine(decision, working, pre_turn, turn)
            case AgentAction.DESIGN_HANDOFF:
                return await self._design_handoff(decision, working, pre_turn, turn, proposals)
            case AgentAction.SEARCH:
                if decision.reference is None:
                    return await self._new_search(decision, working, turn)
                return await self._similar_search_turn(decision.reference, working, pre_turn, turn)
            case AgentAction.REFINE_SEARCH:
                return await self._refine(decision, working, pre_turn, turn)
            case AgentAction.PRODUCT_DETAIL:
                return await self._product_detail(decision, working, pre_turn, turn)
            case AgentAction.COMPARE:
                return await self._compare(decision, working, pre_turn, turn)

    # ── refining the room ───────────────────────────────────────────────────

    async def _bundle_refine(
        self,
        decision: CustomerAgentDecision,
        working: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """Change one piece of the room, or one role in its plan.

        The order matters twice, whatever the operation. The room's products
        are re-read **before** anything is resolved, because a reference must
        be settled against what the catalog says now rather than what state
        remembers. And the reference is resolved against the **pre-turn** room,
        then checked against the working one before any edit lands, so a
        reference settled against one room cannot be applied to another.

        What differs afterwards is how far the change reaches. Keeping a piece
        changes nothing about what the room contains or costs, so nothing is
        searched. Replacing one changes both, so the whole room is discovered
        and optimised again: a cheaper sofa may afford a better rug, and a
        dearer one may push an optional piece out.
        """
        intent = decision.bundle_interaction
        assert intent is not None, "the contract requires one"

        room = pre_turn.room_project
        verified = await self._verify_bundle_products(room, turn.context)
        if verified is None:
            return _Primary(
                state=working,
                failure=TurnFailure(code=TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE),
            )

        if intent.op is BundleInteractionOp.REMOVE_NEED:
            return await self._remove_need(intent, working, room, verified, turn)

        assert intent.selector is not None, "every other operation names a card"
        outcome = self._bundle_references.resolve(intent.selector, room, verified)
        if isinstance(outcome, BundleReferenceUnresolved):
            return _unresolved_bundle(outcome.reason, working)

        stale = _stale_reference(working, outcome.bundle_revision)
        if stale is not None:
            return stale

        match intent.op:
            case BundleInteractionOp.LOCK | BundleInteractionOp.UNLOCK:
                return self._set_status(intent.op, outcome, working, turn)
            case BundleInteractionOp.SET_ACQUISITION:
                return await self._set_acquisition(intent, outcome, working, turn)
            case BundleInteractionOp.REPLACE_PRODUCT:
                return await self._replace_product(intent, outcome, working, verified, turn)

    # ── keeping and releasing ───────────────────────────────────────────────

    def _set_status(
        self,
        op: BundleInteractionOp,
        target: ResolvedBundleReference,
        working: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """Local by construction: nothing about the room's contents changed.

        "You can change it later" is permission, not an instruction, so no
        search runs and no product moves.
        """
        status = (
            BundleItemStatus.LOCKED
            if op is BundleInteractionOp.LOCK
            else BundleItemStatus.SUGGESTED
        )
        updated = apply_update(
            working,
            AgentStateUpdate(
                room_project=RoomProjectUpdate(
                    bundle_operations=tuple(
                        SetBundleLineStatus(line_id=line_id, status=status)
                        for line_id in target.line_ids
                    )
                )
            ),
        )
        self._log_refinement(turn, op, working, updated)
        return _Primary(state=updated, bundle_change=op)

    # ── who is paying ───────────────────────────────────────────────────────

    async def _set_acquisition(
        self,
        intent: BundleInteractionIntent,
        target: ResolvedBundleReference,
        working: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """Record what the customer said about owning it, then re-cost the room.

        The statement is theirs and is applied first, unconditionally: someone
        who says they already own a piece has told us something true whatever
        happens to the room afterwards, and discarding it because a later search
        failed would be answering a fact with an unrelated error.

        With a budget, what they own changes what the room costs, so the whole
        room is optimised again. Without one there is nothing for it to change,
        and re-running the pipeline because a flag moved would replace products
        nobody asked about.

        **A piece they already own is locked by saying so.** Owning it is not a
        preference about which product to choose - it is already in the room,
        and nothing the optimiser could pick would replace it. Without the lock
        it would not reach the optimiser as one (`_verify_locks` reads status),
        and a budgeted re-costing would quietly choose a replacement for a piece
        the customer told us they have. `BundleLine` states the same rule from
        the other side: an already-owned line is never a new selection.

        Going back to buying it does not release the lock. "I do need to buy
        that after all" corrects who pays, not whether the piece stays; letting
        it stay is a separate instruction they can give (CLAUDE.md 10).
        """
        assert intent.acquisition is not None, "the contract requires one"
        owned = intent.acquisition is BundleAcquisition.ALREADY_OWNED
        operations: tuple[BundleOperation, ...] = tuple(
            SetBundleLineAcquisition(line_id=line_id, acquisition=intent.acquisition)
            for line_id in target.line_ids
        )
        if owned:
            operations += tuple(
                SetBundleLineStatus(line_id=line_id, status=BundleItemStatus.LOCKED)
                for line_id in target.line_ids
            )
        updated = apply_update(
            working,
            AgentStateUpdate(room_project=RoomProjectUpdate(bundle_operations=operations)),
        )
        self._log_refinement(turn, intent.op, working, updated)

        room = updated.room_project
        if room is None or room.budget is None:
            return _Primary(state=updated, bundle_change=intent.op)

        refreshed = await self._reoptimise(updated, turn)
        if isinstance(refreshed, _Primary):
            # The fact stands; only the refresh failed, and saying the change
            # did not happen would be false.
            return replace(refreshed, state=updated, bundle_change=intent.op)
        return _Primary(
            state=refreshed.state,
            bundle_outcome=refreshed.outcome,
            bundle_change=intent.op,
        )

    # ── a different product for the same role ───────────────────────────────

    async def _replace_product(
        self,
        intent: BundleInteractionIntent,
        target: ResolvedBundleReference,
        working: AgentStateV1,
        verified: list[ProductCandidate],
        turn: CustomerTurnInput,
    ) -> _Primary:
        """Find another product for this role, or change nothing at all.

        Everything is staged. The rejection, the new wording and the room they
        produce commit together or not at all, because a rejection is only true
        of a room chosen while excluding it - persisting one beside the old
        sofa would make state claim that sofa was picked for a reason it never
        satisfied.

        The target's own lock is released for this replacement, since asking
        for another one is permission to change that piece. Every other lock
        stays hard.
        """
        assert intent.replacement is not None, "the contract requires one"
        need_id = _single_need(target)
        if need_id is None:
            return _Primary(
                state=working,
                clarification=DeterministicClarification(
                    reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
                    need_reason=DesignNeedFailureReason.CARD_SPANS_SEVERAL_NEEDS,
                ),
            )

        product = next((p for p in verified if p.product_id == target.product_id), None)
        assert product is not None, "resolution already required it"
        price = _replacement_price(intent.replacement.mode, product)
        if price is _UNUSABLE_PRICE:
            # "Cheaper than this" cannot be expressed against a price commerce
            # cannot act on, and guessing a bound would answer a different
            # question.
            return _Primary(
                state=working,
                failure=TurnFailure(code=TurnFailureCode.PRODUCT_UNAVAILABLE),
            )

        room = working.room_project
        assert room is not None, "resolution already required a room"
        rejected = _with_rejection(room, need_id, target.product_id)
        override = DesignNeedSearchOverride(
            need_id=need_id,
            exclude_product_ids=rejected,
            price=price,
            semantic_intent=intent.replacement.semantic_intent,
        )

        refreshed = await self._reoptimise(
            working, turn, override=override, released_need_id=need_id
        )
        if isinstance(refreshed, _Primary):
            return refreshed
        if not _replaced(refreshed.outcome, need_id, target.product_id, refreshed.state):
            return _Primary(
                state=working,
                failure=TurnFailure(code=_no_replacement_reason(refreshed.outcome)),
            )
        self._log_refinement(turn, intent.op, working, refreshed.state)
        return _Primary(
            state=refreshed.state,
            bundle_outcome=refreshed.outcome,
            bundle_change=intent.op,
        )

    # ── taking a role out of the plan ───────────────────────────────────────

    async def _remove_need(
        self,
        intent: BundleInteractionIntent,
        working: AgentStateV1,
        room: RoomProjectState | None,
        verified: list[ProductCandidate],
        turn: CustomerTurnInput,
    ) -> _Primary:
        """Drop a furnishing role, and whatever was filling it.

        An explicit removal supersedes an earlier instruction to keep the piece
        in that role: they asked for the sofa to stay, and now they have asked
        for no sofa at all.

        The removal is independently valid, so it survives a later refresh
        failure. Resurrecting the role because a search could not run would put
        back something they took out.
        """
        need_id = self._removal_target(intent, working, room, verified)
        if isinstance(need_id, _Primary):
            return need_id

        staged = apply_update(
            working,
            AgentStateUpdate(
                room_project=RoomProjectUpdate(
                    bundle_operations=(RefineBundle(removed_need_ids=(need_id,)),)
                )
            ),
        )
        self._log_refinement(turn, intent.op, working, staged)

        refreshed = await self._reoptimise(staged, turn)
        if isinstance(refreshed, _Primary):
            return replace(refreshed, state=staged, bundle_change=intent.op)
        return _Primary(
            state=refreshed.state,
            bundle_outcome=refreshed.outcome,
            bundle_change=intent.op,
        )

    def _removal_target(
        self,
        intent: BundleInteractionIntent,
        working: AgentStateV1,
        room: RoomProjectState | None,
        verified: list[ProductCandidate],
    ) -> int | _Primary:
        """Which role to remove, named by a card or directly by its kind.

        A `_Primary` means stop, and it carries the working state: a question
        about which role they meant must not cost them the room they have.
        """
        if intent.need_selector is not None:
            outcome = self._bundle_references.resolve_need(intent.need_selector, room)
            if isinstance(outcome, DesignNeedUnresolved):
                return _Primary(
                    state=working,
                    clarification=DeterministicClarification(
                        reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
                        need_reason=outcome.reason,
                    ),
                )
            return outcome.need_id

        assert intent.selector is not None, "the contract requires one of the two"
        card = self._bundle_references.resolve(intent.selector, room, verified)
        if isinstance(card, BundleReferenceUnresolved):
            return _unresolved_bundle(card.reason, working)
        need_id = _single_need(card)
        if need_id is None:
            return _Primary(
                state=working,
                clarification=DeterministicClarification(
                    reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
                    need_reason=DesignNeedFailureReason.CARD_SPANS_SEVERAL_NEEDS,
                ),
            )
        return need_id

    # ── the whole room, chosen again ────────────────────────────────────────

    async def _reoptimise(
        self,
        state: AgentStateV1,
        turn: CustomerTurnInput,
        *,
        override: DesignNeedSearchOverride | None = None,
        released_need_id: int | None = None,
    ) -> _Reoptimised | _Primary:
        """Discover every current role afresh and choose the whole room again.

        **Every** role, not just the one that changed. Candidate pools are not
        persisted, and one product moving changes what the rest can cost: a
        cheaper sofa may afford a better rug, a dearer one may push an optional
        piece out. Optimising the changed need alone would produce a room that
        no longer adds up.

        The plan is the customer's existing one - no specialist is consulted,
        because substituting a product does not change what the room needs.

        Returns a `_Primary` when the turn should stop there: an unreachable
        catalog or an unverifiable lock is reported, not worked around.
        """
        room = state.room_project
        if room is None or not room.design_needs:
            return _Primary(
                state=state, failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE)
            )

        locks = await self._verify_locks(state, turn.context, released_need_id)
        if locks is None:
            return _Primary(
                state=state,
                failure=TurnFailure(code=TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE),
            )

        try:
            capabilities = await self._capabilities.capabilities(turn.context)
            plan = _plan_from(room)
            request = InteriorDesignRequest(
                task=DesignTask.ROOM_PLAN,
                room_type=room.room_type,
                geometry=room.geometry,
                budget=room.budget,
                design_preferences=room.design_preferences,
                catalog_capabilities=capabilities,
            )
            overrides = (
                {}
                if override is None
                else {
                    position: override
                    for position, need in enumerate(room.design_needs)
                    if need.need_id == override.need_id
                }
            )
            discovery = await self._design_discovery.discover(
                request, plan, turn.context, overrides=overrides
            )
        except _HANDLED_DESIGN_FAILURES:
            logger.warning("room_refresh_unavailable", store_id=turn.context.store_id)
            return _Primary(
                state=state, failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE)
            )
        except _HANDLED_SEARCH_FAILURES:
            logger.warning("room_refresh_unavailable", store_id=turn.context.store_id)
            return _Primary(
                state=state, failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE)
            )

        outcome = self._optimizer.optimize(
            BundleOptimizationRequest(
                discovery=discovery,
                budget=room.budget,
                locked=tuple(product for _, product in locks),
            )
        )
        return _Reoptimised(
            state=self._commit_refinement(state, outcome, override),
            outcome=outcome,
        )

    def _commit_refinement(
        self,
        state: AgentStateV1,
        outcome: BundleOptimizationOutcome,
        override: DesignNeedSearchOverride | None,
    ) -> AgentStateV1:
        """The refined room and the role changes that produced it, in one step.

        Nothing commits unless a real room did: an infeasible package or a
        refusal leaves the previous room, its rejections and its wording
        exactly as they were.

        The plan keeps its identity - the same roles, in the same order, under
        the same allocator - because refining a room is not replanning one.
        """
        if not isinstance(outcome, RoomBundle):
            return state
        if outcome.status is BundleStatus.INFEASIBLE:
            return state

        room = state.room_project
        assert room is not None, "a refinement has a room"
        by_position = {position: need.need_id for position, need in enumerate(room.design_needs)}
        return apply_update(
            state,
            AgentStateUpdate(
                room_project=RoomProjectUpdate(
                    bundle_operations=(
                        RefineBundle(
                            refinements=_staged_refinements(override),
                            preserved=tuple(
                                PreservedBundleLine(line_id=line.line_id)
                                for line in room.bundle_items
                                if line.status is BundleItemStatus.LOCKED
                                and (override is None or line.need_id != override.need_id)
                            ),
                            added=tuple(
                                BundleLineSpec(
                                    product_id=line.product.product_id,
                                    quantity=line.quantity,
                                    acquisition=BundleAcquisition.TO_BUY,
                                    status=BundleItemStatus.SUGGESTED,
                                    need_id=(
                                        None
                                        if line.need_index is None
                                        else by_position.get(line.need_index)
                                    ),
                                )
                                for line in outcome.lines
                                if not line.locked
                            ),
                        ),
                    )
                )
            ),
        )

    async def _verify_locks(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        released_need_id: int | None = None,
    ) -> list[tuple[BundleItemState, LockedBundleProduct]] | None:
        """Every locked line, with its product re-read, or None if one is gone.

        Ids are deduplicated for the read and expanded again afterwards: two
        lines holding the same product are one row to fetch and two lines to
        preserve, and losing that distinction would lose a quantity.

        `released_need_id` is the one role the customer has just asked to
        change. Its lines do not enter as hard locks - asking for another sofa
        is permission to move that sofa - while every other lock stays
        untouchable.

        A lock that no longer resolves stops the whole room. It cannot be
        dropped, substituted or unlocked, and answering from what we remember
        about it would be asserting a price nobody checked (CLAUDE.md 10).
        """
        room = state.room_project
        lines = [
            line
            for line in (room.bundle_items if room else ())
            if line.status is BundleItemStatus.LOCKED
            and (released_need_id is None or line.need_id != released_need_id)
        ]
        if not lines:
            return []

        wanted = tuple(dict.fromkeys(line.product_id for line in lines))
        products = await self._hydration.hydrate_ids(wanted, context)
        by_id = {product.product_id: product for product in products}
        if len(by_id) != len(wanted):
            logger.warning(
                "whole_room_locked_product_unavailable",
                store_id=context.store_id,
                expected=len(wanted),
                verified=len(by_id),
            )
            return None

        return [
            (
                line,
                LockedBundleProduct(
                    product=by_id[line.product_id],
                    acquisition=line.acquisition,
                    quantity=line.quantity,
                ),
            )
            for line in lines
        ]

    @staticmethod
    def _log_refinement(
        turn: CustomerTurnInput,
        op: BundleInteractionOp,
        before: AgentStateV1,
        after: AgentStateV1,
    ) -> None:
        """Shape only: no product, no line id, no price, no customer text."""
        old, new = before.room_project, after.room_project
        logger.info(
            "bundle_refined",
            store_id=turn.context.store_id,
            op=str(op),
            line_count=len(new.bundle_items) if new else 0,
            need_count=len(new.design_needs) if new else 0,
            changed=(new is not None and old is not None and new.bundle_items != old.bundle_items),
        )

    async def _verify_bundle_products(
        self, room: RoomProjectState | None, context: RetailerContext
    ) -> list[ProductCandidate] | None:
        """Fresh facts for everything currently in the room, or a refusal.

        Ids are deduplicated for the read and the products returned as they
        come: a card's identity is a product, and two lines holding one product
        are one row to fetch.

        A missing product behind a **locked** line stops the turn, exactly as a
        room plan does: the customer asked to keep it, and neither dropping it
        nor answering from what we remember about it is allowed. A missing
        product behind a suggestion is not fatal here - nothing is being chosen
        - but it cannot back a reference either, because a card that cannot be
        described is not one the customer can have named.
        """
        if room is None or not room.bundle_items:
            return []

        wanted = tuple(dict.fromkeys(line.product_id for line in room.bundle_items))
        products = await self._hydration.hydrate_ids(wanted, context)
        found = {product.product_id for product in products}
        missing_locked = [
            line.product_id
            for line in room.bundle_items
            if line.status is BundleItemStatus.LOCKED and line.product_id not in found
        ]
        if missing_locked:
            logger.warning(
                "bundle_locked_product_unavailable",
                store_id=context.store_id,
                missing_count=len(set(missing_locked)),
            )
            return None
        return list(products)

    # ── the whole room ──────────────────────────────────────────────────────

    async def _design_handoff(
        self,
        decision: CustomerAgentDecision,
        working: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
        proposals: AgentStateUpdate,
    ) -> _Primary:
        """Plan the room, find products for it, and choose one combination.

        The order is the point. Customer facts land first and unconditionally,
        so a provider failure three steps later cannot take the budget they just
        stated with it. Then the piece they asked to keep is verified and
        recorded, because that too is something they said rather than something
        we computed. Only after both is anything executed, and only a real
        bundle is committed.
        """
        started = time.perf_counter()
        state = apply_update(working, proposals)

        if decision.design_scope is DesignScope.COMPLEMENT:
            return await self._complement(decision, state, pre_turn, turn)

        if self._design is None:
            # Not configured here. Checked before anything is read or written,
            # so an unconfigured deployment leaves the customer's existing room
            # exactly as it was rather than half-rebuilt.
            logger.info("whole_room_not_configured", store_id=turn.context.store_id)
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                failure=TurnFailure(code=TurnFailureCode.DESIGN_UNAVAILABLE),
            )

        # Everything the customer ruled in or out is resolved and verified
        # before a single lock is written, so a contradiction found late cannot
        # leave an earlier instruction half-applied (M12F 2).
        revision = await self._revision_constraints(decision, state, pre_turn, turn)
        if revision.stop is not None:
            return replace(revision.stop, state=state, proposals_applied=True)

        anchored = await self._resolve_anchor(decision, state, pre_turn, turn)
        if anchored.stop is not None:
            return replace(anchored.stop, state=state, proposals_applied=True)

        targets = [
            *revision.preserved,
            *([anchored.product] if anchored.product is not None else []),
        ]
        if _contradicted(revision.excluded, targets, turn):
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                clarification=DeterministicClarification(
                    reason=BlockingClarificationReason.CONTRADICTORY_ROOM_INSTRUCTIONS,
                    need_reason=DesignNeedFailureReason.PRESERVED_ROLE_EXCLUDED,
                ),
            )

        operations = (*revision.operations, *anchored.operations)
        if operations:
            # One transition for every piece they named, so the room is never
            # observed half-preserved and the revision advances exactly once.
            state = apply_update(
                state,
                AgentStateUpdate(room_project=RoomProjectUpdate(bundle_operations=operations)),
            )

        locks = await self._verify_locks(state, turn.context)
        if locks is None:
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                failure=TurnFailure(code=TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE),
            )

        try:
            capabilities = await self._capabilities.capabilities(turn.context)
            plan = await self._design.plan(
                self._design_request(state, turn, capabilities, locks, revision.excluded)
            )
        except _HANDLED_DESIGN_FAILURES:
            logger.warning("whole_room_design_unavailable", store_id=turn.context.store_id)
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                failure=TurnFailure(code=TurnFailureCode.DESIGN_UNAVAILABLE),
            )

        try:
            discovery = await self._design_discovery.discover(
                self._design_request(state, turn, capabilities, locks, revision.excluded),
                plan,
                turn.context,
            )
        except _HANDLED_SEARCH_FAILURES:
            # A catalog or index that could not be reached. Never reported as
            # the retailer having nothing suitable: that is a fact about the
            # catalog, and we did not establish it.
            logger.warning("whole_room_discovery_unavailable", store_id=turn.context.store_id)
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE),
            )

        outcome = self._optimizer.optimize(
            BundleOptimizationRequest(
                discovery=discovery,
                budget=state.room_project.budget if state.room_project else None,
                locked=tuple(product for _, product in locks),
            )
        )
        committed = self._commit(state, plan, outcome)
        self._log_whole_room(turn, state, plan, discovery, outcome, committed, started)
        return _Primary(
            state=committed,
            design_handoff=True,
            proposals_applied=True,
            bundle_outcome=outcome,
        )

    async def _complement(
        self,
        decision: CustomerAgentDecision,
        state: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """The next piece that would most complete the space around one they chose.

        Products, not a room. The specialist names a single furnishing role and
        the ordinary search pipeline finds real ones for it, so the customer
        sees cards exactly as they would from any search - no plan, no total,
        no package they did not ask for (M14 21, 22).

        What goes with what is design reasoning, which is why the specialist
        answers it. Nothing in this service records which products are bought
        together, so any other source for that answer would be invented.
        """
        if self._design is None:
            logger.info("complement_not_configured", store_id=turn.context.store_id)
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                failure=TurnFailure(code=TurnFailureCode.DESIGN_UNAVAILABLE),
            )

        anchored = await self._resolve_anchor(decision, state, pre_turn, turn)
        if anchored.stop is not None:
            return replace(anchored.stop, state=state, proposals_applied=True)

        product = anchored.product or await self._settled_product(pre_turn, turn)
        if product is None:
            # Nothing to complement. Asking the specialist what goes with
            # nothing in particular is a different question.
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                clarification=DeterministicClarification(
                    reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE
                ),
            )

        try:
            capabilities = await self._capabilities.capabilities(turn.context)
            request = self._complement_request(state, turn, capabilities, product)
            plan = await self._design.plan(request)
        except _HANDLED_DESIGN_FAILURES:
            logger.warning("complement_unavailable", store_id=turn.context.store_id)
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                failure=TurnFailure(code=TurnFailureCode.DESIGN_UNAVAILABLE),
            )

        if not plan.needs:
            # The specialist had nothing to suggest this retailer can supply.
            # Reported as a handoff that produced nothing rather than filled
            # with a category nobody chose.
            logger.info("complement_no_need", store_id=turn.context.store_id)
            return _Primary(state=state, design_handoff=True, proposals_applied=True)

        resolved = self._design_discovery.resolve_need(plan.needs[0], request)
        # Promoted like any other search, so "a cheaper one" next turn refines
        # the rug rather than reaching back past it to the sofa.
        composed = ComposedSearch(
            candidate=ActiveSearchState(
                request=resolved.request,
                semantics=resolved.semantics,
                semantic_preferences=resolved.semantic_preferences,
                semantic_intent=resolved.semantic_text,
                # Unread on this path: `_run_search` promotes the criteria
                # through the reducer, which carries the live revision. The
                # constant a first search uses is the honest placeholder.
                revision=NO_RESULTS_REVISION,
            ),
            resolved=resolved,
        )
        return replace(
            await self._run_search(composed, state, turn.context),
            design_handoff=True,
            proposals_applied=True,
        )

    async def _settled_product(
        self, pre_turn: AgentStateV1, turn: CustomerTurnInput
    ) -> ProductCandidate | None:
        """The one piece they have settled on, when the turn named none.

        "I like the second one" routes here without always carrying a
        reference, because the interesting thing about it is the interest, not
        the pointing. One selected product is an unambiguous answer to "which
        piece"; two is a question, and none is nothing to build on.

        Re-read rather than remembered: an anchor is described to the
        specialist, and describing a product from stale state would assert
        facts nobody checked.
        """
        selected = pre_turn.product_interaction.selected_product_ids
        if len(selected) != 1:
            return None
        products = await self._hydration.hydrate_ids(selected, turn.context)
        return products[0] if products else None

    def _complement_request(
        self,
        state: AgentStateV1,
        turn: CustomerTurnInput,
        capabilities: RetailerCatalogCapabilities,
        product: ProductCandidate,
    ) -> InteriorDesignRequest:
        """What the specialist is told about the piece they settled on.

        The anchor carries design facts only - kind, colour, styles, size - and
        no identity, exactly as a room plan's anchors do. Nothing here says
        which product it is, what it cost, or where it came from.
        """
        room = state.room_project
        return InteriorDesignRequest(
            task=DesignTask.COMPLEMENTARY_RECOMMENDATION,
            design_brief=_design_brief(turn.message),
            room_type=room.room_type if room else None,
            design_preferences=room.design_preferences if room else (),
            regular_seating_count=room.regular_seating_count if room else None,
            catalog_capabilities=capabilities,
            anchors=project_anchors(
                [product],
                dimensions=self._dimensions,
                locked_product_ids=[product.product_id],
                quantities={product.product_id: 1},
            ),
        )

    async def _revision_constraints(
        self,
        decision: CustomerAgentDecision,
        state: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Revision:
        """Everything the customer ruled out, and everything they ruled in.

        Resolution before mutation, without exception. Exclusions are settled
        against the plan being revised, preserved cards against the room they
        were looking at, and both against the **pre-turn** universe - what "the
        second one" meant was fixed before this turn changed anything.

        Only when every reference resolves and no two contradict does anything
        lock, and then all of it locks in one state transition.
        """
        intent = decision.design_revision
        if intent is None:
            return _Revision()

        room = pre_turn.room_project
        excluded = self._bundle_references.resolve_exclusions(intent.removed_needs, room)
        if isinstance(excluded, DesignNeedUnresolved):
            return _Revision(
                stop=_Primary(
                    state=state,
                    design_handoff=True,
                    proposals_applied=True,
                    clarification=DeterministicClarification(
                        reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
                        need_reason=excluded.reason,
                    ),
                ),
            )

        verified = await self._verify_bundle_products(room, turn.context)
        if verified is None:
            return _Revision(
                stop=_Primary(
                    state=state,
                    design_handoff=True,
                    proposals_applied=True,
                    failure=TurnFailure(code=TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE),
                ),
            )

        by_id = {product.product_id: product for product in verified}
        line_ids: list[int] = []
        preserved: list[ProductCandidate] = []
        for selector in intent.preserved_items:
            outcome = self._bundle_references.resolve(selector, room, verified)
            if isinstance(outcome, BundleReferenceUnresolved):
                stop = _unresolved_bundle(outcome.reason, state)
                return _Revision(stop=replace(stop, design_handoff=True, proposals_applied=True))
            line_ids.extend(outcome.line_ids)
            preserved.append(by_id[outcome.product_id])

        return _Revision(
            excluded=excluded,
            preserved=tuple(preserved),
            operations=tuple(
                SetBundleLineStatus(line_id=line_id, status=BundleItemStatus.LOCKED)
                for line_id in dict.fromkeys(line_ids)
            ),
        )

    async def _resolve_anchor(
        self,
        decision: CustomerAgentDecision,
        state: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Anchored:
        """Work out what "design around this one" would record, or stop.

        Resolves and verifies; it writes nothing. The anchor's role still has
        to be checked against the revision's exclusions, and a lock written
        before that check could not be taken back (M12F 2).

        Resolved against the **pre-turn** universe, like every other reference:
        what "the second one" meant was fixed before this turn changed
        anything, and resolving it afterwards could point at a different
        product.

        The product is re-read before anything is recorded, so a line is never
        created from a remembered fact. Once verified, the lock is a customer
        statement and survives whatever the rest of the turn does to it.
        """
        anchor = decision.design_anchor
        if anchor is None:
            return _Anchored()

        outcome = await self._references.resolve(anchor.reference, pre_turn, turn.context)
        if isinstance(outcome, ReferenceUnresolved):
            clarification, failure = _reference_outcome(
                outcome.reason, BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE
            )
            return _Anchored(
                stop=_Primary(
                    state=state,
                    design_handoff=True,
                    clarification=clarification,
                    failure=failure,
                )
            )

        products = await self._hydration.hydrate_ids((outcome.product_id,), turn.context)
        if not products:
            return _Anchored(
                stop=_Primary(
                    state=state,
                    design_handoff=True,
                    failure=TurnFailure(code=TurnFailureCode.PRODUCT_UNAVAILABLE),
                )
            )

        room = state.room_project
        existing = [
            line
            for line in (room.bundle_items if room else ())
            if line.product_id == outcome.product_id
        ]
        if len(existing) > 1:
            # Two lines already carry this product, and a reference to "this
            # sofa" names a product rather than a line. Guessing which physical
            # line they meant is exactly what bundle-card references settle -
            # and those are a different surface: an anchor points at a product
            # that was presented, a preserved item at a card in the room.
            return _Anchored(
                stop=_Primary(
                    state=state,
                    design_handoff=True,
                    clarification=DeterministicClarification(
                        reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE
                    ),
                )
            )

        acquisition = anchor.acquisition or BundleAcquisition.TO_BUY
        if existing:
            line = existing[0]
            operations: tuple[BundleOperation, ...] = (
                SetBundleLineStatus(line_id=line.line_id, status=BundleItemStatus.LOCKED),
                SetBundleLineQuantity(line_id=line.line_id, quantity=anchor.quantity),
                SetBundleLineAcquisition(line_id=line.line_id, acquisition=acquisition),
            )
        else:
            operations = (
                AddBundleLine(
                    line=BundleLineSpec(
                        product_id=outcome.product_id,
                        quantity=anchor.quantity,
                        acquisition=acquisition,
                        status=BundleItemStatus.LOCKED,
                    )
                ),
            )
        return _Anchored(product=products[0], operations=operations)

    def _design_request(
        self,
        state: AgentStateV1,
        turn: CustomerTurnInput,
        capabilities: RetailerCatalogCapabilities,
        locks: list[tuple[BundleItemState, LockedBundleProduct]],
        excluded: tuple[ExcludedDesignRole, ...] = (),
    ) -> InteriorDesignRequest:
        """What the specialist is told: the room, and what is already in it.

        No retailer, no state, no identity, no price. The anchors are built
        from the same freshly verified products the optimiser will use, with
        quantities summed per product so the specialist learns there are two of
        something without learning which records said so.

        A plan already in state makes this a **revision**, whether or not the
        customer named any constraint: "add a reading corner" to an existing
        room is a recomposition, and planning it from nothing would silently
        discard everything they already have. The application decides this from
        durable state; the model authors no marker (M12E-4D 7).

        The current needs come from the durable plan rather than anything this
        turn computed. Room type, budget and preferences may already have moved
        - those are independent customer facts and land unconditionally - but
        the composition being revised is the last valid one (M12E-4D 20).
        """
        room = state.room_project
        quantities: dict[int, int] = {}
        for line, _ in locks:
            quantities[line.product_id] = quantities.get(line.product_id, 0) + line.quantity
        unique = list(dict.fromkeys(product.product.product_id for _, product in locks))
        products = [
            next(p.product for _, p in locks if p.product.product_id == product_id)
            for product_id in unique
        ]
        return InteriorDesignRequest(
            task=DesignTask.ROOM_PLAN,
            design_brief=_design_brief(turn.message),
            room_type=room.room_type if room else None,
            geometry=room.geometry if room else None,
            budget=room.budget if room else None,
            design_preferences=room.design_preferences if room else (),
            regular_seating_count=room.regular_seating_count if room else None,
            catalog_capabilities=capabilities,
            anchors=project_anchors(
                products,
                dimensions=self._dimensions,
                locked_product_ids=[p.product.product_id for _, p in locks],
                quantities=quantities,
            ),
            revision=_revision_context(room, excluded),
        )

    def _commit(
        self,
        state: AgentStateV1,
        plan: InteriorDesignResult,
        outcome: BundleOptimizationOutcome,
    ) -> AgentStateV1:
        """A real room replaces the plan and the bundle; anything else leaves both.

        A partial room is still a proposal worth keeping. An infeasible one and
        a refusal are not: replacing a bundle the customer can buy with one they
        cannot would lose something for nothing - and replacing the plan beside
        it would leave needs describing a room that was never chosen.

        The plan and the bundle land in **one** operation, because they are one
        proposal: there is no ordering in which new needs could be observed
        beside a bundle picked from different ones.

        Locked lines are named by id and preserved; every newly selected product
        is a fresh suggestion carrying the plan position it filled, which the
        reducer maps to the id it has just allocated. The optimiser has no
        state-line provenance, so nothing tries to match its locked output back
        to the lines it came from.
        """
        if not isinstance(outcome, RoomBundle):
            return state
        if outcome.status is BundleStatus.INFEASIBLE:
            return state

        room = state.room_project
        locked_ids = [
            line.line_id
            for line in (room.bundle_items if room else ())
            if line.status is BundleItemStatus.LOCKED
        ]
        return apply_update(
            state,
            AgentStateUpdate(
                room_project=RoomProjectUpdate(
                    bundle_operations=(
                        ReplaceDesignPlan(
                            needs=tuple(
                                DesignNeedSpec(
                                    commerce_category=need.commerce_category,
                                    commerce_subcategory=need.commerce_subcategory,
                                    priority=need.priority,
                                    quantity=need.quantity,
                                    seating_capacity=need.seating_capacity,
                                    semantic_intent=need.semantic_intent,
                                )
                                for need in plan.needs
                            ),
                            preserved=tuple(
                                PreservedBundleLine(line_id=line_id) for line_id in locked_ids
                            ),
                            added=tuple(
                                PlannedBundleLineSpec(
                                    product_id=line.product.product_id,
                                    quantity=line.quantity,
                                    acquisition=BundleAcquisition.TO_BUY,
                                    status=BundleItemStatus.SUGGESTED,
                                    need_index=line.need_index,
                                )
                                for line in outcome.lines
                                if not line.locked
                            ),
                        ),
                    )
                )
            ),
        )

    @staticmethod
    def _log_whole_room(
        turn: CustomerTurnInput,
        state: AgentStateV1,
        plan: InteriorDesignResult,
        discovery: DesignDiscoveryResult,
        outcome: BundleOptimizationOutcome,
        committed: AgentStateV1,
        started: float,
    ) -> None:
        """Shape and counts only: no message, no product, no line id."""
        room = committed.room_project
        bundle = outcome if isinstance(outcome, RoomBundle) else None
        logger.info(
            "whole_room_completed",
            store_id=turn.context.store_id,
            locked_line_count=sum(
                1
                for line in ((state.room_project.bundle_items) if state.room_project else ())
                if line.status is BundleItemStatus.LOCKED
            ),
            need_count=len(plan.needs),
            guidance_count=len(plan.guidance),
            searched_need_count=discovery.searched_count,
            bundle_status=str(bundle.status) if bundle else None,
            unavailable_reason=(None if bundle else str(outcome.reason)),  # type: ignore[union-attr]
            bundle_line_count=len(bundle.lines) if bundle else 0,
            unmet_count=len(bundle.unmet) if bundle else 0,
            committed=committed is not state,
            bundle_revision=room.bundle_revision if room else 0,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )

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
            semantic_intent=(decision.new_search.semantic_intent if decision.new_search else None),
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
        """ "Something like the second one" - a new task seeded from a product.

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
            return await self._seed_and_execute(interpretation, decision, working, turn)
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
                raise LLMResponseInvalidError(reason=f"composition refused: {outcome.defect}")
            case NewTaskRequired():  # pragma: no cover - only from refine_taxonomy
                raise LLMResponseInvalidError(reason="unexpected new-task outcome")

    async def _resolve_relative_price(
        self,
        delta: SearchRefinementDelta,
        pre_turn: AgentStateV1,
        context: RetailerContext,
    ) -> SearchRefinementDelta | _Primary:
        """ "Cheaper than the second one" becomes an ordinary bound.

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
            return _Primary(state=pre_turn, clarification=clarification, failure=failure)
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
        outcome = await self._references.resolve(decision.reference, pre_turn, turn.context)
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
                return _Primary(state=working, clarification=clarification, failure=failure)
            product_ids.append(outcome.product_id)

        comparison = await self._comparison.compare(product_ids, turn.context)
        if isinstance(comparison, ComparisonUnavailable):
            clarification, failure = _comparison_outcome(comparison)
            return _Primary(state=working, clarification=clarification, failure=failure)
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
                primary.clarification or proposal_clarification or interaction.clarification
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
                FollowUpPolicy.NONE if asking or failure is not None else decision.follow_up_policy
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
            search_outcome=(str(primary.search.outcome) if primary.search is not None else None),
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
            return ProductInteractionUpdate(selected_product_ids=AddItems(items=(product_id,)))
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
        DeterministicClarification(reason=clarification_reason, reference_reason=reason),
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
        DeterministicClarification(reason=BlockingClarificationReason.COMPARISON_TARGETS),
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
        return DeterministicClarification(reason=_M7_CLARIFICATION[interpretation.reason])
    return DeterministicClarification(reason=_M7_UNSUPPORTED[type(interpretation)])
