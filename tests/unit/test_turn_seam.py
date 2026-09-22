"""The join between the two phases: a real turn, worded by the real generator.

Both halves have thorough tests of their own, against fakes. What neither has
is proof that the object one produces is the object the other expects - they
were built a phase apart against shared contracts, and a mismatch in how a
branch is grounded would pass both suites and fail only in production.

So these use the **real** `CustomerTurnCoordinator` and the **real**
`CustomerResponseGenerator`, with fakes only where the outside world would be:
the provider and the catalog. They check the seam, not the branches.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.core.exceptions import CatalogUnavailableError
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CustomerAgentDecision,
    CustomerStateProposal,
    FollowUpPolicy,
    PriceProposal,
    ProductInteractionIntent,
    ProductInteractionOp,
)
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    ProductInteractionState,
)
from app.schemas.agent_turn import CustomerResponse, CustomerTurnInput
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import ProductSearchRequest
from app.schemas.grounding import (
    SearchExecutionGrounding,
    SearchOutcome,
)
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.query import ConstraintSemantics, ResolvedSearch
from app.schemas.relaxation import StopReason
from app.schemas.resolution import (
    ProductSearchExecutionResult,
    ReferenceFailureReason,
    ReferenceUnresolved,
    ResolvedProductReference,
)
from app.schemas.response import (
    DeterministicResponse,
    DeterministicResponseKind,
    ResponseGroundingView,
    ResponseOutcomeKind,
)
from app.schemas.retailer import RetailerContext
from app.services.bundle_reference import BundleReferenceResolver
from app.services.grounding_builder import to_grounded_product
from app.services.refinement_composer import SearchRefinementComposer
from app.services.response_generator import CustomerResponseGenerator
from app.services.response_view import route_response, valid_grounding_refs
from app.services.response_wording import (
    FAILURE_WORDING,
    FALLBACK_WORDING,
    SIDE_NOTICE_WORDING,
)
from app.services.similar_search import SimilarSearchBuilder
from app.services.turn_coordinator import CustomerTurnCoordinator
from app.taxonomy.attributes import load_catalog_attributes
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
PROSE = "Here are a few that should suit."


def _candidate(product_id: int) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Aurora {product_id}",
        name_arabic="كنبة",
        price_amount=Decimal("4299"),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


# ── fakes, only where the outside world would be ────────────────────────────


class FakeDecisions:
    def __init__(self, decision: CustomerAgentDecision) -> None:
        self.decision = decision
        self.calls = 0

    async def decide(self, decision_input: Any) -> CustomerAgentDecision:
        self.calls += 1
        return self.decision


class FakeM7:
    def __init__(self, outcome: Any = None) -> None:
        self.outcome = outcome

    async def interpret(self, message: str) -> Any:
        return self.outcome


class FakeReferences:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome

    async def resolve(self, selector: Any, state: Any, context: Any) -> Any:
        return self.outcome


class FakePipeline:
    def __init__(self, ids: tuple[int, ...] = (), error: Exception | None = None):
        self.ids, self.error = ids, error

    async def execute(self, resolved: Any, context: Any, **_: Any) -> Any:
        if self.error is not None:
            raise self.error
        return ProductSearchExecutionResult(
            presented_product_ids=self.ids,
            grounding=SearchExecutionGrounding(
                outcome=SearchOutcome.RESULTS if self.ids else SearchOutcome.ZERO_RESULTS,
                products=tuple(
                    to_grounded_product(
                        _candidate(p),
                        grounding_ref=n,
                        presented_ordinal=n,
                        relaxation_depth=0,
                    )
                    for n, p in enumerate(self.ids, start=1)
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
    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[Any, ...]:
        return tuple(_candidate(p) for p in product_ids)


class FakeResponseClient:
    def __init__(self, *replies: CustomerResponse | Exception) -> None:
        self._replies = list(replies) or [CustomerResponse(message=PROSE)]
        self.calls: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return "response-model"

    async def parse(self, *, instructions: str, user_input: str, schema: Any) -> Any:
        self.calls.append({"instructions": instructions, "user_input": user_input})
        reply = self._replies[min(len(self.calls) - 1, len(self._replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply


class _Unusable:
    """Any service the branch under test must not reach."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - a trap
        raise AssertionError(f"this branch must not call {name}")


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
    interpretation: Any = None,
    references: Any = None,
    pipeline: FakePipeline | None = None,
) -> CustomerTurnCoordinator:
    taxonomy = load_taxonomy()
    attributes = load_catalog_attributes()
    dimensions = load_dimension_semantics(taxonomy=taxonomy)
    return CustomerTurnCoordinator(
        FakeDecisions(decision),  # type: ignore[arg-type]
        FakeM7(interpretation),  # type: ignore[arg-type]
        SearchRefinementComposer(attributes, dimensions),
        references
        or FakeReferences(  # type: ignore[arg-type]
            ResolvedProductReference(product_id=1)
        ),
        _Unusable(),  # type: ignore[arg-type]
        _Unusable(),  # type: ignore[arg-type]
        pipeline or FakePipeline(ids=(10, 11)),  # type: ignore[arg-type]
        FakeHydration(),  # type: ignore[arg-type]
        SimilarSearchBuilder(taxonomy, attributes),
        FakeCapabilities(),  # type: ignore[arg-type]
        FakeDesign(),  # type: ignore[arg-type]
        FakeDesignDiscovery(),  # type: ignore[arg-type]
        BundleReferenceResolver(taxonomy),
        FakeOptimizer(),  # type: ignore[arg-type]
        dimensions,
    )


def _state(*, presented: tuple[int, ...] = (), revision: int = 0) -> AgentStateV1:
    return AgentStateV1(
        active_search=ActiveSearchState(request=SOFAS, revision=revision),
        product_interaction=ProductInteractionState(
            presented_product_ids=presented,
            presented_search_revision=revision if presented else None,
        ),
    )


async def _run(
    decision: CustomerAgentDecision,
    *,
    message: str = "show me sofas",
    state: AgentStateV1 | None = None,
    interpretation: Any = None,
    references: Any = None,
    pipeline: FakePipeline | None = None,
    replies: tuple[CustomerResponse | Exception, ...] = (),
) -> tuple[Any, CustomerResponse, FakeResponseClient]:
    """One turn, coordinated for real and then worded for real."""
    turn = CustomerTurnInput(message=message, state=state or _state(), context=CONTEXT)
    coordinator = _coordinator(
        decision,
        interpretation=interpretation,
        references=references,
        pipeline=pipeline,
    )
    result = await coordinator.run(turn)

    client = FakeResponseClient(*replies)
    response = await CustomerResponseGenerator(client).generate(turn, result)
    return result, response, client


def _resolved(request: ProductSearchRequest = RECLINERS) -> ResolvedSearch:
    return ResolvedSearch(request=request, semantics=ConstraintSemantics())


# ── the seam, branch by branch ──────────────────────────────────────────────


async def test_a_successful_search_is_grounded_then_framed() -> None:
    result, response, client = await _run(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(),
        pipeline=FakePipeline(ids=(10, 11)),
    )

    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.SEARCH_RESULTS
    assert len(client.calls) == 1
    assert response.message == PROSE
    # The committed lineage the coordinator produced is what the generator saw.
    assert result.state.product_interaction.presented_product_ids == (10, 11)
    assert valid_grounding_refs(result.grounding) == {1, 2}


async def test_a_zero_result_search_still_reaches_the_model() -> None:
    result, response, client = await _run(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(),
        pipeline=FakePipeline(ids=()),
    )

    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.ZERO_RESULTS
    assert len(client.calls) == 1
    assert response.message == PROSE, "a zero-result turn is still worded"
    assert result.grounding.failure is None, "zero results is not a failure"
    assert result.state.active_search is not None
    assert result.state.active_search.revision == 1, "still a committed search"


async def test_a_handled_search_failure_is_worded_without_a_model() -> None:
    result, response, client = await _run(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(),
        pipeline=FakePipeline(error=CatalogUnavailableError()),
        state=_state(presented=(1, 2), revision=1),
    )

    route = route_response(result)
    assert isinstance(route.primary, DeterministicResponse)
    assert route.primary.kind is DeterministicResponseKind.HANDLED_FAILURE
    assert client.calls == []
    assert response.message == FAILURE_WORDING[route.primary.failure_code]  # type: ignore[index]
    # Nothing rolled back.
    assert result.state.active_search is not None
    assert result.state.active_search.revision == 1
    assert result.state.product_interaction.presented_product_ids == (1, 2)


async def test_a_model_clarification_passes_straight_through() -> None:
    question = "Which kind of table did you mean?"
    _, response, client = await _run(
        CustomerAgentDecision(
            action=AgentAction.CLARIFY,
            clarification=BlockingClarification(
                reason=BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE,
                question=question,
            ),
            follow_up_policy=FollowUpPolicy.NONE,
        )
    )

    assert client.calls == [], "the decision model already wrote it"
    assert response.message == question
    assert response.follow_up_question is None


async def test_a_deterministic_clarification_is_worded_by_the_model() -> None:
    from app.schemas.query import ClarificationReason, ClarificationRequired

    question = CustomerResponse(message="Which kind of product did you mean?")
    result, response, client = await _run(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=ClarificationRequired(reason=ClarificationReason.NO_COMMERCE_CATEGORY),
        replies=(question,),
    )

    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION
    assert len(client.calls) == 1
    assert response.message == question.message
    assert result.grounding.deterministic_clarification is not None


async def test_a_successful_search_beside_a_failed_selection() -> None:
    """The results stand, and the notice is appended without a second call."""
    result, response, client = await _run(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT,
                reference=PresentedOrdinal(position=9),
            ),
        ),
        interpretation=_resolved(),
        references=FakeReferences(
            ReferenceUnresolved(reason=ReferenceFailureReason.PRODUCT_UNAVAILABLE)
        ),
        pipeline=FakePipeline(ids=(10, 11)),
    )

    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.SEARCH_RESULTS
    assert route.side_notice is not None
    assert len(client.calls) == 1, "the notice costs no call"
    assert response.message.startswith(PROSE)
    assert SIDE_NOTICE_WORDING[route.side_notice] in response.message
    assert result.state.product_interaction.presented_product_ids == (10, 11)


async def test_a_successful_search_beside_a_required_clarification() -> None:
    """Value first, one question, one call."""
    result, _, client = await _run(
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            state_proposal=CustomerStateProposal(room_budget=PriceProposal(max_amount="12000")),
        ),
        interpretation=_resolved(),
        pipeline=FakePipeline(ids=(10, 11)),
    )

    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.SEARCH_RESULTS
    assert route.required_clarification is not None
    assert route.primary.clarification_reason is (
        BlockingClarificationReason.MISSING_PRICE_CURRENCY
    )
    assert len(client.calls) == 1
    assert route.follow_up_allowed is False


async def test_an_answer_beside_a_failed_selection_is_still_answered() -> None:
    """No positive grounding, and still a complete piece of work."""
    result, response, client = await _run(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT,
                reference=PresentedOrdinal(position=9),
            ),
        ),
        references=FakeReferences(
            ReferenceUnresolved(reason=ReferenceFailureReason.PRODUCT_UNAVAILABLE)
        ),
    )

    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.ANSWER
    assert route.side_notice is not None
    assert len(client.calls) == 1
    assert SIDE_NOTICE_WORDING[route.side_notice] in response.message


# ── what the seam must never carry ──────────────────────────────────────────


async def test_no_backend_identity_crosses_into_the_response_model() -> None:
    """The coordinator grounded real products. Their merchandise reaches the
    response model, across the whole seam; their identity does not.

    Checked end to end rather than on the projection alone, because the
    interesting failure is a field that is safe in the view and leaks through
    the payload built around it (CLAUDE.md 6).
    """
    _, _, client = await _run(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(),
        pipeline=FakePipeline(ids=(10, 11)),
    )

    payload = client.calls[0]["user_input"]
    for forbidden in ("https://", "store_id", "product_id", "uuid", "pinecone"):
        assert forbidden not in payload, forbidden


async def test_the_visible_cards_cross_into_the_response_model() -> None:
    """The same seam, in the direction the pass opened.

    Two products were searched, hydrated and presented. The reply is written
    beside them, so the model reading it can see them - in the order and at the
    positions the customer will (CLAUDE.md 2, 7).
    """
    _, _, client = await _run(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(),
        pipeline=FakePipeline(ids=(10, 11)),
    )

    from app.schemas.response import ResponseInput

    sent = ResponseInput.model_validate_json(client.calls[0]["user_input"])
    cards = sent.grounding.screen.products

    assert [card.presented_ordinal for card in cards] == [1, 2]
    assert all(card.name for card in cards)
    assert all(card.price_amount is not None for card in cards)


async def test_the_response_layer_never_alters_the_turn_state() -> None:
    """Even when generation fails entirely."""
    result, response, client = await _run(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(),
        pipeline=FakePipeline(ids=(10, 11)),
        replies=(CatalogUnavailableError(),),
    )

    assert response.message == FALLBACK_WORDING[ResponseOutcomeKind.SEARCH_RESULTS]
    assert len(client.calls) == 1, "no retry for a provider failure"
    assert result.state.product_interaction.presented_product_ids == (10, 11)
    assert result.state.active_search is not None
    assert result.state.active_search.revision == 1


async def test_the_cards_remain_authoritative_whatever_the_prose_cites() -> None:
    result, response, _ = await _run(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(),
        pipeline=FakePipeline(ids=(10, 11)),
        replies=(
            CustomerResponse(
                message="The second one suits a smaller room.",
                referenced_grounding_refs=(2,),
            ),
        ),
    )

    assert result.grounding.search is not None
    assert len(result.grounding.search.products) == 2, "both still render"
    assert response.referenced_grounding_refs == (2,)


@pytest.mark.parametrize(
    "decision",
    [
        CustomerAgentDecision(action=AgentAction.ANSWER),
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
    ],
    ids=["answer", "design handoff"],
)
async def test_every_seam_outcome_returns_a_usable_reply(
    decision: CustomerAgentDecision,
) -> None:
    _, response, _ = await _run(decision)

    assert response.message.strip()
    assert response.follow_up_question is None or response.follow_up_question.strip()
