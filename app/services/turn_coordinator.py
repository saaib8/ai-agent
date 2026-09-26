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
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from app.core.exceptions import (
    CatalogUnavailableError,
    IntegrationUnavailableError,
    LLMResponseInvalidError,
    TaxonomyValidationError,
)
from app.core.logging import get_logger
from app.prompts.customer_commerce.v1 import describe_unusable
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarificationReason,
    BundleInteractionIntent,
    BundleInteractionOp,
    BundleReplacementIntent,
    BundleReplacementMode,
    CommercialReason,
    CustomerAgentDecision,
    DesignScope,
    FollowUpPolicy,
    ProductInteractionIntent,
    ProductInteractionOp,
)
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    OfferedCombination,
    OfferedCombinationLine,
    ProductInteractionState,
    RoomProjectState,
    SavedMeasurements,
    SeatingOfferState,
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
from app.schemas.bundle_action import (
    BundleActionRequest,
    BundleAlternativesAction,
    BundleSwapAction,
)
from app.schemas.bundle_reference import BundleItemOrdinal
from app.schemas.comparison import ProductComparisonResult
from app.schemas.composition import (
    ComposedSearch,
    CompositionDefect,
    CompositionFailed,
    CompositionNeedsClarification,
    CompositionOutcome,
    NewTaskRequired,
)
from app.schemas.design import (
    MAX_DESIGN_BRIEF_CHARS,
    MAX_DESIGN_QUESTION_CHARS,
    AnchorProduct,
    DesignCategoryNeed,
    DesignGuidance,
    DesignPriority,
    DesignRevisionContext,
    DesignTask,
    ExcludedDesignRole,
    InteriorDesignRequest,
    InteriorDesignResult,
)
from app.schemas.design_discovery import DesignDiscoveryResult, DesignNeedCandidates
from app.schemas.design_override import DesignNeedSearchOverride
from app.schemas.discovery import (
    MAX_EXCLUDED_PRODUCT_IDS,
    PriceConstraint,
    SeatingCapacityConstraint,
)
from app.schemas.grounding import (
    GroundedProduct,
    SearchExecutionGrounding,
    SelectionGrounding,
    TurnFailure,
    TurnFailureCode,
)
from app.schemas.product import ProductCandidate
from app.schemas.product_reference import PresentedOrdinal, ProductReferenceSelector
from app.schemas.query import (
    ClarificationReason,
    ClarificationRequired,
    ConstraintStrength,
    QueryInterpretation,
    ResolvedSearch,
    SemanticPreference,
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
    CandidatePoolResult,
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
    ResolvedProductReference,
    SearchRequirementClarificationReason,
    SimilarSearchUnavailable,
)
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerContext
from app.schemas.room_opener import RoomQuestion
from app.schemas.screen import PresentedCardView
from app.schemas.search_action import MoreOptionsAction, SearchActionRequest
from app.schemas.seating_solution import (
    SeatingArrangement,
    SeatingRequirements,
    SeatingSolution,
    SeatingSolutionOutcome,
)
from app.services.agent_state import (
    NO_RESULTS_REVISION,
    apply_update,
    commit_search_results,
    remember_measurements,
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
from app.services.proposal_mapping import MappedProposals, map_proposals
from app.services.query_understanding import QueryUnderstandingService
from app.services.reference_resolver import ProductReferenceResolver
from app.services.refinement_composer import SearchRefinementComposer
from app.services.relative_price import RelativePriceResolver
from app.services.room_composition import (
    PRIORITY_FOR_TIER,
    chosen_keys,
    composed_needs,
    default_pieces,
    next_question,
    seating_piece,
)
from app.services.screen_view import cards_from_candidates
from app.services.search_pipeline import ProductSearchPipeline
from app.services.seating_solution import SEATING_CATEGORY, SeatingSolutionPlanner
from app.services.similar_search import SimilarSearchBuilder
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.dimensions import DimensionSemantics
from app.taxonomy.registry import CommerceTaxonomy
from app.taxonomy.rooms import RoomPieces, RoomTemplate
from app.taxonomy.seating import SeatingSemantics
from app.taxonomy.words import customer_words_or_none

logger = get_logger(__name__)

_HANDLED_CATALOG_FAILURES = (CatalogUnavailableError,)
"""A catalog read that failed while gathering *context*, not an answer.

Only the visible-card read uses this. Losing it costs the agent its knowledge
of what is on screen, which makes for a vaguer reply; it does not make a wrong
one. Anything the turn genuinely needs from the catalog is read on a path that
lets the failure propagate, so a database that is really down still ends the
turn there rather than being hidden here (CLAUDE.md 21).
"""

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


def _design_question(message: str) -> str | None:
    """The customer's design question, in their own words.

    Their message, trimmed, and only when it fits. Never truncated: a question
    cut mid-phrase can ask something else entirely, and "what goes with walnut
    in a small room" clipped to "what goes with walnut" would be answered
    wrongly rather than partly.

    It travels as the question rather than as a design brief because the
    contract keeps the two apart: a brief describes a room being planned, and
    carrying both would leave which one was answered ambiguous.
    """
    trimmed = message.strip()
    if not trimmed or len(trimmed) > MAX_DESIGN_QUESTION_CHARS:
        return None
    return trimmed


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
    selection: SelectionGrounding | None = None
    product_detail: GroundedProduct | None = None
    comparison: ProductComparisonResult | None = None
    failure: TurnFailure | None = None
    clarification: DeterministicClarification | None = None
    design_handoff: bool = False
    design_guidance: tuple[DesignGuidance, ...] = ()
    """The specialist's answer to a design question. Words, never products."""

    bundle_outcome: BundleOptimizationOutcome | None = None
    bundle_change: BundleInteractionOp | None = None

    seating_solution: SeatingSolution | None = None
    """A composed seating combination, when a seat count no single piece could
    meet was recovered by pairing pieces (CLAUDE.md 4, 27)."""

    offered_instead_of: str | None = None
    """The seating type they asked for, when it never seats that many and the
    cards are another type that does - shown as the best fit for them."""

    room_question: RoomQuestion | None = None
    """This turn's one question about a room being designed, when something it
    needs is still missing (CLAUDE.md 10.1)."""
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


def _merge_exclusions(current: tuple[int, ...], added: tuple[int, ...]) -> tuple[int, ...]:
    """The excluded ids so far, plus the ones a follow-up just added.

    Deduplicated in insertion order and bounded by the same ceiling a request
    already enforces, so the result is valid by construction - which it must be,
    because `model_copy` writes it onto the request without re-running the field
    validator. When the ceiling is reached the oldest exclusions fall away: the
    products turned down most recently are the ones worth keeping out.
    """
    merged = tuple(dict.fromkeys((*current, *added)))
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


def _forced_into(
    outcome: BundleOptimizationOutcome,
    need_id: int,
    product_id: int,
    state: AgentStateV1,
) -> bool:
    """Whether the chosen product actually ended up filling that role.

    A complete room is not enough: the choice may have been dropped because it
    left the catalog, or the room would not fit around it. Reporting success
    without it in place would describe a room the customer did not get.
    """
    if not isinstance(outcome, RoomBundle) or outcome.status is BundleStatus.INFEASIBLE:
        return False
    room = state.room_project
    if room is None:
        return False
    return any(
        line.need_id == need_id and line.product_id == product_id for line in room.bundle_items
    )


def _bundle_action_decision(action: BundleActionRequest) -> CustomerAgentDecision:
    """The synthesised decision a screen-driven room edit stands in for.

    The customer's clicks already decided the turn, so no model produced this.
    Recorded as a product replacement made at their request, which is what routes
    the finished room to be worded like any other room change. The interaction is
    carried because the contract requires a refinement to name its change; the
    response layer routes on the outcome, not on this stand-in.
    """
    return CustomerAgentDecision(
        action=AgentAction.BUNDLE_REFINE,
        commercial_reason=CommercialReason.CUSTOMER_REQUEST,
        bundle_interaction=BundleInteractionIntent(
            op=BundleInteractionOp.REPLACE_PRODUCT,
            selector=BundleItemOrdinal(ordinal=action.bundle_ordinal),
            replacement=BundleReplacementIntent(mode=BundleReplacementMode.ALTERNATIVE),
        ),
    )


def _search_action_decision() -> CustomerAgentDecision:
    """The synthesised decision a screen-driven search follow-up stands in for.

    The customer's tap already decided the turn, so no model produced this. It
    is a plain search made at their request - which is what routes the fresh
    results to be worded like any other search. A search carries no payload of
    its own here: the query to run is the one already in progress, re-executed
    with a widened exclusion, and that lives in state, not on the decision
    (CLAUDE.md 3.6).
    """
    return CustomerAgentDecision(
        action=AgentAction.SEARCH,
        commercial_reason=CommercialReason.CUSTOMER_REQUEST,
    )


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
        seating_planner: SeatingSolutionPlanner,
        dimensions: DimensionSemantics,
        taxonomy: CommerceTaxonomy,
        rooms: RoomPieces | None = None,
        seating: SeatingSemantics | None = None,
    ) -> None:
        self._taxonomy = taxonomy
        self._rooms = rooms
        """The room registry: which pieces a living room or a bedroom may hold.
        None where rooms are planned without it, as they always were."""
        self._seating = seating
        """Reviewed seat counts, to count a room's real seats - a chair records
        no capacity but seats one by review (CLAUDE.md 7)."""
        self._decisions = decisions
        self._seating_planner = seating_planner
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

        A turn carrying a `bundle_action` is a screen-driven room edit and takes
        the deterministic path instead: the customer's clicks are the decision,
        so no decision model is consulted (CLAUDE.md 3.6). A `search_action` is
        the same idea for a product search - re-run it, excluding what was shown.

        **Model output we cannot use never reaches the customer as an error.**
        A reasoning answer that breaks a rule, or a proposal that cannot be
        read, is our failure to understand, not something the customer did
        wrong, so it ends as a conversational turn that changes nothing (see
        `_not_understood`). Only that one exception is recovered here: an
        outage or an internal invariant violation still propagates, because
        dressing a real defect as "please rephrase" would hide it.
        """
        try:
            return await self._dispatch(turn)
        except LLMResponseInvalidError as exc:
            if not self._may_redecide(turn, exc):
                return self._not_understood(turn, exc)
            first = exc
        # A decision that was valid but could not be *applied* - an amount that
        # is not a number, a colour in the wrong family - gets one corrective
        # decision, told what could not be applied, and the turn runs again
        # from the untouched pre-turn state. Only then the fallback.
        problems = describe_unusable(
            first.context.get("reason"), first.context.get("violations", ())
        )
        logger.warning(
            "customer_turn_redeciding",
            store_id=turn.context.store_id,
            reason=first.context.get("reason"),
        )
        try:
            return await self._run_decided_turn(turn, problems=problems)
        except LLMResponseInvalidError as exc:
            return self._not_understood(turn, exc)

    @staticmethod
    def _may_redecide(turn: CustomerTurnInput, exc: LLMResponseInvalidError) -> bool:
        """Whether a second decision could fix this.

        Not for a screen action: the customer's tap was the decision, and no
        model reads it. Not when the decision step or query understanding
        already spent its own corrective attempt - a turn never costs more
        than three decisions.
        """
        typed = turn.bundle_action is None and turn.search_action is None
        return typed and exc.context.get("stage") not in ("decision", "interpretation")

    async def _dispatch(self, turn: CustomerTurnInput) -> CustomerTurnResult:
        if turn.bundle_action is not None:
            return await self._run_bundle_action(turn, turn.bundle_action)
        if turn.search_action is not None:
            return await self._run_search_action(turn, turn.search_action)
        return await self._run_decided_turn(turn)

    def _not_understood(
        self, turn: CustomerTurnInput, exc: LLMResponseInvalidError
    ) -> CustomerTurnResult:
        """A turn we could not act on, answered as conversation.

        **The pre-turn state, untouched.** Everything the turn did before the
        failure lived only in memory - nothing is persisted until the reply
        exists - so returning the state the customer started from is exactly
        "nothing happened". A half-applied selection, preference or search
        would be a change nobody could explain.

        The decision is a stand-in, like the one a screen-driven action
        carries: an answer that executes nothing, so the reply is routed by the
        failure alone and worded deterministically. No question is offered
        beside it; the reply itself invites them to say it another way.
        """
        logger.warning(
            "customer_turn_not_understood",
            store_id=turn.context.store_id,
            reason=exc.context.get("reason"),
            violations=list(exc.context.get("violations", ())),
            screen_action=(
                "bundle"
                if turn.bundle_action is not None
                else "search"
                if turn.search_action is not None
                else None
            ),
        )
        return CustomerTurnResult(
            state=turn.state,
            decision=CustomerAgentDecision(
                action=AgentAction.ANSWER,
                commercial_reason=CommercialReason.CUSTOMER_REQUEST,
                follow_up_policy=FollowUpPolicy.NONE,
            ),
            grounding=TurnGrounding(
                failure=TurnFailure(code=TurnFailureCode.REQUEST_NOT_UNDERSTOOD),
                follow_up_policy=FollowUpPolicy.NONE,
            ),
        )

    async def _run_decided_turn(
        self, turn: CustomerTurnInput, *, problems: tuple[str, ...] = ()
    ) -> CustomerTurnResult:
        """A typed message: the decision model decides, services execute.

        `problems` makes the decision a corrective one - what could not be
        applied last time this same turn ran.
        """
        started = time.perf_counter()
        pre_turn = turn.state

        decision = await self._decisions.decide(
            DecisionInput(
                message=turn.message,
                conversation=turn.conversation,
                state_view=project_state(pre_turn, await self._visible_cards(turn)),
            ),
            problems=problems,
        )

        proposals = self._with_room_pieces(
            decision, map_proposals(decision.state_proposal, decision.commerce_proposal), pre_turn
        )
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
            selected_kinds=await self._chosen_kinds(final_state, turn),
            decision=decision,
            grounding=grounding,
            selection_added=bool(
                set(final_state.product_interaction.selected_product_ids)
                - set(pre_turn.product_interaction.selected_product_ids)
            ),
            bundle_outcome=primary.bundle_outcome,
            bundle_change=primary.bundle_change,
            seating_solution=primary.seating_solution,
            room_question=primary.room_question,
            room_seats=self._room_seats(primary.bundle_outcome),
            offered_instead_of=primary.offered_instead_of,
        )

    # ── a screen-driven room edit ───────────────────────────────────────────

    async def _run_bundle_action(
        self, turn: CustomerTurnInput, action: BundleActionRequest
    ) -> CustomerTurnResult:
        """A deterministic room edit the customer drove from the screen.

        No decision model and no query understanding: the action names the piece
        (and the chosen option) by ordinal, and both resolve against verified
        state. Showing alternatives grounds a search; a swap grounds a room, and
        each is routed and worded exactly as its ordinary counterpart would be
        (CLAUDE.md 3.6, 27).
        """
        started = time.perf_counter()
        pre_turn = turn.state
        if isinstance(action, BundleAlternativesAction):
            primary = await self._list_alternatives(action, pre_turn, turn)
        else:
            primary = await self._apply_swap(action, pre_turn, turn)

        decision = _bundle_action_decision(action)
        final_state = primary.state
        result = CustomerTurnResult(
            state=final_state,
            selected_kinds=await self._chosen_kinds(final_state, turn),
            decision=decision,
            grounding=self._ground(decision, primary, _Interaction(state=pre_turn), None),
            selection_added=False,
            bundle_outcome=primary.bundle_outcome,
            bundle_change=primary.bundle_change,
            room_seats=self._room_seats(primary.bundle_outcome),
        )
        logger.info(
            "bundle_action_completed",
            store_id=turn.context.store_id,
            action=action.kind,
            applied=primary.bundle_outcome is not None or primary.search is not None,
            failed=primary.failure is not None,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return result

    # ── a screen-driven search follow-up ──────────────────────────────────────

    async def _run_search_action(
        self, turn: CustomerTurnInput, action: SearchActionRequest
    ) -> CustomerTurnResult:
        """A deterministic re-run of the search in progress, excluding what was
        already shown - or the one product the customer turned down.

        No decision model and no query understanding: the query is the one
        already resolved and held in state, re-executed with a widened
        exclusion. It grounds and is worded exactly as any other search, so
        "show me different options" can never be mistaken for a new request or a
        refinement (CLAUDE.md 3.6, 26).
        """
        started = time.perf_counter()
        pre_turn = turn.state
        primary = await self._apply_search_action(action, pre_turn, turn)

        decision = _search_action_decision()
        final_state = primary.state
        result = CustomerTurnResult(
            state=final_state,
            selected_kinds=await self._chosen_kinds(final_state, turn),
            decision=decision,
            grounding=self._ground(decision, primary, _Interaction(state=pre_turn), None),
            selection_added=False,
            bundle_outcome=primary.bundle_outcome,
            bundle_change=primary.bundle_change,
        )
        logger.info(
            "search_action_completed",
            store_id=turn.context.store_id,
            action=action.kind,
            applied=primary.search is not None,
            failed=primary.failure is not None,
            clarified=primary.clarification is not None,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return result

    async def _apply_search_action(
        self,
        action: SearchActionRequest,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """Widen the search in progress by an exclusion, then re-run it.

        There must be a search to re-run: without one there is nothing on screen
        the follow-up could refer to, so it fails rather than inventing a query.
        "Show me different options" excludes the whole set just presented; "not
        this one" resolves a single ordinal against verified state and excludes
        only that product. Either way the widened request replaces the search in
        progress, so the exclusion carries into the next follow-up too, and the
        results are committed like any other search (CLAUDE.md 3.6, 13.5).
        """
        active = pre_turn.active_search
        if active is None:
            return _Primary(
                state=pre_turn,
                failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE),
            )

        if isinstance(action, MoreOptionsAction):
            new_exclusions: tuple[int, ...] = pre_turn.product_interaction.presented_product_ids
        else:
            resolved_ref = await self._references.resolve(
                PresentedOrdinal(position=action.ordinal), pre_turn, turn.context
            )
            if isinstance(resolved_ref, ReferenceUnresolved):
                clarification, failure = _reference_outcome(
                    resolved_ref.reason, BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE
                )
                return _Primary(state=pre_turn, clarification=clarification, failure=failure)
            new_exclusions = (resolved_ref.product_id,)

        return await self._rerun_excluding(active, new_exclusions, pre_turn, turn.context)

    async def _continue_search(
        self,
        decision: CustomerAgentDecision,
        working: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """A typed "show me more" or "not this one", on the buttons' own path.

        The screen actions and these typed requests share `_rerun_excluding`,
        so the two cannot drift apart again: typing "show more options" once
        re-ran nothing, while tapping the button showed new products (issue 7).

        Resolved against the pre-turn state, like every reference. With no
        search in progress there is nothing to show more of, which is a
        question to ask, not a failure to report.
        """
        active = pre_turn.active_search
        if active is None:
            return _Primary(
                state=working,
                clarification=DeterministicClarification(
                    reason=BlockingClarificationReason.NO_SEARCH_TO_REFINE
                ),
            )
        # "I don't like the second one, show me others" sets both: the card
        # they turned down is left out, and with show_more so is the rest of
        # what they have already seen.
        new_exclusions: tuple[int, ...] = (
            pre_turn.product_interaction.presented_product_ids if decision.show_more else ()
        )
        if decision.exclude_reference is not None:
            outcome = await self._references.resolve(
                decision.exclude_reference, pre_turn, turn.context
            )
            if isinstance(outcome, ReferenceUnresolved):
                clarification, failure = _reference_outcome(
                    outcome.reason, BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE
                )
                return _Primary(state=working, clarification=clarification, failure=failure)
            new_exclusions = tuple(dict.fromkeys((*new_exclusions, outcome.product_id)))
        return await self._rerun_excluding(active, new_exclusions, working, turn.context)

    async def _more_combinations(
        self,
        decision: CustomerAgentDecision,
        working: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """ "Show me more" or "not the second option" with combinations on screen.

        The combinations on screen are what they mean, not product cards: the
        ones paged past, or the one turned down, are remembered and never shown
        again, and the search runs as before so the builder offers the next
        best. When none are left, the reply says so instead of repeating.
        """
        offer = working.seating_offer
        active = pre_turn.active_search
        if offer is None or active is None:
            return await self._continue_search(decision, working, pre_turn, turn)
        leaving = list(offer.shown) if decision.show_more else []
        position = decision.combination_dismiss
        if position is not None and 1 <= position <= len(offer.shown):
            leaving.append(offer.shown[position - 1])
        excluded = tuple(dict.fromkeys((*offer.excluded, *leaving)))[-MAX_EXCLUDED_COMBINATIONS:]
        remembered = _with_offer(working, offer.model_copy(update={"excluded": excluded}))
        logger.info(
            "seating_combinations_paged",
            store_id=turn.context.store_id,
            show_more=decision.show_more,
            dismissed=position,
            excluded_count=len(excluded),
        )
        return await self._rerun_excluding(active, (), remembered, turn.context)

    async def _rerun_excluding(
        self,
        active: ActiveSearchState,
        new_exclusions: tuple[int, ...],
        working: AgentStateV1,
        context: RetailerContext,
    ) -> _Primary:
        """The search in progress again, with more products left out.

        The widened request replaces the search in progress, so exclusions
        accumulate across follow-ups and "keep going" keeps moving.
        """
        merged = _merge_exclusions(active.request.exclude_product_ids, new_exclusions)
        new_request = active.request.model_copy(update={"exclude_product_ids": merged})
        composed = ComposedSearch(
            candidate=active.model_copy(update={"request": new_request}),
            resolved=ResolvedSearch(
                request=new_request,
                semantics=active.semantics,
                semantic_preferences=active.semantic_preferences,
                semantic_text=active.semantic_intent,
            ),
        )
        return await self._run_search(composed, working, context)

    async def _list_alternatives(
        self,
        action: BundleAlternativesAction,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """Show the products that could take one room role, for the customer to pick.

        Deterministic all the way: the role is read from the resolved card, its
        search is the same one the room plan would run for that need, and the
        results are presented and committed like any other search - so the
        ordinals the swap will reference are exactly what is on screen. No model
        interprets anything, so "show me other beds" can never be mistaken for a
        room refinement (CLAUDE.md 3.6).
        """
        room = pre_turn.room_project
        if room is None or not room.bundle_items:
            return _unresolved_bundle(BundleReferenceFailureReason.NO_BUNDLE, pre_turn)

        verified = await self._verify_bundle_products(room, turn.context)
        if verified is None:
            return _Primary(
                state=pre_turn,
                failure=TurnFailure(code=TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE),
            )

        card = self._bundle_references.resolve(
            BundleItemOrdinal(ordinal=action.bundle_ordinal), room, verified
        )
        if isinstance(card, BundleReferenceUnresolved):
            return _unresolved_bundle(card.reason, pre_turn)
        need_id = _single_need(card)
        need = (
            next((n for n in room.design_needs if n.need_id == need_id), None)
            if need_id is not None
            else None
        )
        if need is None:
            return _Primary(
                state=pre_turn,
                clarification=DeterministicClarification(
                    reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
                    need_reason=DesignNeedFailureReason.CARD_SPANS_SEVERAL_NEEDS,
                ),
            )

        try:
            capabilities = await self._capabilities.capabilities(turn.context)
        except _HANDLED_DESIGN_FAILURES:
            logger.warning("alternatives_capabilities_unavailable", store_id=turn.context.store_id)
            return _Primary(
                state=pre_turn, failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE)
            )

        resolved = self._design_discovery.resolve_need(
            DesignCategoryNeed(
                commerce_category=need.commerce_category,
                commerce_subcategory=need.commerce_subcategory,
                priority=need.priority,
                quantity=need.quantity,
                seating_capacity=need.seating_capacity,
                semantic_intent=need.semantic_intent,
            ),
            InteriorDesignRequest(
                task=DesignTask.ROOM_PLAN,
                room_type=room.room_type,
                geometry=room.geometry,
                budget=room.budget,
                design_preferences=room.design_preferences,
                catalog_capabilities=capabilities,
            ),
        )
        composed = self._composer.seed_new_task(
            resolved,
            room_preferences=room.design_preferences,
            customer_defaults=pre_turn.customer_preferences.semantic_preferences,
            revision=_current_revision(pre_turn),
        )
        # Alternatives for one role in a room, never a combination: the room's
        # seating was already sized to its head count.
        return await self._run_search(composed, pre_turn, turn.context, recover_seating=False)

    async def _apply_swap(
        self, action: BundleSwapAction, pre_turn: AgentStateV1, turn: CustomerTurnInput
    ) -> _Primary:
        """Put the chosen product into the named role, then choose the room again.

        Everything is named by ordinal and resolved against the pre-turn room and
        the products the customer was just shown - never a product id crossing the
        boundary. The chosen product must be one the role could take, so a
        mismatched pick is refused rather than forced, and the room is then
        re-optimised whole, keeping every other piece (CLAUDE.md 27).
        """
        room = pre_turn.room_project
        if room is None or not room.bundle_items:
            return _unresolved_bundle(BundleReferenceFailureReason.NO_BUNDLE, pre_turn)

        verified = await self._verify_bundle_products(room, turn.context)
        if verified is None:
            return _Primary(
                state=pre_turn,
                failure=TurnFailure(code=TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE),
            )

        card = self._bundle_references.resolve(
            BundleItemOrdinal(ordinal=action.bundle_ordinal), room, verified
        )
        if isinstance(card, BundleReferenceUnresolved):
            return _unresolved_bundle(card.reason, pre_turn)
        stale = _stale_reference(pre_turn, card.bundle_revision)
        if stale is not None:
            return stale
        need_id = _single_need(card)
        if need_id is None:
            return _Primary(
                state=pre_turn,
                clarification=DeterministicClarification(
                    reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
                    need_reason=DesignNeedFailureReason.CARD_SPANS_SEVERAL_NEEDS,
                ),
            )

        chosen = await self._references.resolve(
            PresentedOrdinal(position=action.alternative_ordinal), pre_turn, turn.context
        )
        if isinstance(chosen, ReferenceUnresolved):
            clarification, failure = _reference_outcome(
                chosen.reason, BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE
            )
            return _Primary(state=pre_turn, clarification=clarification, failure=failure)

        if not await self._is_product_for_need(need_id, chosen.product_id, room, turn.context):
            # The chosen option is not a product this role can take. Refused
            # rather than forced: a lamp does not go where the plan wants a rug.
            return _Primary(
                state=pre_turn,
                clarification=DeterministicClarification(
                    reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
                    need_reason=DesignNeedFailureReason.NO_NEED_MATCH,
                ),
            )

        override = DesignNeedSearchOverride(need_id=need_id, forced_product_id=chosen.product_id)
        refreshed = await self._reoptimise(
            pre_turn, turn, override=override, released_need_id=need_id
        )
        if isinstance(refreshed, _Primary):
            return refreshed
        if not _forced_into(refreshed.outcome, need_id, chosen.product_id, refreshed.state):
            # The choice could not be placed: it left the catalog between being
            # shown and being picked, or the room would not fit around it.
            return _Primary(
                state=pre_turn,
                failure=TurnFailure(code=_no_replacement_reason(refreshed.outcome)),
            )
        self._log_refinement(turn, BundleInteractionOp.REPLACE_PRODUCT, pre_turn, refreshed.state)
        return _Primary(
            state=refreshed.state,
            bundle_outcome=refreshed.outcome,
            bundle_change=BundleInteractionOp.REPLACE_PRODUCT,
        )

    async def _is_product_for_need(
        self,
        need_id: int,
        product_id: int,
        room: RoomProjectState,
        context: RetailerContext,
    ) -> bool:
        """Whether the chosen product is the kind of thing this role calls for.

        Read fresh and matched against the role's own commerce type, so a pick
        that does not belong is refused rather than forced. Fails closed: a
        product that cannot be read is treated as not matching, because forcing
        an unverifiable one would put a guess in the room (CLAUDE.md 31).
        """
        need = next((n for n in room.design_needs if n.need_id == need_id), None)
        if need is None:
            return False
        products = await self._hydration.hydrate_ids((product_id,), context)
        if not products:
            return False
        commerce = products[0].commerce
        if commerce.category != need.commerce_category:
            return False
        return (
            need.commerce_subcategory is None or commerce.subcategory == need.commerce_subcategory
        )

    async def _chosen_kinds(self, state: AgentStateV1, turn: CustomerTurnInput) -> tuple[str, ...]:
        """What kinds of thing they have chosen, in the order they chose them.

        Read fresh, like every other product fact: a kind taken from state
        would be a classification that was true when they picked it. One entry
        per choice, so the kinds and the count cannot disagree.

        A choice the catalog no longer returns yields nothing, which makes the
        lists shorter than the selection - so the projection carries kinds only
        when it has one for every choice, and otherwise carries none. A partial
        list read as a whole one is how "a sofa and a table" became "2 sofas".
        """
        chosen = state.product_interaction.selected_product_ids
        if not chosen:
            return ()
        try:
            products = await self._hydration.hydrate_ids(chosen, turn.context)
        except _HANDLED_CATALOG_FAILURES:
            logger.warning("chosen_kinds_unavailable", store_id=turn.context.store_id)
            return ()
        if len(products) != len(chosen):
            return ()
        kinds = tuple(
            customer_words_or_none(product.commerce.subcategory or product.commerce.category)
            for product in products
        )
        return (
            ()
            if any(kind is None for kind in kinds)
            else tuple(kind for kind in kinds if kind is not None)
        )

    async def _visible_cards(self, turn: CustomerTurnInput) -> tuple[PresentedCardView, ...]:
        """The products the customer was looking at when they typed.

        Read fresh rather than remembered. The session records which products
        were shown and in what order; what they cost and how many they seat
        comes from the catalog every turn, so "the second one" is priced at
        today's price and not at the price it had when the card was drawn
        (CLAUDE.md 61, 62).

        One bounded read of at most the presentation limit, and it never fails
        the turn: a catalog the agent could not reach leaves it talking about
        no card in particular, which is worse conversation and not a wrong
        answer (CLAUDE.md 21).
        """
        presented = turn.state.product_interaction.presented_product_ids
        if not presented:
            return ()
        try:
            products = await self._hydration.hydrate_ids(presented, turn.context)
        except _HANDLED_CATALOG_FAILURES:
            logger.warning("visible_cards_unavailable", store_id=turn.context.store_id)
            return ()
        return cards_from_candidates(products, presented)

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

        if not await self._is_the_kind_they_named(decision.interaction, outcome, context):
            clarification, failure = _reference_outcome(
                ReferenceFailureReason.KIND_MISMATCH,
                BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
            )
            return _Interaction(state=pre_turn, clarification=clarification, failure=failure)

        update = _interaction_update(decision.interaction.op, outcome.product_id, pre_turn)
        return _Interaction(
            state=apply_update(pre_turn, AgentStateUpdate(product_interaction=update)),
            applied=True,
        )

    async def _is_the_kind_they_named(
        self,
        intent: ProductInteractionIntent,
        outcome: ResolvedProductReference,
        context: RetailerContext,
    ) -> bool:
        """Whether the position resolved to the kind of thing they said it was.

        "Sofa five" carries a position *and* a kind. The position alone
        resolved perfectly against a list of five centre tables that had
        replaced the sofas behind the conversation, and a coffee table was
        selected for a customer talking about a sofa (M15 2).

        So the kind is checked against the catalog rather than trusted. An
        unapproved value is treated as no expectation at all: the model must
        never introduce a taxonomy value (CLAUDE.md 14.3), and refusing a
        reference over one it invented would punish the customer for our
        model's slip. What it can never do is *pass* a check against a value
        the registry does not contain.

        No expectation means no check - "the second one" names no kind, and
        inventing one from the active search would refuse references the
        customer never contradicted.
        """
        named = intent.expected_subcategory
        if named is None:
            return True
        if not self._taxonomy.is_subcategory(named):
            logger.info("interaction_kind_unapproved", store_id=context.store_id)
            return True

        products = await self._hydration.hydrate_ids((outcome.product_id,), context)
        if not products:
            # Unreadable, not mismatched. The reference resolver already
            # verified the product exists; a read that fails here should not
            # become a claim about what kind it is.
            return True
        actual = products[0].commerce.subcategory
        if actual == named:
            return True
        logger.info(
            "interaction_kind_mismatch",
            store_id=context.store_id,
            named=named,
            actual=actual,
        )
        return False

    # ── the primary action ──────────────────────────────────────────────────

    async def _execute(
        self,
        decision: CustomerAgentDecision,
        working: AgentStateV1,
        pre_turn: AgentStateV1,
        turn: CustomerTurnInput,
        proposals: AgentStateUpdate,
    ) -> _Primary:
        working = _with_seating_answer(working, decision)
        match decision.action:
            case AgentAction.CLARIFY if _asks_about_the_room(decision):
                # The room's questions are the application's, asked one at a
                # time from what is really missing (CLAUDE.md 10.1).
                asked = await self._room_question(apply_update(working, proposals), turn)
                return asked if asked is not None else _Primary(state=working)
            case AgentAction.ANSWER | AgentAction.CLARIFY:
                return _Primary(state=working)
            case AgentAction.BUNDLE_REFINE:
                return await self._bundle_refine(decision, working, pre_turn, turn)
            case AgentAction.DESIGN_HANDOFF:
                return await self._design_handoff(decision, working, pre_turn, turn, proposals)
            case AgentAction.SEARCH:
                if _pages_combinations(decision, working):
                    return await self._more_combinations(decision, working, pre_turn, turn)
                if decision.show_more or decision.exclude_reference is not None:
                    return await self._continue_search(decision, working, pre_turn, turn)
                if decision.reference is None:
                    return await self._new_search(decision, working, turn)
                return await self._similar_search_turn(decision.reference, working, pre_turn, turn)
            case AgentAction.REFINE_SEARCH:
                return await self._refine(decision, working, pre_turn, turn)
            case AgentAction.PRODUCT_DETAIL:
                return await self._product_detail(decision, working, pre_turn, turn)
            case AgentAction.COMPARE:
                return await self._compare(decision, working, pre_turn, turn)
            case AgentAction.SHOW_SELECTION:
                return await self._show_selection(working, turn)

    async def _show_selection(self, working: AgentStateV1, turn: CustomerTurnInput) -> _Primary:
        """The products they have chosen, put back on screen.

        Read fresh from the catalog, in the order they chose them. The session
        records which products; what they cost today comes from the catalog,
        so a card drawn now shows today's price rather than the price it had
        when they picked it (CLAUDE.md 61).

        Nothing is searched, ranked or relaxed - there is no query here, only a
        list the customer already built. So the grounding carries no provenance
        either: a product they chose has no relaxation depth, and claiming one
        would describe a search that never ran.

        **It does not become the presented list.** "The second one" still means
        the second of whatever they were browsing; a list they asked to review
        is not a new result set, and renumbering their search under them is how
        an ordinal starts meaning something else (M17 3).
        """
        chosen = working.product_interaction.selected_product_ids
        if not chosen:
            logger.info("show_selection_empty", store_id=turn.context.store_id)
            return _Primary(
                state=working,
                failure=TurnFailure(code=TurnFailureCode.NOTHING_SELECTED),
            )

        try:
            products = await self._hydration.hydrate_ids(chosen, turn.context)
        except _HANDLED_SEARCH_FAILURES:
            logger.warning("show_selection_unavailable", store_id=turn.context.store_id)
            return _Primary(
                state=working,
                failure=TurnFailure(code=TurnFailureCode.PRODUCT_UNAVAILABLE),
            )

        if not products:
            # Everything they chose has since left the catalog. Reported as
            # nothing to show rather than as an empty success.
            logger.info("show_selection_all_stale", store_id=turn.context.store_id)
            return _Primary(
                state=working,
                failure=TurnFailure(code=TurnFailureCode.PRODUCT_UNAVAILABLE),
            )

        grounded = tuple(
            to_grounded_product(
                product,
                grounding_ref=position,
                presented_ordinal=position,
                relaxation_depth=None,
            )
            for position, product in enumerate(products, start=1)
        )
        return _Primary(state=working, selection=SelectionGrounding(products=grounded))

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

        if decision.design_scope is DesignScope.ADVICE:
            return await self._design_advice(decision, state, turn)

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

        # A living room or a bedroom asks what it still needs first - one
        # question per turn, each once (CLAUDE.md 10.1).
        asked = await self._room_question(state, turn)
        if asked is not None:
            return asked

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

        request = self._design_request(state, turn, capabilities, locks, revision.excluded)
        budget = state.room_project.budget if state.room_project else None
        locked = tuple(product for _, product in locks)
        template = self._composed_template(state, request)
        try:
            if template is not None:
                plan, discovery, outcome = await self._composed_room(
                    template, state, turn, request, plan, capabilities, locked
                )
            else:
                discovery = await self._design_discovery.discover(request, plan, turn.context)
                outcome = self._optimizer.optimize(
                    BundleOptimizationRequest(discovery=discovery, budget=budget, locked=locked)
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
        committed = self._commit(state, plan, outcome)
        self._log_whole_room(turn, state, plan, discovery, outcome, committed, started)
        return _Primary(
            state=committed,
            design_handoff=True,
            proposals_applied=True,
            bundle_outcome=outcome,
        )

    # ── a room of chosen pieces ─────────────────────────────────────────────

    def _with_room_pieces(
        self, decision: CustomerAgentDecision, proposals: MappedProposals, pre_turn: AgentStateV1
    ) -> MappedProposals:
        """The room kind checked against the registry, and the pieces they named
        resolved to its keys. A kind or a key the registry does not hold is
        dropped, never mapped onto a near one (CLAUDE.md 14.3)."""
        proposal = decision.state_proposal
        if self._rooms is None or proposal is None:
            return proposals
        kind = proposal.room_kind
        if kind is not None and self._rooms.template(kind) is None:
            logger.info("room_kind_not_in_registry")
            kind = None
        current = pre_turn.room_project.room_kind if pre_turn.room_project else None
        template = self._rooms.template(kind or current)
        pieces = (
            chosen_keys(template, proposal.room_pieces, default=proposal.room_pieces_default)
            if template is not None
            else None
        )
        room = proposals.update.room_project
        if room is None and pieces is None:
            return proposals
        updated = (room or RoomProjectUpdate()).model_copy(
            update={"room_kind": kind, "pieces": pieces}
        )
        return proposals._replace(
            update=proposals.update.model_copy(update={"room_project": updated})
        )

    async def _room_question(self, state: AgentStateV1, turn: CustomerTurnInput) -> _Primary | None:
        """This turn's one question about the room, or `None` to build it.

        Only for a room the registry knows and not yet built. The question is
        recorded as asked with it, so it is never asked twice.
        """
        room = state.room_project
        template = self._rooms.template(room.room_kind) if self._rooms and room else None
        if room is None or template is None:
            return None
        try:
            capabilities = await self._capabilities.capabilities(turn.context)
        except _HANDLED_DESIGN_FAILURES:
            logger.warning("room_question_capabilities_unavailable", store_id=turn.context.store_id)
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                failure=TurnFailure(code=TurnFailureCode.DESIGN_UNAVAILABLE),
            )
        question = next_question(room, template, capabilities, _earlier_seat_count(state, template))
        if question is None:
            return None
        logger.info(
            "room_question_asked",
            store_id=turn.context.store_id,
            room_kind=template.kind,
            question=str(question.kind),
            offered_pieces=len(question.pieces),
        )
        return _Primary(
            state=apply_update(
                state,
                AgentStateUpdate(room_project=RoomProjectUpdate(question_asked=question.kind)),
            ),
            design_handoff=True,
            proposals_applied=True,
            room_question=question,
        )

    def _room_seats(self, outcome: BundleOptimizationOutcome | None) -> int | None:
        """How many the room's seating really seats, counted from its pieces.

        A recorded capacity, or the reviewed count for a type that seats one.
        `None` when there is no room, no seating, or a seating piece whose count
        nobody established - then no seat claim is made at all.
        """
        if not isinstance(outcome, RoomBundle) or self._seating is None:
            return None
        total = 0
        for line in outcome.lines:
            commerce = line.product.commerce
            if commerce.category != SEATING_CATEGORY:
                continue
            seats = commerce.seating_capacity or self._seating.implied_capacity(
                commerce.subcategory
            )
            if seats is None:
                return None
            total += seats * line.quantity
        return total or None

    def _composed_template(
        self, state: AgentStateV1, request: InteriorDesignRequest
    ) -> RoomTemplate | None:
        """The template a new room is built from, when it is one the registry
        knows. A revision of an existing room keeps its own plan."""
        room = state.room_project
        if self._rooms is None or room is None or request.revision is not None:
            return None
        return self._rooms.template(room.room_kind)

    async def _composed_room(
        self,
        template: RoomTemplate,
        state: AgentStateV1,
        turn: CustomerTurnInput,
        request: InteriorDesignRequest,
        designed: InteriorDesignResult,
        capabilities: RetailerCatalogCapabilities,
        locked: tuple[LockedBundleProduct, ...],
    ) -> _ComposedRoom:
        """The room built from exactly the pieces they chose (CLAUDE.md 10.3).

        Every piece but the seating is one need, searched once. The seating is
        sized to the head count: each way the store can seat them within the
        budget is tried with the rest of the room, and the room that keeps all
        its seats and the most of its pieces wins - so a sofa never crowds out
        the rug, and the rug never leaves someone standing (CLAUDE.md 27).
        """
        room = state.room_project
        assert room is not None, "a composed room has a room"
        keys = room.pieces if room.pieces is not None else default_pieces(template)
        others = InteriorDesignResult(
            guidance=designed.guidance,
            needs=tuple(composed_needs(template, keys, capabilities, designed)),
        )
        found = await self._design_discovery.discover(request, others, turn.context)
        seating = seating_piece(template, keys, capabilities)
        budget = room.budget

        def optimise(
            seats: Sequence[tuple[DesignCategoryNeed, CandidatePoolResult]],
        ) -> _ComposedRoom:
            plan, discovery = _with_seating(others, found, seats)
            outcome = self._optimizer.optimize(
                BundleOptimizationRequest(discovery=discovery, budget=budget, locked=locked)
            )
            return plan, discovery, outcome

        if seating is None:
            return optimise(())

        priority = PRIORITY_FOR_TIER[seating.tier]
        count = room.regular_seating_count
        arrangements: tuple[SeatingArrangement, ...] = ()
        if count is not None:
            arrangements = await self._seating_planner.arrangements(
                target_seats=count,
                budget_amount=budget.max_amount if budget else None,
                # Only read to price a budget; without one nothing is priced.
                currency=budget.currency if budget else "",
                context=turn.context,
                types=frozenset(seating.seating_types),
                requirements=_room_seating_wishes(room),
            )
        if not arrangements:
            # Nothing seats them within the budget, or no head count was
            # given: one piece of the main type, which the reply then names
            # as missing, or as the one sofa it chose (CLAUDE.md 10.3).
            need = DesignCategoryNeed(
                commerce_category=seating.commerce_category,
                commerce_subcategory=seating.seating_types[0],
                priority=priority,
                seating_capacity=(
                    SeatingCapacityConstraint(min_capacity=count) if count is not None else None
                ),
            )
            pool = await self._design_discovery.discover(
                request, InteriorDesignResult(needs=(need,)), turn.context
            )
            entry = pool.needs[0] if pool.needs else None
            return optimise(
                ((need, entry.pool),) if entry is not None and entry.pool is not None else ()
            )

        pools: dict[int, CandidatePoolResult] = {}
        best: tuple[tuple[int, ...], _ComposedRoom] | None = None
        for position, arrangement in enumerate(arrangements):
            seats: list[tuple[DesignCategoryNeed, CandidatePoolResult]] = []
            for line in arrangement.lines:
                if line.product_id not in pools:
                    pools[line.product_id] = await self._pipeline.execute_forced_pool(
                        line.product_id, turn.context
                    )
                seats.append(
                    (
                        DesignCategoryNeed(
                            commerce_category=seating.commerce_category,
                            commerce_subcategory=line.commerce_subcategory,
                            priority=priority,
                            quantity=line.quantity,
                            # Its alternatives seat the same number, so a swap
                            # keeps the room's head count (a one-seat type
                            # carries no filter: discovery drops it).
                            seating_capacity=SeatingCapacityConstraint(
                                min_capacity=line.seats_each, max_capacity=line.seats_each
                            ),
                        ),
                        pools[line.product_id],
                    )
                )
            tried = optimise(seats)
            score = (*_room_shortfall(tried[2], len(seats)), position)
            if best is None or score < best[0]:
                best = (score, tried)
        assert best is not None
        return best[1]

    async def _design_advice(
        self,
        decision: CustomerAgentDecision,
        state: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """A design question, answered as design knowledge.

        No catalog, no search, no products. "What colours work with walnut?" is
        a question about rooms in general, and answering it with a shelf of
        rugs would answer something else and bury what they asked
        (CLAUDE.md 36, 38).

        The specialist gets no capabilities and no anchors beyond whatever the
        customer's own message carried, because general advice is not a claim
        about what this retailer stocks - and `_advice_only` drops any need it
        returns, so a product cannot enter through this door (CLAUDE.md 41).

        A failure here is the customer's question going unanswered, so unlike a
        suggestion of ours it is reported.
        """
        if self._design is None:
            logger.info("design_advice_not_configured", store_id=turn.context.store_id)
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                failure=TurnFailure(code=TurnFailureCode.DESIGN_ADVICE_UNAVAILABLE),
            )

        # Their question, put back together. A question can span turns - "how
        # big should a rug be?" then "5x5" then "m" - and the last message on
        # its own is the word **m**, which the specialist answered with nothing
        # (M16 1). The decision model reads the thread and restates it; the
        # message itself is used when it asks the whole question by itself.
        question = _design_question(decision.design_question or turn.message)
        if question is None:
            # Nothing to answer. A message too long to carry as a question is
            # not a design question we can put to the specialist, and passing a
            # truncated one would ask something they did not say.
            logger.info("design_advice_unquotable", store_id=turn.context.store_id)
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                failure=TurnFailure(code=TurnFailureCode.DESIGN_ADVICE_UNAVAILABLE),
            )

        room = state.room_project
        request = InteriorDesignRequest(
            task=DesignTask.GENERAL_ADVICE,
            question=question,
            room_type=room.room_type if room else None,
            design_preferences=room.design_preferences if room else (),
            regular_seating_count=room.regular_seating_count if room else None,
            anchors=await self._advice_anchors(decision, state, turn),
        )
        try:
            plan = await self._design.plan(request)
        except _HANDLED_DESIGN_FAILURES:
            logger.warning("design_advice_unavailable", store_id=turn.context.store_id)
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                failure=TurnFailure(code=TurnFailureCode.DESIGN_ADVICE_UNAVAILABLE),
            )

        if not plan.guidance:
            # A design question with no design answer. Reported rather than
            # dressed up: there is nothing to tell them.
            logger.info("design_advice_empty", store_id=turn.context.store_id)
            return _Primary(
                state=state,
                design_handoff=True,
                proposals_applied=True,
                failure=TurnFailure(code=TurnFailureCode.DESIGN_ADVICE_UNAVAILABLE),
            )
        return _Primary(
            state=state,
            design_handoff=True,
            # Applied above, before the specialist was asked, so a room size
            # stated in the same breath as the question is part of it. Saying
            # so stops `run` folding them in a second time, which would double
            # every preference the turn added.
            proposals_applied=True,
            design_guidance=plan.guidance,
        )

    async def _advice_anchors(
        self,
        decision: CustomerAgentDecision,
        state: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> tuple[AnchorProduct, ...]:
        """The piece the question is about, when it names one.

        "Would the second sofa work with a walnut coffee table?" is a design
        question *about something on screen*, so the specialist is told the
        design facts of that piece - its kind, colour, styles and size - and
        nothing that identifies it (CLAUDE.md 42).

        An unresolvable reference yields no anchor rather than a failure: the
        general form of the question is still answerable, and refusing it over
        a pointing word would be worse than answering it broadly.
        """
        if decision.reference is None:
            return ()
        outcome = await self._references.resolve(decision.reference, state, turn.context)
        if isinstance(outcome, ReferenceUnresolved):
            return ()
        products = await self._hydration.hydrate_ids((outcome.product_id,), turn.context)
        if not products:
            return ()
        return project_anchors(
            list(products),
            dimensions=self._dimensions,
            locked_product_ids=[products[0].product_id],
            quantities={products[0].product_id: 1},
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

        # Recorded before anything else is attempted. A complement exists
        # because they settled on this piece, and until now the piece was used
        # to ask the specialist and then forgotten - so a customer who chose a
        # sofa, was offered rugs and chose one of those had *neither* choice
        # on record, while the agent went on talking as though both were
        # (M17 1).
        #
        # Unconditional: whether the suggestion finds anything is our business,
        # and their choice is theirs. It survives a specialist that fails, a
        # retailer with nothing to suggest, and a search that comes back empty.
        state = self._settle_on(state, product.product_id)

        try:
            capabilities = await self._capabilities.capabilities(turn.context)
            request = await self._complement_request(state, turn, capabilities, product)
            plan = await self._design.plan(request)
        except _HANDLED_DESIGN_FAILURES:
            # Our own idea, and it could not be formed. Reporting it would tell
            # the customer that something failed when the thing they actually
            # did - choosing a product - succeeded (CLAUDE.md 51).
            logger.warning("complement_unavailable", store_id=turn.context.store_id)
            return _Primary(state=state, design_handoff=True, proposals_applied=True)

        if not plan.needs:
            # The specialist had nothing to suggest this retailer can supply.
            # Reported as a handoff that produced nothing rather than filled
            # with a category nobody chose.
            logger.info("complement_no_need", store_id=turn.context.store_id)
            return _Primary(state=state, design_handoff=True, proposals_applied=True)

        return await self._first_viable_complement(plan.needs, request, state, turn)

    async def _first_viable_complement(
        self,
        needs: Sequence[DesignCategoryNeed],
        request: InteriorDesignRequest,
        state: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> _Primary:
        """The best complementary role this retailer can actually fill.

        The specialist returns a short ordered list rather than one answer,
        because "supported" and "in stock for this request" are different
        questions and only the second can be settled by running the search. A
        retailer with one lounge chair supports lounge chairs; proposing it and
        finding nothing left the customer with an empty screen and a remark
        about a product type they never mentioned (CLAUDE.md 26).

        Strictly in order, and the first that returns products wins. The order
        is the specialist's judgement of design usefulness, so falling past a
        role is a statement about stock, never a re-ranking by this service -
        no category is guessed here, and none is reordered.

        **One category per turn.** Later roles are discarded rather than
        queued: a customer who liked a sofa is offered a rug, not a rug and a
        table and a lamp (CLAUDE.md 28).

        Each attempt runs from the same pre-attempt state, so a role that came
        to nothing promotes no criteria and leaves nothing for the next turn's
        "a cheaper one" to refine.
        """
        for position, need in enumerate(needs, start=1):
            resolved = self._design_discovery.resolve_need(need, request)
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
            attempt = await self._run_search(
                composed, state, turn.context, recover_seating=False
            )
            if attempt.failure is not None:
                # The catalog, not the idea. Trying the next role would issue
                # another query against something that just failed.
                break
            if attempt.search is not None and attempt.search.products:
                if position > 1:
                    logger.info(
                        "complement_role_fallback",
                        store_id=turn.context.store_id,
                        attempts=position,
                    )
                return replace(attempt, design_handoff=True, proposals_applied=True)

        # Every role the specialist proposed came back empty, or the catalog
        # could not be reached. Either way this was *our* idea and it produced
        # nothing, so the turn carries no search grounding and no failure: the
        # customer asked for none of it, and nothing they asked for failed
        # (CLAUDE.md 51).
        logger.info(
            "complement_no_products",
            store_id=turn.context.store_id,
            roles_tried=len(needs),
        )
        return _Primary(state=state, design_handoff=True, proposals_applied=True)

    @staticmethod
    def _settle_on(state: AgentStateV1, product_id: int) -> AgentStateV1:
        """Record the piece the customer has settled on.

        Selected *and* focused. Selected because it is one of the things they
        are buying; focused because it is the one they are talking about, so
        "show me the one I picked" follows the latest choice rather than the
        first (M15 3).

        Idempotent - selecting the same product twice is one selection - so a
        customer who says they like it again loses nothing and gains no
        duplicate.
        """
        return apply_update(
            state,
            AgentStateUpdate(
                product_interaction=ProductInteractionUpdate(
                    selected_product_ids=AddItems(items=(product_id,)),
                    focused_product_id=product_id,
                )
            ),
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

    async def _complement_request(
        self,
        state: AgentStateV1,
        turn: CustomerTurnInput,
        capabilities: RetailerCatalogCapabilities,
        product: ProductCandidate,
    ) -> InteriorDesignRequest:
        """What the specialist is told about what they already have.

        **Every piece they have chosen**, not only the newest. A complement is
        what is *missing*, and that cannot be worked out from one product: a
        customer who had picked a sofa, then picked a centre table, was offered
        sofas - because the table was the only anchor and nothing said a sofa
        was already settled (M19 1).

        The piece they just settled on comes first, so the specialist reads it
        as the one in question and the rest as context.

        Anchors carry design facts only - kind, colour, styles, size - and no
        identity, exactly as a room plan's anchors do. Nothing here says which
        product it is, what it cost, or where it came from.
        """
        room = state.room_project
        chosen = await self._chosen_alongside(product, state, turn)
        return InteriorDesignRequest(
            task=DesignTask.COMPLEMENTARY_RECOMMENDATION,
            design_brief=_design_brief(turn.message),
            room_type=room.room_type if room else None,
            design_preferences=room.design_preferences if room else (),
            regular_seating_count=room.regular_seating_count if room else None,
            catalog_capabilities=capabilities,
            anchors=project_anchors(
                chosen,
                dimensions=self._dimensions,
                locked_product_ids=[p.product_id for p in chosen],
                quantities={p.product_id: 1 for p in chosen},
            ),
        )

    async def _chosen_alongside(
        self,
        product: ProductCandidate,
        state: AgentStateV1,
        turn: CustomerTurnInput,
    ) -> list[ProductCandidate]:
        """The piece in question, then everything else they have chosen.

        Read fresh, like every anchor: describing a product from remembered
        state would assert facts nobody checked. A selection the catalog no
        longer returns simply produces no anchor.
        """
        others = tuple(
            product_id
            for product_id in state.product_interaction.selected_product_ids
            if product_id != product.product_id
        )
        if not others:
            return [product]
        try:
            alongside = await self._hydration.hydrate_ids(others, turn.context)
        except _HANDLED_CATALOG_FAILURES:
            # Context, not the answer. Losing it means a weaker suggestion,
            # never a wrong one.
            logger.warning("complement_context_unavailable", store_id=turn.context.store_id)
            return [product]
        return [product, *alongside]

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
        reference = decision.anchor_reference
        if reference is None:
            return _Anchored()
        anchor = decision.design_anchor

        outcome = await self._references.resolve(reference, pre_turn, turn.context)
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

        # A bare reference names the piece and says nothing else about it, so
        # the application's own defaults apply - exactly as they do when
        # `design_anchor` leaves them unsaid. Neither is read as a claim the
        # customer did not make (CLAUDE.md 3.3).
        acquisition = (anchor.acquisition if anchor else None) or BundleAcquisition.TO_BUY
        quantity = anchor.quantity if anchor else 1
        if existing:
            line = existing[0]
            operations: tuple[BundleOperation, ...] = (
                SetBundleLineStatus(line_id=line.line_id, status=BundleItemStatus.LOCKED),
                SetBundleLineQuantity(line_id=line.line_id, quantity=quantity),
                SetBundleLineAcquisition(line_id=line.line_id, acquisition=acquisition),
            )
        else:
            operations = (
                AddBundleLine(
                    line=BundleLineSpec(
                        product_id=outcome.product_id,
                        quantity=quantity,
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
        """A genuinely new task.

        M7 reads one message and nothing else, which keeps the same sentence
        meaning the same thing on every turn. When the request was finished
        across turns the decision restates it, and the restatement is what M7
        reads - still one self-contained request, interpreted and validated
        exactly as a message would be (M22 1).
        """
        interpretation = await self._interpret(decision.search_request or turn.message)
        if isinstance(interpretation, TurnFailure):
            return _Primary(state=working, failure=interpretation)
        if isinstance(interpretation, UnresolvedStrictRequirement):
            interpretation = _search_despite(interpretation)
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
            saved_measurements=_saved_sizes(
                decision, working, resolved.request.commerce_subcategory
            ),
            revision=_current_revision(working),
        )
        return await self._run_search(composed, working, turn.context, save_sizes=True)

    async def _maybe_compose_seating(
        self,
        resolved: ResolvedSearch,
        primary: _Primary,
        context: RetailerContext,
    ) -> _Primary:
        """When a seat count no single piece can meet leaves a search empty,
        offer combinations instead of a dead end (CLAUDE.md 27).

        Only a zero-result seating search with a stated minimum seat count
        qualifies; the customer's exact request always ran first. The first
        time for a seat count, when there is a real choice of shape or their
        colour is unknown, the customer is asked which shape they would like -
        only shapes that exist, each with its lowest total, never a budget
        question. Asked once: afterwards the chosen shape, or the best of each,
        is shown.

        Recovery attaches only for outcomes there is something to say about.
        A single piece that in fact suffices, or a store with no seating, leaves
        the ordinary zero-result reply untouched.
        """
        request = resolved.request
        if primary.search is None or primary.search.products:
            return primary
        if request.commerce_category != SEATING_CATEGORY or request.seating_capacity is None:
            return primary
        target = request.seating_capacity.min_capacity
        if target is None:
            return primary

        if request.price is not None:
            budget: Decimal | None = request.price.max_amount
            currency = request.price.currency
        else:
            budget = None
            store_currency = (await self._capabilities.overview(context)).currency
            if store_currency is None:
                # Nothing priced in one clean currency: a composed bundle could
                # not state a trustworthy total, so leave the plain reply.
                return primary
            currency = store_currency

        offer = primary.state.seating_offer
        if offer is not None and offer.target_seats != target:
            offer = None
        requirements = _seating_requirements(resolved)
        solution = await self._seating_planner.plan(
            target_seats=target,
            budget_amount=budget,
            currency=currency,
            context=context,
            requirements=requirements,
            shape=offer.chosen_shape if offer is not None else None,
            exclude=frozenset(
                _contents(combination) for combination in (offer.excluded if offer else ())
            ),
        )
        if solution.outcome is SeatingSolutionOutcome.NONE_WITHIN_BUDGET and (
            solution.closest_total is not None and offer is None
        ):
            # "The closest is about 3,700 - shall I show it?" is this seat
            # count's one question: a yes shows the combinations, never another
            # question first.
            return replace(
                primary,
                state=_with_offer(
                    primary.state, SeatingOfferState(target_seats=target, shape_asked=True)
                ),
                seating_solution=solution,
            )
        if solution.outcome in (
            SeatingSolutionOutcome.NONE_WITHIN_BUDGET,
            SeatingSolutionOutcome.NO_MORE,
        ):
            # Nothing new on screen: what they saw last is still what "the
            # second option" means.
            return replace(primary, seating_solution=solution)
        if solution.outcome is not SeatingSolutionOutcome.BUNDLES:
            return primary

        shapes = tuple(option.shape for option in solution.options)
        if _should_ask_shape(offer, solution, requirements):
            asked = SeatingOfferState(target_seats=target, offered_shapes=shapes, shape_asked=True)
            question = SeatingSolution(
                target_seats=solution.target_seats,
                budget_amount=solution.budget_amount,
                currency=solution.currency,
                outcome=SeatingSolutionOutcome.CHOOSE_SHAPE,
                options=solution.options,
                ask_colour=not _colour_known(requirements),
                lifted=solution.lifted,
            )
            return replace(
                primary,
                state=_with_offer(primary.state, asked),
                seating_solution=question,
            )

        shown = SeatingOfferState(
            target_seats=target,
            # Every shape ever offered: after paging, `shapes` lists only those
            # with unseen combinations, and a chosen shape must stay offered.
            offered_shapes=tuple(
                dict.fromkeys((*(offer.offered_shapes if offer else ()), *shapes))
            ),
            shape_asked=offer.shape_asked if offer is not None else False,
            chosen_shape=offer.chosen_shape if offer is not None else None,
            chosen=offer.chosen if offer is not None else None,
            excluded=offer.excluded if offer is not None else (),
            shown=tuple(
                OfferedCombination(
                    shape=bundle.shape,
                    lines=tuple(
                        OfferedCombinationLine(product_id=line.product_id, quantity=line.quantity)
                        for line in bundle.lines
                    ),
                )
                for bundle in solution.bundles
            ),
        )
        return replace(primary, state=_with_offer(primary.state, shown), seating_solution=solution)

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
        *,
        save_sizes: bool = False,
        recover_seating: bool = True,
    ) -> _Primary:
        """Execute, then promote and commit in one step.

        The candidate is local until the pipeline returns. On success the
        criteria are promoted through the reducer - which carries the current
        revision forward - and `commit_search_results` advances it exactly
        once, atomically with the results it belongs to.

        `save_sizes` is set only for a search the customer asked
        for: its sizes are then saved against its product type. A room plan's
        or a similar-product search states no size of its own, and letting it
        save would erase the one the customer gave.

        The seating-combination recovery lives here rather than on any one caller,
        so a seat count no single piece can meet is answered by pairing pieces
        whether the customer stated it fresh, refined a budget onto it, or paged
        (CLAUDE.md 27). `recover_seating=False` opts a caller out - the cross-sell
        suggestion loop does, because a piece we proposed that finds nothing is
        withheld, not turned into a bundle the customer never asked for.
        """
        try:
            execution = await self._pipeline.execute(
                composed.resolved,
                context,
                dropped_constraints=composed.dropped_constraints,
                earlier_sizes_applied=composed.earlier_sizes_applied,
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
        committed = commit_search_results(promoted, execution.presented_product_ids)
        primary = _Primary(
            state=remember_measurements(committed) if save_sizes else committed,
            search=execution.grounding,
        )
        if not recover_seating:
            return primary
        another = await self._another_type_seats_them(composed, primary, working, context)
        if another is not None:
            return another
        return await self._maybe_compose_seating(composed.resolved, primary, context)

    async def _another_type_seats_them(
        self,
        composed: ComposedSearch,
        primary: _Primary,
        working: AgentStateV1,
        context: RetailerContext,
    ) -> _Primary | None:
        """A seat count the type they asked for never reaches, but another main
        type does in a single piece - "a sofa for six" where sofas stop at
        four and sofa sets seat six or seven.

        That other type is searched with everything else they asked kept, and
        shown as the best fit for them (CLAUDE.md 27.1). Only when the asked
        type itself cannot seat them: a purple sofa for three that found
        nothing is a colour problem, not a reason to change the type.
        """
        request = composed.resolved.request
        asked = request.commerce_subcategory
        capacity = request.seating_capacity
        if (
            self._seating is None
            or primary.search is None
            or primary.search.products
            or request.commerce_category != SEATING_CATEGORY
            or asked is None
            or capacity is None
            or capacity.min_capacity is None
        ):
            return None
        target = capacity.min_capacity
        shelves = (await self._capabilities.overview(context)).shelves_in(SEATING_CATEGORY)
        asked_ceiling = max(
            (s.seating.maximum for s in shelves if s.commerce_subcategory == asked and s.seating),
            default=None,
        )
        if asked_ceiling is not None and asked_ceiling >= target:
            return None
        others = sorted(
            (
                shelf
                for shelf in shelves
                if shelf.commerce_subcategory not in (None, asked)
                and self._seating.is_combination_main(shelf.commerce_subcategory)
                and shelf.seating is not None
                and shelf.seating.maximum >= target
            ),
            key=lambda shelf: shelf.price_minimum,
        )
        for shelf in others:
            instead = _with_subcategory(composed, shelf.commerce_subcategory)
            found = await self._run_search(instead, working, context, recover_seating=False)
            if found.search is not None and found.search.products:
                logger.info(
                    "seating_type_offered_instead",
                    store_id=context.store_id,
                    asked=asked,
                    offered=shelf.commerce_subcategory,
                    target_seats=target,
                )
                return replace(found, offered_instead_of=asked)
        return None

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

        # Only a refinement that touched a size says anything about sizes. A
        # plain "cheaper ones" after a similar-product search, which stated no
        # size, must not save that silence over the size they gave earlier.
        return await self._compose_and_run(
            self._composer.refine(working.active_search, delta),
            working,
            turn,
            save_sizes=bool(delta.dimensions) or delta.planar_dimensions is not None,
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
        if isinstance(interpretation, UnresolvedStrictRequirement):
            # Only the product type crosses over from here, so a strict colour
            # nothing expresses is no reason to stop and ask (see _search_despite).
            interpretation = _search_despite(interpretation)
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
            saved_measurements=_saved_sizes(
                decision, working, interpretation.request.commerce_subcategory
            ),
        )
        if isinstance(outcome, NewTaskRequired):
            # A different product family is a different task, so nothing of the
            # old one carries across (CLAUDE.md 13.3).
            return await self._seed_and_execute(interpretation, decision, working, turn)
        return await self._compose_and_run(outcome, working, turn, save_sizes=True)

    async def _compose_and_run(
        self,
        outcome: CompositionOutcome,
        working: AgentStateV1,
        turn: CustomerTurnInput,
        *,
        save_sizes: bool,
    ) -> _Primary:
        match outcome:
            case ComposedSearch():
                return await self._run_search(outcome, working, turn.context, save_sizes=save_sizes)
            case CompositionNeedsClarification():
                return _Primary(
                    state=working,
                    clarification=DeterministicClarification(reason=outcome.reason),
                )
            case CompositionFailed(defect=CompositionDefect.NO_ACTIVE_SEARCH):
                # Not a sequencing bug, and the only defect that is not. The
                # turn before asked a question and ran no search, so there is
                # nothing to adjust - which is an ordinary place for a
                # conversation to be, and a question anyone can put to a
                # customer. Raising made answering the agent's own question a
                # failed turn (M21 1).
                logger.info("turn_refinement_without_a_search")
                return _Primary(
                    state=working,
                    clarification=DeterministicClarification(
                        reason=BlockingClarificationReason.NO_SEARCH_TO_REFINE
                    ),
                )
            case CompositionFailed():
                # An unapproved attribute value, an unresolved relative price.
                # Each means the decision did not match the state it was shown,
                # which is not a question anyone can put to a customer. A one-seat
                # misreading is expected model behaviour the correction fixes, so
                # it is not logged as an error.
                log = (
                    logger.warning
                    if outcome.defect is CompositionDefect.ONE_SEAT_ON_MULTI_SEAT_TYPE
                    else logger.error
                )
                log("turn_composition_defect", defect=str(outcome.defect))
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
        # Recorded in column order, so "the second one" can mean the second
        # column rather than only the second search result. Taken from the
        # comparison the service actually built, never from the references
        # asked for: a target that could not be resolved is not a column
        # (M15 1).
        return _Primary(
            state=apply_update(
                working,
                AgentStateUpdate(
                    product_interaction=ProductInteractionUpdate(
                        # The resolved ids, in the order asked for. A
                        # successful comparison covers exactly them - it
                        # refuses outright rather than dropping one - so this
                        # is the column order the customer is reading.
                        compared_product_ids=tuple(product_ids)
                    )
                ),
            ),
            comparison=comparison,
        )

    # ── query understanding ─────────────────────────────────────────────────

    async def _interpret(self, message: str) -> QueryInterpretation | TurnFailure:
        """M7 on the current message alone.

        No history and no state: it interprets what was just said, and giving
        it the conversation would make the same sentence mean different things
        on different turns.
        """
        try:
            return await self._query_understanding.interpret(message)
        except IntegrationUnavailableError:
            logger.warning("turn_query_understanding_unavailable")
            return TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE)
        except (LLMResponseInvalidError, TaxonomyValidationError) as exc:
            # Unusable even after its own corrective attempt. The whole turn is
            # then not understood - not only its search - so it ends with the
            # pre-turn state: a selection or budget from the same message must
            # not be kept while the customer is asked to say it again. Marked
            # so the turn is not re-decided; interpretation had its retry.
            logger.warning(
                "turn_query_understanding_not_understood",
                error_type=type(exc).__name__,
                reason=exc.context.get("reason"),
            )
            raise LLMResponseInvalidError(
                reason="interpretation unusable", stage="interpretation"
            ) from exc

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
            selection=primary.selection,
            product_detail=primary.product_detail,
            comparison=primary.comparison,
            clarification=decision.clarification,
            deterministic_clarification=deterministic,
            failure=failure,
            design_guidance=primary.design_guidance,
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


MAX_EXCLUDED_COMBINATIONS = 60
"""How many paged-past or turned-down combinations are remembered - the
oldest are forgotten first, never refused."""


def _pages_combinations(decision: CustomerAgentDecision, state: AgentStateV1) -> bool:
    """More, or not this one, said about combinations on screen."""
    offer = state.seating_offer
    return (
        offer is not None
        and bool(offer.shown)
        and (decision.show_more or decision.combination_dismiss is not None)
    )


def _contents(combination: OfferedCombination) -> tuple[tuple[int, int], ...]:
    """A combination as its products and quantities, the builder's identity."""
    return tuple(sorted((line.product_id, line.quantity) for line in combination.lines))


def _should_ask_shape(
    offer: SeatingOfferState | None,
    solution: SeatingSolution,
    requirements: SeatingRequirements,
) -> bool:
    """Ask first only once per seat count, and only when there is something
    worth asking: a real choice of shape, or a colour nobody has mentioned."""
    if not solution.options:
        return False
    if offer is not None and (offer.shape_asked or offer.chosen_shape is not None):
        return False
    return len(solution.options) >= 2 or not _colour_known(requirements)


def _colour_known(requirements: SeatingRequirements) -> bool:
    """Whether they named a colour, strictly or as a wish, or one is on record."""
    return bool(
        requirements.colors_any_of
        or requirements.wished_colors
        or AttributeFamily.COLOR in requirements.unmatchable_strict
    )


def _with_offer(state: AgentStateV1, offer: SeatingOfferState) -> AgentStateV1:
    """The state with this seating offer recorded - application-owned, like
    the committed results it sits beside."""
    return AgentStateV1(
        customer_preferences=state.customer_preferences,
        active_search=state.active_search,
        product_interaction=state.product_interaction,
        room_project=state.room_project,
        derived_commerce=state.derived_commerce,
        seating_offer=offer,
    )


def _with_seating_answer(state: AgentStateV1, decision: CustomerAgentDecision) -> AgentStateV1:
    """Their answer to the shape question, recorded before the search runs.

    Only a shape that was actually offered is taken; anything else, and "any",
    leave the choice open - the best of every shape is then shown.
    """
    offer = state.seating_offer
    if offer is not None and decision.combination_choice is not None:
        return _with_chosen_combination(state, offer, decision.combination_choice)
    answer = decision.seating_answer
    if offer is None or answer is None:
        return state
    shape = answer.shape
    chosen = shape if shape in offer.offered_shapes else None
    return _with_offer(
        state, offer.model_copy(update={"shape_asked": True, "chosen_shape": chosen})
    )


def _with_chosen_combination(
    state: AgentStateV1, offer: SeatingOfferState, choice: int
) -> AgentStateV1:
    """The combination they chose, among the ones on screen, added to their picks.

    Only a combination that was actually shown: a choice past the end changes
    nothing, and the selection shown next says what they really have.
    """
    if not 1 <= choice <= len(offer.shown):
        return state
    chosen = offer.shown[choice - 1]
    picks = state.product_interaction
    selected = tuple(
        dict.fromkeys((*picks.selected_product_ids, *(line.product_id for line in chosen.lines)))
    )
    interaction = ProductInteractionState.model_validate(
        {**picks.model_dump(), "selected_product_ids": selected}
    )
    with_picks = AgentStateV1(
        customer_preferences=state.customer_preferences,
        active_search=state.active_search,
        product_interaction=interaction,
        room_project=state.room_project,
        derived_commerce=state.derived_commerce,
        seating_offer=offer.model_copy(update={"chosen": chosen}),
    )
    logger.info("seating_combination_chosen", choice=choice, pieces=len(chosen.lines))
    return with_picks


def _with_subcategory(composed: ComposedSearch, subcategory: str | None) -> ComposedSearch:
    """The same search for another type: every other requirement kept."""
    return composed.model_copy(
        update={
            "candidate": composed.candidate.model_copy(
                update={
                    "request": composed.candidate.request.model_copy(
                        update={"commerce_subcategory": subcategory}
                    )
                }
            ),
            "resolved": composed.resolved.model_copy(
                update={
                    "request": composed.resolved.request.model_copy(
                        update={"commerce_subcategory": subcategory}
                    )
                }
            ),
        }
    )


def _asks_about_the_room(decision: CustomerAgentDecision) -> bool:
    clarification = decision.clarification
    return (
        clarification is not None
        and clarification.reason is BlockingClarificationReason.MISSING_ROOM_REQUIREMENTS
    )


def _with_seating(
    others: InteriorDesignResult,
    found: DesignDiscoveryResult,
    seats: Sequence[tuple[DesignCategoryNeed, CandidatePoolResult]],
) -> tuple[InteriorDesignResult, DesignDiscoveryResult]:
    """The room's plan and candidates with its seating first.

    Seating leads the plan, as it leads a living room; the other pieces keep
    their order, one position further on."""
    entries = [
        DesignNeedCandidates(need_index=position, need=need, pool=pool)
        for position, (need, pool) in enumerate(seats)
    ]
    entries.extend(
        entry.model_copy(update={"need_index": entry.need_index + len(seats)})
        for entry in found.needs
    )
    plan = InteriorDesignResult(
        guidance=others.guidance, needs=(*(need for need, _ in seats), *others.needs)
    )
    return plan, DesignDiscoveryResult(needs=tuple(entries))


_ComposedRoom = tuple[InteriorDesignResult, DesignDiscoveryResult, BundleOptimizationOutcome]
"""A room's plan, its candidates and the room chosen from them."""

_UNBUILT = 10_000
"""A shortfall no built room can reach, so any real room is preferred."""


def _room_shortfall(
    outcome: BundleOptimizationOutcome, seat_needs: int
) -> tuple[int, int, int, int]:
    """How far a room falls short: seats first - nobody should be left standing
    - then essential, recommended and optional pieces, in that order."""
    if not isinstance(outcome, RoomBundle) or outcome.status is BundleStatus.INFEASIBLE:
        return (_UNBUILT, _UNBUILT, _UNBUILT, _UNBUILT)
    unmet = dict.fromkeys(DesignPriority, 0)
    seats = 0
    for entry in outcome.unmet:
        unmet[entry.priority] += 1
        seats += entry.need_index < seat_needs
    return (
        seats,
        unmet[DesignPriority.REQUIRED],
        unmet[DesignPriority.RECOMMENDED],
        unmet[DesignPriority.OPTIONAL],
    )


def _room_seating_wishes(room: RoomProjectState) -> SeatingRequirements:
    """The room's colour and style leanings, as wishes each seat is ranked by -
    never filters (CLAUDE.md 12.4)."""

    def wished(family: AttributeFamily) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                p.canonical_value
                for p in room.design_preferences
                if p.family is family and p.canonical_value is not None
            )
        )

    return SeatingRequirements(
        wished_colors=wished(AttributeFamily.COLOR),
        wished_styles=wished(AttributeFamily.STYLE),
    )


def _earlier_seat_count(state: AgentStateV1, template: RoomTemplate) -> int | None:
    """A head count they gave while searching for seating earlier in the chat.

    Offered for confirmation only - "is it for the nine you mentioned?" - and
    never copied into the room unasked: a sofa search is not a statement about
    the room (CLAUDE.md 10.1)."""
    seating = template.seating
    if seating is None:
        return None
    if state.seating_offer is not None:
        return state.seating_offer.target_seats
    search = state.active_search
    if search is None or search.request.commerce_subcategory not in seating.seating_types:
        return None
    capacity = search.request.seating_capacity
    return capacity.min_capacity if capacity is not None else None


def _seating_requirements(resolved: ResolvedSearch) -> SeatingRequirements:
    """Everything the customer asked beyond the seat count and budget, for
    every piece of a combination to be held to (CLAUDE.md 12.4, 13)."""
    request = resolved.request

    def wished(family: AttributeFamily) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                p.canonical_value
                for p in resolved.semantic_preferences
                if p.family is family and p.canonical_value is not None
            )
        )

    return SeatingRequirements(
        colors_any_of=request.colors_any_of,
        styles_all_of=request.styles_all_of,
        unmatchable_strict=tuple(dict.fromkeys(a.family for a in resolved.unmatched_strict)),
        wished_colors=wished(AttributeFamily.COLOR),
        wished_styles=wished(AttributeFamily.STYLE),
        asked_type=request.commerce_subcategory,
        sized_type=request.commerce_subcategory if request.dimensions else None,
        dimensions=request.dimensions,
    )


def _saved_sizes(
    decision: CustomerAgentDecision, state: AgentStateV1, subcategory: str | None
) -> SavedMeasurements | None:
    """The sizes saved for the type being searched, unless they let go of them."""
    if decision.drop_saved_sizes:
        return None
    return state.customer_preferences.measurements_for(subcategory)


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
            # The piece they just chose becomes the one under discussion.
            # Without this, "show me the one I selected" kept resolving through
            # a focus set turns earlier - so a customer who corrected their
            # choice was shown the product they had just rejected (M15 3).
            return ProductInteractionUpdate(
                selected_product_ids=AddItems(items=(product_id,)),
                focused_product_id=product_id,
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


def _search_despite(unresolved: UnresolvedStrictRequirement) -> ResolvedSearch:
    """Search anyway when a strict colour or style names no approved value.

    "Only red sofas" when the vocabulary has no red: no product can carry that
    exact value, so asking "should I show nothing?" only delays the answer the
    catalog will give. The search runs without it, their words rank the
    closest first, and `unmatched_strict` makes the result record the lifted
    requirement so the reply says plainly that nothing matched - the same last
    resort as a strict colour no product in stock carries.
    """
    request = unresolved.request
    # Colours are alternatives: "only red or beige" is satisfied by beige, so
    # red is one more alternative, kept as a preference, and not unmatched.
    # Styles are all required: "Modern and cottagecore" is NOT satisfied by a
    # Modern-only piece, so an unexpressible style stays unmatched even beside
    # an approved one, and the reply must say no exact match exists.
    filtered = {
        AttributeFamily.COLOR: bool(request.colors_any_of),
        AttributeFamily.STYLE: False,
    }
    return ResolvedSearch(
        request=request,
        semantics=unresolved.semantics,
        semantic_preferences=(
            *unresolved.semantic_preferences,
            *(
                SemanticPreference(
                    family=attribute.family,
                    raw_value=attribute.raw_value,
                    strength=ConstraintStrength.LOCKED,
                )
                for attribute in unresolved.unresolved
            ),
        ),
        semantic_text=unresolved.semantic_text,
        unmatched_strict=tuple(a for a in unresolved.unresolved if not filtered[a.family]),
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
