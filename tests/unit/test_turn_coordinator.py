"""One customer turn, end to end, with the provider and catalog faked.

The composer and the similar-search builder are the *real* ones - they are pure
and registry-driven, so faking them would only test the fake. Everything that
reaches a model or a database is replaced.

What these are really checking is the transaction: which state a handled
failure leaves behind, when a revision moves, what a selector means, and which
single question a turn asks.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.core.exceptions import (
    CatalogUnavailableError,
    LLMResponseInvalidError,
    LLMUnavailableError,
    RankingIntegrityError,
)
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CustomerAgentDecision,
    CustomerStateProposal,
    DerivedCommerceProposal,
    FollowUpPolicy,
    NewSearchProposal,
    PreferenceProposal,
    PreferenceProposalOp,
    PriceProposal,
    ProductInteractionIntent,
    ProductInteractionOp,
)
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    CustomerPreferenceState,
    ProductInteractionState,
    PurchaseStage,
    RoomProjectState,
)
from app.schemas.agent_turn import CustomerTurnInput, CustomerTurnResult
from app.schemas.comparison import ProductComparisonResult
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import PriceConstraint, ProductSearchRequest
from app.schemas.grounding import (
    SearchExecutionGrounding,
    SearchOutcome,
    TurnFailureCode,
)
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.product_reference import (
    FocusedProduct,
    PresentedOrdinal,
    ProductReferenceSelector,
    SoleSelectedProduct,
)
from app.schemas.query import (
    ClarificationReason,
    ClarificationRequired,
    ConstraintSemantics,
    ConstraintStrength,
    ResolvedSearch,
    SemanticPreference,
    UnresolvedStrictRequirement,
    UnsupportedDimensionRequirement,
    UnsupportedRequirement,
)
from app.schemas.refinement import (
    PriceRefinement,
    PriceRefinementOp,
    PriceRelation,
    RelativePriceRefinement,
    SearchRefinementDelta,
    SemanticIntentOp,
    SemanticIntentRefinement,
)
from app.schemas.relaxation import StopReason
from app.schemas.resolution import (
    ComparisonFailureReason,
    ComparisonUnavailable,
    ProductSearchExecutionResult,
    ReferenceFailureReason,
    ReferenceUnresolved,
    RelativePriceFailureReason,
    RelativePriceUnresolved,
    ResolvedProductReference,
    ResolvedRelativePrice,
    SearchRequirementClarificationReason,
)
from app.schemas.retailer import RetailerContext
from app.services.bundle_reference import BundleReferenceResolver
from app.services.grounding_builder import to_grounded_product
from app.services.refinement_composer import SearchRefinementComposer
from app.services.similar_search import SimilarSearchBuilder
from app.services.turn_coordinator import CustomerTurnCoordinator
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy

STOCKED_WITH_RANGE = 12
"""A capability depth that is not the thing under test.

Every capability carries how many products back it. These suites are about
which *types* a plan may use, so they give each one an unremarkable range -
enough that nothing is refused for being thin, and a number no assertion here
reads.
"""

CONTEXT = RetailerContext(store_id=50)
SOFAS = ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa")
RECLINERS = ProductSearchRequest(commerce_category="seating", commerce_subcategory="recliner")
TABLES = ProductSearchRequest(commerce_category="tables", commerce_subcategory="console")

OFF_SCREEN = 42
"""Selected but never presented - the id that makes the focus rules bite."""


# ── fakes ───────────────────────────────────────────────────────────────────


class FakeDecisions:
    def __init__(self, decision: CustomerAgentDecision, error: Exception | None = None):
        self.decision, self.error = decision, error
        self.inputs: list[Any] = []

    async def decide(self, decision_input: Any, **_: Any) -> CustomerAgentDecision:
        self.inputs.append(decision_input)
        if self.error is not None:
            raise self.error
        return self.decision


class FakeQueryUnderstanding:
    def __init__(self, outcome: Any = None, error: Exception | None = None):
        self.outcome, self.error = outcome, error
        self.messages: list[str] = []

    async def interpret(self, message: str) -> Any:
        self.messages.append(message)
        if self.error is not None:
            raise self.error
        return self.outcome


class FakeReferences:
    """Resolves by selector type, so a turn can mix resolvable and not."""

    def __init__(self, outcomes: dict[str, Any] | None = None, default: Any = None):
        self.outcomes = outcomes or {}
        self.default = default or ResolvedProductReference(product_id=OFF_SCREEN)
        self.calls: list[tuple[Any, AgentStateV1]] = []

    async def resolve(self, selector: Any, state: AgentStateV1, context: Any) -> Any:
        self.calls.append((selector, state))
        return self.outcomes.get(type(selector).__name__, self.default)


class FakeRelativePrice:
    def __init__(self, outcome: Any):
        self.outcome = outcome
        self.calls: list[tuple[Any, AgentStateV1, str | None]] = []

    async def resolve(
        self, refinement: Any, state: AgentStateV1, context: Any, *, active_currency: Any = None
    ) -> Any:
        self.calls.append((refinement, state, active_currency))
        return self.outcome


class FakeComparison:
    def __init__(self, outcome: Any):
        self.outcome = outcome
        self.calls: list[list[int]] = []

    async def compare(self, product_ids: Any, context: Any) -> Any:
        self.calls.append(list(product_ids))
        return self.outcome


class FakePipeline:
    def __init__(self, ids: tuple[int, ...] = (), error: Exception | None = None):
        self.ids, self.error = ids, error
        self.calls: list[Any] = []

    async def execute(
        self,
        resolved: Any,
        context: Any,
        *,
        dropped_constraints: Any = (),
        earlier_sizes_applied: bool = False,
    ) -> ProductSearchExecutionResult:
        self.calls.append(resolved)
        if self.error is not None:
            raise self.error
        return ProductSearchExecutionResult(
            presented_product_ids=self.ids,
            grounding=SearchExecutionGrounding(
                outcome=SearchOutcome.RESULTS if self.ids else SearchOutcome.ZERO_RESULTS,
                products=tuple(
                    to_grounded_product(
                        _product(pid),
                        grounding_ref=n,
                        presented_ordinal=n,
                        relaxation_depth=0,
                    )
                    for n, pid in enumerate(self.ids, start=1)
                ),
                eligible_count=len(self.ids),
                ranked_count=len(self.ids),
                selected_count=len(self.ids),
                presented_count=len(self.ids),
                exact_candidate_count=len(self.ids),
                stop_reason=StopReason.EXACT_SUFFICIENT,
            ),
        )


class FakeHydration:
    def __init__(self, available: tuple[int, ...] = (OFF_SCREEN,), price: str | None = None):
        self.available = available
        self.price = price
        self.calls: list[list[int]] = []

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[Any, ...]:
        self.calls.append(list(product_ids))
        return tuple(_product(p, price=self.price) for p in product_ids if p in self.available)


def _action_reads(parts: dict[str, Any], state: AgentStateV1) -> list[list[int]]:
    """Catalog reads the turn's *action* made, with the screen read removed.

    Every turn now reads the cards the customer is looking at before the
    decision model runs, so it knows which five sofas they are comparing rather
    than only that there are five (CLAUDE.md 2). That read is context - it
    happens whatever the turn goes on to do - so tests about what an action
    touched subtract it rather than counting it.

    Two such reads bracket every turn now. Before the decision, the cards the
    customer is looking at; after it, the kinds of thing they have chosen, so
    a reply can name them instead of guessing (M19 2). Both happen whatever
    the turn goes on to do, so both are subtracted.

    Matched by their exact arguments, so a genuine second read of the same
    products would still be visible.
    """
    calls = [list(call) for call in parts["hydration"].calls]
    screen = list(state.product_interaction.presented_product_ids)
    if screen and calls and calls[0] == screen:
        calls.pop(0)
    chosen = list(state.product_interaction.selected_product_ids)
    if chosen and calls and calls[-1] == chosen:
        calls.pop()
    return calls


def _product(
    product_id: int, *, subcategory: str | None = "sofa", price: str | None = None
) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Sofa {product_id}",
        name_arabic="كنبة",
        price_amount=Decimal(price or "1000"),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(category="seating", subcategory=subcategory),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


_UNSET = object()
"""Distinguishes an unspecified collaborator from one deliberately absent."""


class FakeCapabilities:
    """The retailer's stocked types, or an unreachable catalog."""

    def __init__(
        self,
        pairs: tuple[tuple[str, str | None], ...] = (("seating", "sofa"),),
        error: Exception | None = None,
    ) -> None:
        self.pairs = pairs
        self.error = error
        self.calls: list[Any] = []

    async def capabilities(self, context: Any) -> Any:
        self.calls.append(context)
        if self.error is not None:
            raise self.error
        from app.schemas.retailer import (
            RetailerCatalogCapabilities,
            RetailerCatalogCapability,
        )

        return RetailerCatalogCapabilities(
            capabilities=tuple(
                RetailerCatalogCapability(
                    commerce_category=category,
                    commerce_subcategory=subcategory,
                    active_product_count=STOCKED_WITH_RANGE,
                )
                for category, subcategory in self.pairs
            )
        )


class FakeDesign:
    """The specialist, recording exactly what it was told."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        from app.schemas.design import InteriorDesignResult

        self.result = result if result is not None else InteriorDesignResult()
        self.error = error
        self.requests: list[Any] = []

    async def plan(self, request: Any) -> Any:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


class FakeDesignDiscovery:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        from app.schemas.design_discovery import DesignDiscoveryResult

        self.result = result if result is not None else DesignDiscoveryResult()
        self.error = error
        self.calls: list[Any] = []
        self.overrides: dict[int, Any] = {}

    async def discover(
        self, request: Any, plan: Any, context: Any, *, overrides: Any = None
    ) -> Any:
        self.calls.append((request, plan, context))
        self.overrides = dict(overrides or {})
        if self.error is not None:
            raise self.error
        return self.result


class FakeOptimizer:
    def __init__(self, outcome: Any = None) -> None:
        from app.schemas.bundle import (
            BundleStatus,
            RoomBundle,
            TotalUnavailableReason,
        )

        self.outcome = (
            outcome
            if outcome is not None
            else RoomBundle(
                status=BundleStatus.COMPLETE,
                total_unavailable=TotalUnavailableReason.NO_PRICED_LINES,
            )
        )
        self.requests: list[Any] = []

    def optimize(self, request: Any) -> Any:
        self.requests.append(request)
        return self.outcome


def _coordinator(
    decision: CustomerAgentDecision,
    *,
    decision_error: Exception | None = None,
    interpretation: Any = None,
    m7_error: Exception | None = None,
    references: FakeReferences | None = None,
    relative_price: Any = None,
    comparison: Any = None,
    pipeline: FakePipeline | None = None,
    hydration: FakeHydration | None = None,
    capabilities: Any = None,
    design: Any = _UNSET,
    design_discovery: Any = None,
    optimizer: Any = None,
    decisions: Any = None,
    seating: Any = None,
) -> tuple[CustomerTurnCoordinator, dict[str, Any]]:
    taxonomy = load_taxonomy()
    attributes = load_catalog_attributes()
    dimensions = load_dimension_semantics(taxonomy=taxonomy)
    parts = {
        "decisions": decisions or FakeDecisions(decision, decision_error),
        "m7": FakeQueryUnderstanding(interpretation, m7_error),
        "references": references or FakeReferences(),
        "relative_price": relative_price or FakeRelativePrice(None),
        "comparison": comparison or FakeComparison(None),
        "pipeline": pipeline or FakePipeline(ids=(20, 21)),
        "hydration": hydration or FakeHydration(),
        "capabilities": capabilities or FakeCapabilities(),
        # Explicit None means the capability is unconfigured, which is a
        # different thing from "the caller did not care".
        "design": FakeDesign() if design is _UNSET else design,
        "design_discovery": design_discovery or FakeDesignDiscovery(),
        "optimizer": optimizer or FakeOptimizer(),
    }
    coordinator = CustomerTurnCoordinator(
        parts["decisions"],  # type: ignore[arg-type]
        parts["m7"],  # type: ignore[arg-type]
        SearchRefinementComposer(attributes, dimensions, seating),
        parts["references"],  # type: ignore[arg-type]
        parts["relative_price"],  # type: ignore[arg-type]
        parts["comparison"],  # type: ignore[arg-type]
        parts["pipeline"],  # type: ignore[arg-type]
        parts["hydration"],  # type: ignore[arg-type]
        SimilarSearchBuilder(taxonomy, attributes),
        parts["capabilities"],  # type: ignore[arg-type]
        parts["design"],  # type: ignore[arg-type]
        parts["design_discovery"],  # type: ignore[arg-type]
        BundleReferenceResolver(taxonomy),
        parts["optimizer"],  # type: ignore[arg-type]
        dimensions,
        taxonomy,
    )
    return coordinator, parts


def _state(
    *,
    request: ProductSearchRequest | None = SOFAS,
    revision: int = 1,
    presented: tuple[int, ...] = (10, 11),
    selected: tuple[int, ...] = (OFF_SCREEN,),
    focus: int | None = None,
    preferences: tuple[SemanticPreference, ...] = (),
    room: RoomProjectState | None = None,
    stage: PurchaseStage | None = None,
) -> AgentStateV1:
    from app.schemas.agent_state import DerivedCommerceState

    return AgentStateV1(
        customer_preferences=CustomerPreferenceState(semantic_preferences=preferences),
        active_search=(ActiveSearchState(request=request, revision=revision) if request else None),
        product_interaction=ProductInteractionState(
            presented_product_ids=presented,
            presented_search_revision=revision if presented and request else None,
            selected_product_ids=selected,
            focused_product_id=focus,
        ),
        room_project=room,
        derived_commerce=DerivedCommerceState(purchase_stage=stage),
    )


def _turn(state: AgentStateV1, message: str = "show me sofas") -> CustomerTurnInput:
    return CustomerTurnInput(message=message, state=state, context=CONTEXT)


def _resolved(request: ProductSearchRequest = SOFAS, **kwargs: Any) -> ResolvedSearch:
    return ResolvedSearch(request=request, semantics=ConstraintSemantics(), **kwargs)


def _preference(value: str) -> SemanticPreference:
    return SemanticPreference(
        family=AttributeFamily.STYLE,
        raw_value=value,
        canonical_value=value,
        strength=ConstraintStrength.PREFERRED,
    )


# ── projection and the decision call ────────────────────────────────────────


async def test_the_decision_sees_a_safe_projection_and_nothing_else() -> None:
    coordinator, parts = _coordinator(CustomerAgentDecision(action=AgentAction.ANSWER))

    await coordinator.run(_turn(_state(focus=10), "the second one"))

    decision_input = parts["decisions"].inputs[0]
    assert decision_input.message == "the second one"
    payload = decision_input.model_dump_json()
    for forbidden in ("store_id", "product_id", "retailer", "revision", "schema_version"):
        assert forbidden not in payload, forbidden


async def test_the_projection_counts_products_without_naming_them() -> None:
    coordinator, parts = _coordinator(CustomerAgentDecision(action=AgentAction.ANSWER))

    await coordinator.run(_turn(_state(presented=(10, 11), selected=(11, OFF_SCREEN))))

    view = parts["decisions"].inputs[0].state_view
    assert view.presented.count == 2
    assert view.presented.selected_count == 2
    # 11 is on screen at position 2; the off-screen selection has no position.
    assert view.presented.selected_ordinals == (2,)


async def test_the_current_message_stays_out_of_the_history() -> None:
    from app.schemas.conversation import (
        ConversationContext,
        ConversationMessage,
        ConversationRole,
    )

    history = ConversationContext(
        messages=(ConversationMessage(role=ConversationRole.USER, content="show me sofas"),)
    )
    coordinator, parts = _coordinator(CustomerAgentDecision(action=AgentAction.ANSWER))

    await coordinator.run(
        CustomerTurnInput(
            message="show me sofas", conversation=history, state=_state(), context=CONTEXT
        )
    )

    decision_input = parts["decisions"].inputs[0]
    assert len(decision_input.conversation.messages) == 1
    assert decision_input.message == "show me sofas"


async def test_the_decision_service_is_called_once() -> None:
    coordinator, parts = _coordinator(CustomerAgentDecision(action=AgentAction.ANSWER))

    await coordinator.run(_turn(_state()))

    assert len(parts["decisions"].inputs) == 1


async def test_a_decision_failure_propagates_and_produces_no_result() -> None:
    """No decision, nothing executed, nothing to report."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        decision_error=LLMUnavailableError(provider="openai"),
    )

    with pytest.raises(LLMUnavailableError):
        await coordinator.run(_turn(_state()))


# ── interactions ────────────────────────────────────────────────────────────


async def test_select_adds_the_resolved_product() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=1)
            ),
        ),
        references=FakeReferences(default=ResolvedProductReference(product_id=10)),
    )

    result = await coordinator.run(_turn(_state(selected=())))

    assert result.state.product_interaction.selected_product_ids == (10,)


async def test_focus_sets_the_resolved_product() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.FOCUS, reference=PresentedOrdinal(position=1)
            ),
        ),
        references=FakeReferences(default=ResolvedProductReference(product_id=10)),
    )

    result = await coordinator.run(_turn(_state()))

    assert result.state.product_interaction.focused_product_id == 10


async def test_deselecting_the_focused_off_screen_product_clears_focus() -> None:
    """The atomic case. Removing the selection removes the only thing making
    the focus resolvable, so both must move in one update."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.DESELECT, reference=SoleSelectedProduct()
            ),
        ),
        references=FakeReferences(default=ResolvedProductReference(product_id=OFF_SCREEN)),
    )

    result = await coordinator.run(
        _turn(_state(presented=(10, 11), selected=(OFF_SCREEN,), focus=OFF_SCREEN))
    )

    assert result.state.product_interaction.selected_product_ids == ()
    assert result.state.product_interaction.focused_product_id is None


async def test_deselecting_a_still_presented_product_keeps_focus() -> None:
    """Focus stays valid because the product is still on screen, so clearing
    it would drop something the customer can still see."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.DESELECT, reference=PresentedOrdinal(position=1)
            ),
        ),
        references=FakeReferences(default=ResolvedProductReference(product_id=10)),
    )

    result = await coordinator.run(_turn(_state(presented=(10, 11), selected=(10,), focus=10)))

    assert result.state.product_interaction.selected_product_ids == ()
    assert result.state.product_interaction.focused_product_id == 10


async def test_interactions_resolve_against_the_pre_turn_state() -> None:
    """ "The second one" means the second of what they are looking at now."""
    pre_turn = _state(presented=(10, 11))
    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=2)
            ),
        ),
        interpretation=_resolved(),
        references=FakeReferences(default=ResolvedProductReference(product_id=11)),
        pipeline=FakePipeline(ids=(20, 21)),
    )

    result = await coordinator.run(_turn(pre_turn))

    resolved_against = parts["references"].calls[0][1]
    assert resolved_against.product_interaction.presented_product_ids == (10, 11)
    assert result.state.product_interaction.selected_product_ids == (OFF_SCREEN, 11)
    assert result.state.product_interaction.presented_product_ids == (20, 21)


async def test_an_unresolved_interaction_does_not_stop_the_search() -> None:
    """Value first: the side effect failed, the customer's actual request did
    not."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=9)
            ),
        ),
        interpretation=_resolved(),
        references=FakeReferences(
            default=ReferenceUnresolved(reason=ReferenceFailureReason.ORDINAL_OUT_OF_RANGE)
        ),
    )

    result = await coordinator.run(_turn(_state()))

    assert result.grounding.search is not None
    assert result.state.product_interaction.selected_product_ids == (OFF_SCREEN,)
    assert result.grounding.deterministic_clarification is not None
    assert result.grounding.deterministic_clarification.reference_reason is (
        ReferenceFailureReason.ORDINAL_OUT_OF_RANGE
    )


async def test_an_unavailable_interaction_reference_is_a_failure_not_a_question() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=SoleSelectedProduct()
            ),
        ),
        references=FakeReferences(
            default=ReferenceUnresolved(reason=ReferenceFailureReason.PRODUCT_UNAVAILABLE)
        ),
    )

    result = await coordinator.run(_turn(_state()))

    assert result.grounding.deterministic_clarification is None
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.PRODUCT_UNAVAILABLE


# ── answer, clarify, design handoff ─────────────────────────────────────────


async def test_an_answer_touches_no_service() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER, follow_up_policy=FollowUpPolicy.OPTIONAL)
    )

    state = _state()
    result = await coordinator.run(_turn(state))

    assert parts["m7"].messages == []
    assert parts["pipeline"].calls == []
    assert _action_reads(parts, state) == []
    assert result.grounding.search is None
    assert result.grounding.follow_up_policy is FollowUpPolicy.OPTIONAL


async def test_a_model_clarification_is_carried_through_verbatim() -> None:
    question = BlockingClarification(
        reason=BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE,
        question="Which kind of table?",
    )
    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.CLARIFY,
            clarification=question,
            follow_up_policy=FollowUpPolicy.NONE,
        )
    )

    result = await coordinator.run(_turn(_state()))

    assert result.grounding.clarification is question
    assert result.grounding.follow_up_policy is FollowUpPolicy.NONE
    assert parts["pipeline"].calls == []


async def test_a_design_handoff_is_a_marker_only() -> None:
    coordinator, parts = _coordinator(CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF))

    state = _state()
    result = await coordinator.run(_turn(state))

    assert result.grounding.design_handoff_requested is True
    assert parts["pipeline"].calls == []
    assert _action_reads(parts, state) == []


# ── search ──────────────────────────────────────────────────────────────────


async def test_a_successful_search_promotes_commits_and_advances_once() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(RECLINERS),
        pipeline=FakePipeline(ids=(20, 21)),
    )

    result = await coordinator.run(_turn(_state(revision=1)))

    assert result.state.active_search is not None
    assert result.state.active_search.request.commerce_subcategory == "recliner"
    assert result.state.active_search.revision == 2
    assert result.state.product_interaction.presented_product_ids == (20, 21)
    assert result.state.product_interaction.presented_search_revision == 2
    assert result.state.product_interaction.focused_product_id is None
    assert result.state.product_interaction.selected_product_ids == (OFF_SCREEN,)
    assert parts["m7"].messages == ["show me sofas"]


async def test_a_zero_result_search_is_a_successful_committed_search() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(RECLINERS),
        pipeline=FakePipeline(ids=()),
    )

    result = await coordinator.run(_turn(_state(revision=1, focus=10)))

    assert result.state.active_search is not None
    assert result.state.active_search.revision == 2
    assert result.state.product_interaction.presented_product_ids == ()
    assert result.state.product_interaction.presented_search_revision == 2
    assert result.state.product_interaction.focused_product_id is None
    assert result.state.product_interaction.selected_product_ids == (OFF_SCREEN,)
    assert result.grounding.search is not None
    assert result.grounding.search.outcome is SearchOutcome.ZERO_RESULTS
    assert result.grounding.failure is None


@pytest.mark.parametrize(
    "error",
    [CatalogUnavailableError(), LLMResponseInvalidError(reason="bad")],
    ids=["catalog", "schema"],
)
async def test_a_handled_pipeline_failure_keeps_the_whole_lineage(
    error: Exception,
) -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(RECLINERS),
        pipeline=FakePipeline(error=error),
    )
    pre_turn = _state(revision=1, presented=(10, 11), focus=10)

    result = await coordinator.run(_turn(pre_turn))

    assert result.state.active_search is not None
    assert result.state.active_search.request.commerce_subcategory == "sofa"
    assert result.state.active_search.revision == 1
    assert result.state.product_interaction.presented_product_ids == (10, 11)
    assert result.state.product_interaction.presented_search_revision == 1
    assert result.state.product_interaction.focused_product_id == 10
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.SEARCH_UNAVAILABLE


async def test_a_handled_m7_failure_behaves_the_same_way() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        m7_error=LLMUnavailableError(provider="openai"),
    )

    result = await coordinator.run(_turn(_state(revision=1)))

    assert result.state.active_search is not None
    assert result.state.active_search.revision == 1
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.SEARCH_UNAVAILABLE
    assert parts["pipeline"].calls == []


@pytest.mark.parametrize(
    "error",
    [RankingIntegrityError(reason="lost"), ValueError("programmer error")],
    ids=["integrity", "unexpected"],
)
async def test_an_internal_failure_propagates_rather_than_becoming_a_result(
    error: Exception,
) -> None:
    """A defect must not be dressed as "search unavailable"."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(),
        pipeline=FakePipeline(error=error),
    )

    with pytest.raises(type(error)):
        await coordinator.run(_turn(_state()))


async def test_this_turns_proposal_seeds_this_turns_search() -> None:
    """ "I usually prefer Modern. Show me sofas." - the preference must reach
    the search that runs now, before it is persisted."""
    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            state_proposal=CustomerStateProposal(
                customer_preferences=PreferenceProposal(
                    op=PreferenceProposalOp.ADD, preferences=(_preference("Modern"),)
                )
            ),
        ),
        interpretation=_resolved(RECLINERS),
    )

    result = await coordinator.run(_turn(_state(revision=1)))

    executed = parts["pipeline"].calls[0]
    assert [p.canonical_value for p in executed.semantic_preferences] == ["Modern"]
    assert [p.canonical_value for p in result.state.customer_preferences.semantic_preferences] == [
        "Modern"
    ]


async def test_a_new_search_carries_the_proposed_semantic_intent() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            new_search=NewSearchProposal(
                semantic_intent=SemanticIntentRefinement(op=SemanticIntentOp.SET, value="cosy")
            ),
        ),
        interpretation=_resolved(RECLINERS),
    )

    result = await coordinator.run(_turn(_state()))

    assert result.state.active_search is not None
    assert result.state.active_search.semantic_intent == "cosy"


async def test_a_new_task_drops_the_previous_semantic_intent() -> None:
    """Omitting the intent would carry a previous task's wording forward."""
    state = _state(revision=1)
    assert state.active_search is not None
    with_intent = state.model_copy(
        update={
            "active_search": state.active_search.model_copy(update={"semantic_intent": "elegant"})
        }
    )
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(RECLINERS),
    )

    result = await coordinator.run(_turn(with_intent))

    assert result.state.active_search is not None
    assert result.state.active_search.semantic_intent is None


@pytest.mark.parametrize(
    ("interpretation", "expected"),
    [
        (
            ClarificationRequired(reason=ClarificationReason.NO_COMMERCE_CATEGORY),
            BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE,
        ),
        (
            ClarificationRequired(reason=ClarificationReason.MISSING_PRICE_CURRENCY),
            BlockingClarificationReason.MISSING_PRICE_CURRENCY,
        ),
        (
            ClarificationRequired(reason=ClarificationReason.MISSING_DIMENSION_UNIT),
            BlockingClarificationReason.MISSING_DIMENSION_UNIT,
        ),
    ],
    ids=["no category", "no currency", "no unit"],
)
async def test_query_understanding_clarifications_become_questions(
    interpretation: Any, expected: BlockingClarificationReason
) -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH), interpretation=interpretation
    )

    result = await coordinator.run(_turn(_state()))

    assert result.grounding.deterministic_clarification is not None
    assert result.grounding.deterministic_clarification.reason is expected
    assert parts["pipeline"].calls == []


def _unsupported_requirement() -> UnsupportedRequirement:
    from app.schemas.query import RequirementFamily

    return UnsupportedRequirement(
        request=SOFAS,
        semantics=ConstraintSemantics(),
        unsupported=(RequirementFamily.MATERIAL,),
    )


def _unsupported_dimension() -> UnsupportedDimensionRequirement:
    from app.schemas.discovery import DimensionConstraintKind
    from app.schemas.query import UnsupportedDimension
    from app.taxonomy.dimensions import DimensionRole, UnsupportedDimensionReason

    return UnsupportedDimensionRequirement(
        request=SOFAS,
        semantics=ConstraintSemantics(),
        unsupported_dimensions=(
            UnsupportedDimension(
                role=DimensionRole.LENGTH,
                kind=DimensionConstraintKind.MAX,
                max_cm=Decimal("200"),
                strength=ConstraintStrength.LOCKED,
                reason=UnsupportedDimensionReason.UNRELIABLE_AXIS_MAPPING,
            ),
        ),
    )


def _unresolved_strict() -> UnresolvedStrictRequirement:
    from app.schemas.query import UnresolvedAttribute

    return UnresolvedStrictRequirement(
        request=SOFAS,
        semantics=ConstraintSemantics(),
        unresolved=(UnresolvedAttribute(family=AttributeFamily.COLOR, raw_value="crimson"),),
    )


M7_UNSUPPORTED_CASES: dict[str, tuple[Any, SearchRequirementClarificationReason]] = {
    "unsupported requirement": (
        _unsupported_requirement,
        SearchRequirementClarificationReason.UNSUPPORTED_REQUIREMENT,
    ),
    "unsupported dimension": (
        _unsupported_dimension,
        SearchRequirementClarificationReason.UNSUPPORTED_DIMENSION_REQUIREMENT,
    ),
}
# An unresolved strict colour or style is no longer here: it searches anyway,
# recorded as a lifted requirement the reply must disclose (stage D).


@pytest.mark.parametrize(
    ("build", "expected"),
    M7_UNSUPPORTED_CASES.values(),
    ids=M7_UNSUPPORTED_CASES.keys(),
)
async def test_each_unsupported_outcome_keeps_its_own_reason(
    build: Any, expected: SearchRequirementClarificationReason
) -> None:
    """Three different things to say. Collapsing them would destroy exactly
    what the reply needs in order to explain itself."""
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH), interpretation=build()
    )

    result = await coordinator.run(_turn(_state()))

    assert parts["pipeline"].calls == [], "running it would be a false claim"
    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is expected


@pytest.mark.parametrize(
    "build", [c[0] for c in M7_UNSUPPORTED_CASES.values()], ids=M7_UNSUPPORTED_CASES
)
async def test_no_unsupported_outcome_is_an_undefined_quality_criterion(
    build: Any,
) -> None:
    """That reason is for "more premium" - a quality axis nobody defined - not
    a bucket for every requirement the catalog cannot execute."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH), interpretation=build()
    )

    result = await coordinator.run(_turn(_state()))

    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is not (BlockingClarificationReason.UNDEFINED_QUALITY_CRITERION)


@pytest.mark.parametrize(
    "build", [c[0] for c in M7_UNSUPPORTED_CASES.values()], ids=M7_UNSUPPORTED_CASES
)
async def test_an_unsupported_outcome_promotes_no_search(build: Any) -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH), interpretation=build()
    )
    pre_turn = _state(revision=1, presented=(10, 11), focus=10)

    result = await coordinator.run(_turn(pre_turn))

    assert result.state.active_search is not None
    assert result.state.active_search.request.commerce_subcategory == "sofa"
    assert result.state.active_search.revision == 1
    assert result.state.product_interaction.presented_product_ids == (10, 11)
    assert result.state.product_interaction.presented_search_revision == 1
    assert result.state.product_interaction.focused_product_id == 10
    assert result.grounding.failure is None, "a question, not a failure"
    assert result.grounding.search is None


@pytest.mark.parametrize(
    "build", [c[0] for c in M7_UNSUPPORTED_CASES.values()], ids=M7_UNSUPPORTED_CASES
)
async def test_an_unsupported_outcome_keeps_this_turns_other_work(
    build: Any,
) -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=1)
            ),
            state_proposal=CustomerStateProposal(room_type="living room"),
            commerce_proposal=DerivedCommerceProposal(purchase_stage=PurchaseStage.CONSIDERING),
        ),
        interpretation=build(),
        references=FakeReferences(default=ResolvedProductReference(product_id=10)),
    )

    result = await coordinator.run(_turn(_state(selected=())))

    assert result.state.product_interaction.selected_product_ids == (10,)
    assert result.state.room_project is not None
    assert result.state.room_project.room_type == "living room"
    assert result.state.derived_commerce.purchase_stage is PurchaseStage.CONSIDERING


async def test_a_primary_m7_question_outranks_a_proposal_question() -> None:
    """The search is what they asked for, so its question is asked first."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            state_proposal=CustomerStateProposal(room_budget=PriceProposal(max_amount="12000")),
        ),
        interpretation=_unsupported_requirement(),
    )

    result = await coordinator.run(_turn(_state()))

    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is SearchRequirementClarificationReason.UNSUPPORTED_REQUIREMENT


async def test_the_reason_comes_from_the_type_not_the_message() -> None:
    """Routing reads M7's typed outcome. The words are never re-inspected."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_unsupported_dimension(),
    )

    result = await coordinator.run(
        _turn(_state(), "I need something in solid oak, only in crimson")
    )

    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is (
        SearchRequirementClarificationReason.UNSUPPORTED_DIMENSION_REQUIREMENT
    ), "the message mentions material and colour; the outcome type decides"


# ── similar search ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reference",
    [PresentedOrdinal(position=1), FocusedProduct(), SoleSelectedProduct()],
    ids=lambda r: r.kind,
)
async def test_a_search_with_a_reference_seeds_from_the_product(
    reference: ProductReferenceSelector,
) -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, reference=reference),
        references=FakeReferences(default=ResolvedProductReference(product_id=OFF_SCREEN)),
        hydration=FakeHydration(available=(OFF_SCREEN,)),
    )

    result = await coordinator.run(_turn(_state(revision=1)))

    assert parts["m7"].messages == [], "the reference classifies it, not M7"
    executed = parts["pipeline"].calls[0]
    assert executed.request.commerce_subcategory == "sofa"
    assert executed.request.exclude_product_ids == (OFF_SCREEN,)
    assert result.state.active_search is not None
    assert result.state.active_search.revision == 2


async def test_a_similar_search_leans_on_the_reference_colour_and_style() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, reference=PresentedOrdinal(position=1))
    )

    await coordinator.run(_turn(_state()))

    executed = parts["pipeline"].calls[0]
    values = {p.canonical_value for p in executed.semantic_preferences}
    assert {"Beige", "Modern"} <= values


async def test_an_unresolved_similar_reference_stops_the_search() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, reference=PresentedOrdinal(position=9)),
        references=FakeReferences(
            default=ReferenceUnresolved(reason=ReferenceFailureReason.ORDINAL_OUT_OF_RANGE)
        ),
    )

    result = await coordinator.run(_turn(_state(revision=1)))

    assert parts["pipeline"].calls == []
    assert result.state.active_search is not None
    assert result.state.active_search.revision == 1
    assert result.grounding.deterministic_clarification is not None


async def test_a_stale_similar_reference_is_a_failure() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, reference=PresentedOrdinal(position=1)),
        hydration=FakeHydration(available=()),
    )

    result = await coordinator.run(_turn(_state()))

    assert parts["pipeline"].calls == []
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.PRODUCT_UNAVAILABLE


class UnclassifiedHydration(FakeHydration):
    """Hydration succeeds; the product simply carries no reviewed category."""

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[Any, ...]:
        self.calls.append(list(product_ids))
        return (
            _product(OFF_SCREEN).model_copy(
                update={"commerce": CommerceClassification(category=None)}
            ),
        )


async def test_an_unbuildable_seed_reports_the_search_unavailable_not_the_product() -> None:
    """The product was read from the catalog a moment ago, so it is plainly
    available. What cannot be built is a search from it.

    Calling that `PRODUCT_UNAVAILABLE` would tell the customer something untrue
    about a product still in front of them.
    """
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, reference=PresentedOrdinal(position=1)),
        hydration=UnclassifiedHydration(),
    )

    state = _state(revision=1)
    result = await coordinator.run(_turn(state))

    assert _action_reads(parts, state) == [[OFF_SCREEN]], "the product WAS read"
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.SEARCH_UNAVAILABLE


async def test_an_unbuildable_seed_falls_back_to_nothing() -> None:
    """No generic search and no M7: guessing a category from the product's
    name is exactly what this service must not do (CLAUDE.md 6.1)."""
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, reference=PresentedOrdinal(position=1)),
        hydration=UnclassifiedHydration(),
    )

    result = await coordinator.run(_turn(_state(revision=1, presented=(10, 11))))

    assert parts["pipeline"].calls == []
    assert parts["m7"].messages == []
    assert result.state.active_search is not None
    assert result.state.active_search.request.commerce_subcategory == "sofa"
    assert result.state.active_search.revision == 1
    assert result.state.product_interaction.presented_product_ids == (10, 11)


async def test_the_two_similar_search_failures_are_told_apart() -> None:
    """The distinction this closure exists for, asserted side by side."""
    decision = CustomerAgentDecision(
        action=AgentAction.SEARCH, reference=PresentedOrdinal(position=1)
    )

    gone, _ = _coordinator(decision, hydration=FakeHydration(available=()))
    unbuildable, _ = _coordinator(decision, hydration=UnclassifiedHydration())

    product_gone = await gone.run(_turn(_state()))
    seed_unbuildable = await unbuildable.run(_turn(_state()))

    assert product_gone.grounding.failure is not None
    assert product_gone.grounding.failure.code is TurnFailureCode.PRODUCT_UNAVAILABLE
    assert seed_unbuildable.grounding.failure is not None
    assert seed_unbuildable.grounding.failure.code is (TurnFailureCode.SEARCH_UNAVAILABLE)


async def test_an_unbuildable_seed_keeps_this_turns_other_work() -> None:
    """Handled-failure semantics: the proposals and the applied interaction
    the customer also made this turn are not thrown away with the search."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            reference=PresentedOrdinal(position=1),
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=FocusedProduct()
            ),
            state_proposal=CustomerStateProposal(room_type="living room"),
            commerce_proposal=DerivedCommerceProposal(purchase_stage=PurchaseStage.CONSIDERING),
        ),
        references=FakeReferences(
            outcomes={"FocusedProduct": ResolvedProductReference(product_id=10)},
            default=ResolvedProductReference(product_id=OFF_SCREEN),
        ),
        hydration=UnclassifiedHydration(),
    )

    result = await coordinator.run(_turn(_state(selected=(), focus=10)))

    assert result.state.product_interaction.selected_product_ids == (10,)
    assert result.state.room_project is not None
    assert result.state.room_project.room_type == "living room"
    assert result.state.derived_commerce.purchase_stage is PurchaseStage.CONSIDERING


# ── refinement ──────────────────────────────────────────────────────────────


async def test_a_refinement_does_not_consult_query_understanding() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            refinement=SearchRefinementDelta(
                price=PriceRefinement(op=PriceRefinementOp.SET, max_amount="3000", currency="SAR")
            ),
        )
    )

    result = await coordinator.run(_turn(_state(revision=1)))

    assert parts["m7"].messages == []
    assert result.state.active_search is not None
    assert result.state.active_search.request.price is not None
    assert result.state.active_search.request.price.max_amount == Decimal("3000")
    assert result.state.active_search.revision == 2


async def test_a_relative_price_is_resolved_before_composition() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            refinement=SearchRefinementDelta(
                price=PriceRefinement(
                    op=PriceRefinementOp.SET_RELATIVE,
                    relative=RelativePriceRefinement(
                        relation=PriceRelation.PERCENT_CHEAPER,
                        reference=PresentedOrdinal(position=1),
                        percent="20",
                    ),
                )
            ),
        ),
        relative_price=FakeRelativePrice(
            ResolvedRelativePrice(
                price=PriceRefinement(op=PriceRefinementOp.SET, max_amount="800", currency="SAR"),
                reference_product_id=10,
                reference_price_amount="1000",
                reference_price_unit="SAR",
            )
        ),
    )
    state = _state(
        request=SOFAS.model_copy(
            update={"price": PriceConstraint(currency="SAR", max_amount=Decimal("5000"))}
        )
    )

    result = await coordinator.run(_turn(state))

    assert parts["relative_price"].calls[0][2] == "SAR", "active currency passed"
    assert result.state.active_search is not None
    assert result.state.active_search.request.price is not None
    assert result.state.active_search.request.price.max_amount == Decimal("800")


async def test_a_relative_reference_resolves_against_the_pre_turn_state() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            refinement=SearchRefinementDelta(
                price=PriceRefinement(
                    op=PriceRefinementOp.SET_RELATIVE,
                    relative=RelativePriceRefinement(
                        relation=PriceRelation.CHEAPER_THAN,
                        reference=PresentedOrdinal(position=1),
                    ),
                )
            ),
        ),
        relative_price=FakeRelativePrice(
            RelativePriceUnresolved(
                reason=RelativePriceFailureReason.REFERENCE_UNRESOLVED,
                reference_reason=ReferenceFailureReason.TIED_EXTREMUM,
            )
        ),
    )
    pre_turn = _state(presented=(10, 11))

    result = await coordinator.run(_turn(pre_turn))

    seen = parts["relative_price"].calls[0][1]
    assert seen.product_interaction.presented_product_ids == (10, 11)
    assert result.grounding.deterministic_clarification is not None
    assert result.grounding.deterministic_clarification.reference_reason is (
        ReferenceFailureReason.TIED_EXTREMUM
    )
    assert parts["pipeline"].calls == []


async def test_a_currency_conflict_is_a_question_not_a_conversion() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            refinement=SearchRefinementDelta(
                price=PriceRefinement(
                    op=PriceRefinementOp.SET_RELATIVE,
                    relative=RelativePriceRefinement(
                        relation=PriceRelation.CHEAPER_THAN,
                        reference=PresentedOrdinal(position=1),
                    ),
                )
            ),
        ),
        relative_price=FakeRelativePrice(
            RelativePriceUnresolved(reason=RelativePriceFailureReason.CURRENCY_CONFLICT)
        ),
    )

    result = await coordinator.run(_turn(_state()))

    assert parts["pipeline"].calls == []
    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.relative_price_reason is (RelativePriceFailureReason.CURRENCY_CONFLICT)


async def test_a_refinement_with_no_active_search_asks_rather_than_fails() -> None:
    """Covered in full further down, where the reasoning it replaces is set
    out. Kept here so the refinement section reads completely: adjusting a
    search that was never run is a question, not a failed turn (M21 1)."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            refinement=SearchRefinementDelta(
                price=PriceRefinement(op=PriceRefinementOp.SET, max_amount="3000", currency="SAR")
            ),
        )
    )

    result = await coordinator.run(_turn(_state(request=None, presented=(), selected=())))

    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is BlockingClarificationReason.NO_SEARCH_TO_REFINE


async def test_a_same_parent_taxonomy_change_refines_in_place() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.REFINE_SEARCH, taxonomy_change_requested=True),
        interpretation=_resolved(RECLINERS),
    )

    result = await coordinator.run(_turn(_state(revision=1), "make them recliners"))

    assert parts["m7"].messages == ["make them recliners"]
    assert result.state.active_search is not None
    assert result.state.active_search.request.commerce_category == "seating"
    assert result.state.active_search.request.commerce_subcategory == "recliner"
    assert result.state.active_search.revision == 2


async def test_a_taxonomy_change_takes_only_the_category_pair_from_m7() -> None:
    """M7's price, wording and sort must not leak into a refinement made only
    to change the product type."""
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.REFINE_SEARCH, taxonomy_change_requested=True),
        interpretation=_resolved(
            RECLINERS.model_copy(
                update={"price": PriceConstraint(currency="SAR", max_amount=Decimal("99999"))}
            ),
            semantic_text="something dramatic",
        ),
    )

    result = await coordinator.run(_turn(_state(revision=1), "make them recliners"))

    executed = parts["pipeline"].calls[0]
    assert executed.request.price is None, "M7's price must not leak"
    assert executed.semantic_text is None, "M7's wording must not leak"
    assert result.state.active_search is not None
    assert result.state.active_search.semantic_intent is None


async def test_a_different_parent_taxonomy_change_becomes_a_new_task() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.REFINE_SEARCH, taxonomy_change_requested=True),
        interpretation=_resolved(TABLES),
    )
    state = _state(
        request=SOFAS.model_copy(
            update={"price": PriceConstraint(currency="SAR", max_amount=Decimal("5000"))}
        ),
        revision=1,
    )

    result = await coordinator.run(_turn(state, "now show me consoles"))

    executed = parts["pipeline"].calls[0]
    assert executed.request.commerce_category == "tables"
    assert executed.request.price is None, "the old task's budget does not carry over"
    assert result.state.active_search is not None
    assert result.state.active_search.revision == 2


# ── product detail ──────────────────────────────────────────────────────────


async def test_a_product_detail_hydrates_and_focuses() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.PRODUCT_DETAIL, reference=PresentedOrdinal(position=1)
        ),
        references=FakeReferences(default=ResolvedProductReference(product_id=10)),
        hydration=FakeHydration(available=(10,)),
    )

    state = _state()
    result = await coordinator.run(_turn(state))

    assert _action_reads(parts, state) == [[10]]
    assert result.grounding.product_detail is not None
    assert result.state.product_interaction.focused_product_id == 10


async def test_detail_grounding_invents_no_provenance() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.PRODUCT_DETAIL, reference=PresentedOrdinal(position=1)
        ),
        references=FakeReferences(default=ResolvedProductReference(product_id=10)),
        hydration=FakeHydration(available=(10,)),
    )

    result = await coordinator.run(_turn(_state()))

    detail = result.grounding.product_detail
    assert detail is not None
    assert detail.grounding_ref == 1
    assert detail.presented_ordinal is None
    assert detail.relaxation_depth is None
    assert detail.matched_exactly is None


async def test_a_detail_can_name_an_off_screen_selected_product() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.PRODUCT_DETAIL, reference=SoleSelectedProduct()),
        hydration=FakeHydration(available=(OFF_SCREEN,)),
    )

    result = await coordinator.run(_turn(_state(selected=(OFF_SCREEN,))))

    assert result.state.product_interaction.focused_product_id == OFF_SCREEN


async def test_the_detail_focus_wins_over_an_optional_focus() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.PRODUCT_DETAIL,
            reference=SoleSelectedProduct(),
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.FOCUS, reference=PresentedOrdinal(position=1)
            ),
        ),
        references=FakeReferences(
            outcomes={"PresentedOrdinal": ResolvedProductReference(product_id=10)},
            default=ResolvedProductReference(product_id=OFF_SCREEN),
        ),
        hydration=FakeHydration(available=(OFF_SCREEN,)),
    )

    result = await coordinator.run(_turn(_state()))

    assert result.state.product_interaction.focused_product_id == OFF_SCREEN


async def test_a_detail_reference_resolves_against_the_pre_turn_state() -> None:
    """A selection made this turn must not change what "the one I selected"
    meant when the customer said it."""
    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.PRODUCT_DETAIL,
            reference=SoleSelectedProduct(),
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=1)
            ),
        ),
        references=FakeReferences(
            outcomes={"PresentedOrdinal": ResolvedProductReference(product_id=10)},
            default=ResolvedProductReference(product_id=OFF_SCREEN),
        ),
        hydration=FakeHydration(available=(OFF_SCREEN,)),
    )

    await coordinator.run(_turn(_state(selected=(OFF_SCREEN,))))

    _, detail_state = parts["references"].calls[1]
    assert detail_state.product_interaction.selected_product_ids == (OFF_SCREEN,), (
        "the detail saw the pre-turn selection, not the one just added"
    )


async def test_comparison_references_resolve_against_the_pre_turn_state() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.COMPARE,
            comparison_references=(
                PresentedOrdinal(position=1),
                PresentedOrdinal(position=2),
            ),
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=FocusedProduct()
            ),
        ),
        references=FakeReferences(
            outcomes={"FocusedProduct": ResolvedProductReference(product_id=11)},
            default=ResolvedProductReference(product_id=10),
        ),
        comparison=FakeComparison(
            ComparisonUnavailable(
                reason=ComparisonFailureReason.DUPLICATE_PRODUCT,
                requested_count=2,
                allowed_maximum=3,
            )
        ),
    )

    await coordinator.run(_turn(_state(selected=(), focus=10)))

    for _, seen in parts["references"].calls[1:]:
        assert seen.product_interaction.selected_product_ids == ()


async def test_a_stale_detail_product_is_not_focused() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.PRODUCT_DETAIL, reference=PresentedOrdinal(position=1)
        ),
        references=FakeReferences(default=ResolvedProductReference(product_id=10)),
        hydration=FakeHydration(available=()),
    )

    result = await coordinator.run(_turn(_state(focus=None)))

    assert result.state.product_interaction.focused_product_id is None
    assert result.grounding.product_detail is None
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.PRODUCT_UNAVAILABLE


# ── comparison ──────────────────────────────────────────────────────────────


async def test_a_comparison_resolves_every_reference_in_order() -> None:
    comparison = ProductComparisonResult(
        products=(
            to_grounded_product(_product(10), grounding_ref=1),
            to_grounded_product(_product(11), grounding_ref=2),
        )
    )
    counter = iter([10, 11])

    class OrderedReferences(FakeReferences):
        async def resolve(self, selector: Any, state: Any, context: Any) -> Any:
            self.calls.append((selector, state))
            return ResolvedProductReference(product_id=next(counter))

    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.COMPARE,
            comparison_references=(
                PresentedOrdinal(position=1),
                PresentedOrdinal(position=2),
            ),
        ),
        references=OrderedReferences(),
        comparison=FakeComparison(comparison),
    )

    result = await coordinator.run(_turn(_state()))

    assert parts["comparison"].calls == [[10, 11]]
    assert result.grounding.comparison is comparison


async def test_one_unresolved_reference_stops_the_whole_comparison() -> None:
    outcomes = iter(
        [
            ResolvedProductReference(product_id=10),
            ReferenceUnresolved(reason=ReferenceFailureReason.SEVERAL_ATTRIBUTE_MATCHES),
        ]
    )

    class MixedReferences(FakeReferences):
        async def resolve(self, selector: Any, state: Any, context: Any) -> Any:
            self.calls.append((selector, state))
            return next(outcomes)

    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.COMPARE,
            comparison_references=(
                PresentedOrdinal(position=1),
                PresentedOrdinal(position=2),
            ),
        ),
        references=MixedReferences(),
    )

    result = await coordinator.run(_turn(_state()))

    assert parts["comparison"].calls == [], "no partial comparison"
    assert result.grounding.deterministic_clarification is not None
    assert result.grounding.deterministic_clarification.reason is (
        BlockingClarificationReason.AMBIGUOUS_COMPARATIVE_REFERENCE
    )


@pytest.mark.parametrize(
    ("reason", "asks"),
    [
        (ComparisonFailureReason.TOO_MANY_PRODUCTS, True),
        (ComparisonFailureReason.DUPLICATE_PRODUCT, True),
        (ComparisonFailureReason.PRODUCT_UNAVAILABLE, False),
    ],
)
async def test_the_comparison_service_owns_its_own_policy(
    reason: ComparisonFailureReason, asks: bool
) -> None:
    """The coordinator pre-checks nothing: too many, repeated or unreadable are
    all the service's to decide."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.COMPARE,
            comparison_references=(
                PresentedOrdinal(position=1),
                PresentedOrdinal(position=2),
            ),
        ),
        comparison=FakeComparison(
            ComparisonUnavailable(reason=reason, requested_count=2, allowed_maximum=3)
        ),
    )

    result = await coordinator.run(_turn(_state()))

    assert (result.grounding.deterministic_clarification is not None) is asks
    assert (result.grounding.failure is not None) is not asks


# ── proposals ───────────────────────────────────────────────────────────────


async def test_a_room_budget_without_a_currency_is_withheld_and_asked_about() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            state_proposal=CustomerStateProposal(
                room_type="living room", room_budget=PriceProposal(max_amount="12000")
            ),
        )
    )

    result = await coordinator.run(_turn(_state()))

    assert result.state.room_project is not None
    assert result.state.room_project.room_type == "living room"
    assert result.state.room_project.budget is None
    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is BlockingClarificationReason.MISSING_PRICE_CURRENCY


async def test_a_room_budget_with_a_currency_persists() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            state_proposal=CustomerStateProposal(
                room_budget=PriceProposal(max_amount="12000", currency="SAR")
            ),
        )
    )

    result = await coordinator.run(_turn(_state()))

    assert result.state.room_project is not None
    assert result.state.room_project.budget is not None
    assert result.state.room_project.budget.max_amount == Decimal("12000")
    assert result.grounding.deterministic_clarification is None


@pytest.mark.parametrize(
    ("proposal", "check"),
    [
        (
            DerivedCommerceProposal(purchase_stage=PurchaseStage.HIGH_PURCHASE_INTENT),
            PurchaseStage.HIGH_PURCHASE_INTENT,
        ),
        (DerivedCommerceProposal(clear_purchase_stage=True), None),
    ],
    ids=["set", "clear"],
)
async def test_the_derived_commerce_proposal_maps_straight_through(
    proposal: DerivedCommerceProposal, check: PurchaseStage | None
) -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER, commerce_proposal=proposal)
    )

    result = await coordinator.run(_turn(_state(stage=PurchaseStage.EXPLORING)))

    assert result.state.derived_commerce.purchase_stage is check


async def test_a_room_type_can_be_cleared() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            state_proposal=CustomerStateProposal(clear_room_type=True),
        )
    )

    result = await coordinator.run(_turn(_state(room=RoomProjectState(room_type="bedroom"))))

    assert result.state.room_project is not None
    assert result.state.room_project.room_type is None


async def test_proposals_apply_on_a_clarify_turn() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.CLARIFY,
            clarification=BlockingClarification(
                reason=BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE,
                question="Which kind?",
            ),
            follow_up_policy=FollowUpPolicy.NONE,
            commerce_proposal=DerivedCommerceProposal(purchase_stage=PurchaseStage.CONSIDERING),
        )
    )

    result = await coordinator.run(_turn(_state()))

    assert result.state.derived_commerce.purchase_stage is PurchaseStage.CONSIDERING
    assert result.grounding.clarification is not None


async def test_proposals_survive_a_handled_search_failure() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            state_proposal=CustomerStateProposal(room_type="living room"),
            commerce_proposal=DerivedCommerceProposal(purchase_stage=PurchaseStage.CONSIDERING),
        ),
        interpretation=_resolved(),
        pipeline=FakePipeline(error=CatalogUnavailableError()),
    )

    result = await coordinator.run(_turn(_state(revision=1)))

    assert result.state.room_project is not None
    assert result.state.room_project.room_type == "living room"
    assert result.state.derived_commerce.purchase_stage is PurchaseStage.CONSIDERING
    assert result.state.active_search is not None
    assert result.state.active_search.revision == 1


async def test_proposals_survive_a_handled_m7_failure() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            commerce_proposal=DerivedCommerceProposal(purchase_stage=PurchaseStage.CONSIDERING),
        ),
        m7_error=LLMUnavailableError(provider="openai"),
    )

    result = await coordinator.run(_turn(_state()))

    assert result.state.derived_commerce.purchase_stage is PurchaseStage.CONSIDERING


async def test_a_successful_search_keeps_its_results_when_proposals_apply() -> None:
    """The proposals are applied to what the action produced, not to the
    pre-turn state - rebuilding from the start would undo the search."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            state_proposal=CustomerStateProposal(room_type="living room"),
        ),
        interpretation=_resolved(RECLINERS),
        pipeline=FakePipeline(ids=(20, 21)),
    )

    result = await coordinator.run(_turn(_state(revision=1)))

    assert result.state.product_interaction.presented_product_ids == (20, 21)
    assert result.state.active_search is not None
    assert result.state.active_search.revision == 2
    assert result.state.room_project is not None
    assert result.state.room_project.room_type == "living room"


# ── question priority ───────────────────────────────────────────────────────


async def test_a_model_clarification_outranks_a_proposal_question() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.CLARIFY,
            clarification=BlockingClarification(
                reason=BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE,
                question="Which kind?",
            ),
            follow_up_policy=FollowUpPolicy.NONE,
            state_proposal=CustomerStateProposal(room_budget=PriceProposal(max_amount="12000")),
        )
    )

    result = await coordinator.run(_turn(_state()))

    assert result.grounding.clarification is not None
    assert result.grounding.deterministic_clarification is None
    assert result.grounding.follow_up_policy is FollowUpPolicy.NONE


async def test_a_primary_question_outranks_a_proposal_question() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            state_proposal=CustomerStateProposal(room_budget=PriceProposal(max_amount="12000")),
        ),
        interpretation=ClarificationRequired(reason=ClarificationReason.MULTIPLE_PRODUCT_TYPES),
    )

    result = await coordinator.run(_turn(_state()))

    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is BlockingClarificationReason.MULTIPLE_PRODUCT_TYPES


async def test_a_proposal_question_outranks_an_interaction_question() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=9)
            ),
            state_proposal=CustomerStateProposal(room_budget=PriceProposal(max_amount="12000")),
        ),
        references=FakeReferences(
            default=ReferenceUnresolved(reason=ReferenceFailureReason.ORDINAL_OUT_OF_RANGE)
        ),
    )

    result = await coordinator.run(_turn(_state()))

    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is BlockingClarificationReason.MISSING_PRICE_CURRENCY


async def test_a_primary_failure_suppresses_every_question() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=9)
            ),
            state_proposal=CustomerStateProposal(room_budget=PriceProposal(max_amount="12000")),
            follow_up_policy=FollowUpPolicy.OPTIONAL,
        ),
        interpretation=_resolved(),
        references=FakeReferences(
            default=ReferenceUnresolved(reason=ReferenceFailureReason.ORDINAL_OUT_OF_RANGE)
        ),
        pipeline=FakePipeline(error=CatalogUnavailableError()),
    )

    result = await coordinator.run(_turn(_state()))

    assert result.grounding.failure is not None
    assert result.grounding.clarification is None
    assert result.grounding.deterministic_clarification is None
    assert result.grounding.follow_up_policy is FollowUpPolicy.NONE


async def test_an_ordinary_turn_keeps_the_decisions_follow_up_policy() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, follow_up_policy=FollowUpPolicy.OPTIONAL),
        interpretation=_resolved(),
    )

    result = await coordinator.run(_turn(_state()))

    assert result.grounding.follow_up_policy is FollowUpPolicy.OPTIONAL


# ── result invariants ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "decision",
    [
        CustomerAgentDecision(action=AgentAction.ANSWER),
        CustomerAgentDecision(action=AgentAction.SEARCH),
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
    ],
    ids=["answer", "search", "handoff"],
)
async def test_every_handled_turn_returns_a_valid_state(
    decision: CustomerAgentDecision,
) -> None:
    coordinator, _ = _coordinator(decision, interpretation=_resolved())

    result = await coordinator.run(_turn(_state()))

    assert isinstance(result, CustomerTurnResult)
    assert result.decision is decision
    # Re-validated through the real constructor: a state that cannot exist
    # would have raised on the way out, not here.
    assert AgentStateV1.model_validate(result.state.model_dump()) == result.state


async def test_no_intermediate_state_escapes_a_failed_search() -> None:
    """New criteria beside the old presentation validates structurally, so the
    only proof that it never escapes is checking what comes back."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(RECLINERS),
        pipeline=FakePipeline(error=CatalogUnavailableError()),
    )

    result = await coordinator.run(_turn(_state(revision=1, presented=(10, 11))))

    assert result.state.active_search is not None
    assert result.state.active_search.request.commerce_subcategory == "sofa"
    assert result.state.product_interaction.presented_product_ids == (10, 11)


async def test_the_returned_state_is_never_the_one_passed_in() -> None:
    """Immutability in practice: the caller's state is untouched."""
    pre_turn = _state(revision=1)
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(RECLINERS),
    )

    result = await coordinator.run(_turn(pre_turn))

    assert pre_turn.active_search is not None
    assert pre_turn.active_search.request.commerce_subcategory == "sofa"
    assert pre_turn.active_search.revision == 1
    assert result.state is not pre_turn


# ── composition defects are not questions ───────────────────────────────────
#
# Each of the four means the decision did not match the state the model was
# shown, or that our own sequencing went wrong. None is answerable by the
# customer, so none becomes a clarification - they take the existing
# "the model returned something unusable" boundary instead.


def test_every_composition_defect_is_accounted_for() -> None:
    """A fifth defect must be classified deliberately, not inherit a mapping."""
    from app.schemas.composition import CompositionDefect

    assert {d.name for d in CompositionDefect} == {
        "NO_ACTIVE_SEARCH",
        "RELATIVE_PRICE_NOT_RESOLVED",
        "UNAPPROVED_ATTRIBUTE_VALUE",
        "MALFORMED_AMOUNT",
        # Corrected like an unapproved value: a one-seat piece is its own type.
        "ONE_SEAT_ON_MULTI_SEAT_TYPE",
    }


async def test_a_refinement_with_no_active_search_becomes_a_question() -> None:
    """This used to raise, on the reasoning that the customer "were never the
    reason this failed, and asking them would hide the mismatch".

    A live conversation disproved the premise. "I want a sofa less than 200 cm"
    names no measurement, so the turn is a blocking clarification - which asks
    *and runs no search*. The customer answers "the width along the wall", the
    model calls that a refinement, and there is nothing active to refine. They
    were the reason, they were answering our own question, and they got "we
    could not interpret that request" (M21 1).

    So this one defect becomes a question. The mismatch is still visible - it
    is logged - but a customer-facing 5xx is the wrong way to surface it.
    """
    coordinator, parts = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            refinement=SearchRefinementDelta(
                price=PriceRefinement(op=PriceRefinementOp.SET, max_amount="3000", currency="SAR")
            ),
        )
    )

    result = await coordinator.run(_turn(_state(request=None, presented=(), selected=())))

    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is BlockingClarificationReason.NO_SEARCH_TO_REFINE
    assert parts["pipeline"].calls == [], "nothing was searched for"


async def test_the_other_composition_defects_still_raise() -> None:
    """Only `NO_ACTIVE_SEARCH` was reclassified. An unapproved attribute value
    and an unresolved relative price are still internal mismatches, and still
    not questions anyone can put to a customer."""
    from app.schemas.composition import CompositionDefect

    assert {d.value for d in CompositionDefect} >= {
        "no_active_search",
        "relative_price_not_resolved",
        "unapproved_attribute_value",
    }


async def test_an_unapproved_attribute_value_is_a_defect() -> None:
    """The schema called it canonical; only the registry decides whether it is
    (CLAUDE.md 14.3). An unapproved value is refused, never filtered on."""
    from app.schemas.refinement import (
        AttributeRefinement,
        AttributeRefinementOp,
        ProposedAttributeValue,
    )

    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            refinement=SearchRefinementDelta(
                attributes=(
                    AttributeRefinement(
                        op=AttributeRefinementOp.SET_REQUIREMENT,
                        family=AttributeFamily.COLOR,
                        values=(
                            ProposedAttributeValue(raw_value="crimson", canonical_value="Crimson"),
                        ),
                    ),
                )
            ),
        )
    )

    state = _state()
    result = await coordinator.run(_turn(state))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.REQUEST_NOT_UNDERSTOOD
    assert result.state == state, "never filtered on, and nothing else changed"


async def test_a_malformed_amount_is_a_defect() -> None:
    """A number that is not one. The customer said a figure; our failure to
    read what the model produced is not theirs to fix."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            refinement=SearchRefinementDelta(
                price=PriceRefinement(op=PriceRefinementOp.SET, max_amount="cheap", currency="SAR")
            ),
        )
    )

    state = _state()
    result = await coordinator.run(_turn(state))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.REQUEST_NOT_UNDERSTOOD
    assert result.state == state


async def test_a_defect_never_becomes_a_clarification_or_a_failure() -> None:
    """A defect is recovered as "not understood", never dressed as a question.

    The customer is not asked to fix our misreading, and nothing the turn
    started is kept: the state is exactly what they had before."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            refinement=SearchRefinementDelta(
                price=PriceRefinement(op=PriceRefinementOp.SET, max_amount="cheap", currency="SAR")
            ),
        )
    )

    state = _state()
    result = await coordinator.run(_turn(state))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.REQUEST_NOT_UNDERSTOOD
    assert result.grounding.clarification is None
    assert result.grounding.deterministic_clarification is None
    assert result.state == state
