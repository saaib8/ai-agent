"""A question about the shop, answered from its own documents.

The decision restates the question; the coordinator looks it up in the
request's store and nowhere else; the reply is written from the passages
found - and only from them, figures included. Nothing is searched, and a
lookup that cannot run is reported, never answered from anywhere else.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.exceptions import StoreKnowledgeUnavailableError
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.grounding import TurnFailureCode
from app.schemas.response import (
    DeterministicResponse,
    DeterministicResponseKind,
    ResponseGroundingView,
    ResponseOutcomeKind,
)
from app.schemas.retailer import RetailerContext
from app.schemas.store_knowledge import KnowledgePassage, StoreKnowledgeGrounding
from app.services.numeric_guard import build_allowance, check_numeric_policy, policy_figures
from app.services.response_view import route_response

from tests.unit.test_turn_coordinator import _coordinator, _state, _turn

QUESTION = "How long does delivery to Jeddah take?"
DELIVERY = KnowledgePassage(
    title="faq.pdf", page=2, text="Delivery to Jeddah takes 4 to 7 working days."
)
SUPPORT = KnowledgePassage(
    title="faq.pdf", page=11, text="WhatsApp our support team on +966 50 000 0000."
)


class FakeStoreKnowledge:
    def __init__(
        self,
        passages: tuple[KnowledgePassage, ...] = (DELIVERY,),
        error: Exception | None = None,
    ) -> None:
        self.passages = passages
        self.error = error
        self.calls: list[tuple[RetailerContext, str]] = []

    async def lookup(self, context: RetailerContext, question: str) -> StoreKnowledgeGrounding:
        self.calls.append((context, question))
        if self.error is not None:
            raise self.error
        return StoreKnowledgeGrounding(question=question, passages=self.passages)


def _asks(question: str = QUESTION) -> CustomerAgentDecision:
    return CustomerAgentDecision(action=AgentAction.ANSWER, policy_question=question)


async def test_the_question_is_looked_up_in_the_requests_own_store() -> None:
    knowledge = FakeStoreKnowledge()
    coordinator, parts = _coordinator(_asks(), store_knowledge=knowledge)

    result = await coordinator.run(_turn(_state(), "and to Jeddah?"))

    assert knowledge.calls == [(RetailerContext(store_id=50), QUESTION)]
    assert result.grounding.store_knowledge == StoreKnowledgeGrounding(
        question=QUESTION, passages=(DELIVERY,)
    )
    assert result.grounding.failure is None
    assert parts["pipeline"].calls == [], "a question about the shop searches no products"


async def test_an_ordinary_answer_never_reaches_the_documents() -> None:
    knowledge = FakeStoreKnowledge()
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER), store_knowledge=knowledge
    )

    result = await coordinator.run(_turn(_state(), "which one do you recommend?"))

    assert knowledge.calls == []
    assert result.grounding.store_knowledge is None


async def test_the_answer_is_written_from_the_passages() -> None:
    coordinator, _ = _coordinator(_asks(), store_knowledge=FakeStoreKnowledge())

    view = route_response(await coordinator.run(_turn(_state(), QUESTION))).primary

    assert isinstance(view, ResponseGroundingView)
    assert view.kind is ResponseOutcomeKind.POLICY_ANSWER
    assert view.policy_question == QUESTION
    assert view.policy_passages == (DELIVERY,)
    assert view.presented_count == 0, "an answer about the shop shows no cards"


async def test_documents_that_say_nothing_are_still_answered_honestly() -> None:
    coordinator, _ = _coordinator(_asks(), store_knowledge=FakeStoreKnowledge(passages=()))

    view = route_response(await coordinator.run(_turn(_state(), QUESTION))).primary

    assert isinstance(view, ResponseGroundingView)
    assert view.kind is ResponseOutcomeKind.POLICY_ANSWER
    assert view.policy_passages == ()


async def test_a_lookup_that_cannot_run_is_reported_not_guessed() -> None:
    knowledge = FakeStoreKnowledge(error=StoreKnowledgeUnavailableError(reason="down"))
    coordinator, _ = _coordinator(_asks(), store_knowledge=knowledge)

    result = await coordinator.run(_turn(_state(), QUESTION))
    route = route_response(result).primary

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.STORE_INFO_UNAVAILABLE
    assert route == DeterministicResponse(
        kind=DeterministicResponseKind.HANDLED_FAILURE,
        failure_code=TurnFailureCode.STORE_INFO_UNAVAILABLE,
    )


async def test_without_store_knowledge_the_question_is_reported_unavailable() -> None:
    coordinator, _ = _coordinator(_asks())

    result = await coordinator.run(_turn(_state(), QUESTION))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.STORE_INFO_UNAVAILABLE


def test_figures_the_shop_states_are_sayable() -> None:
    allowance = build_allowance(QUESTION, figures=policy_figures((DELIVERY, SUPPORT)))

    reply = "Delivery to Jeddah takes 4 to 7 working days. WhatsApp +966 50 000 0000."

    assert check_numeric_policy(message=reply, follow_up_question=None, allowance=allowance) is None


def test_a_figure_the_shop_never_states_is_still_refused() -> None:
    allowance = build_allowance(QUESTION, figures=policy_figures((DELIVERY,)))

    reply = "Delivery to Jeddah takes 4 to 7 working days and costs 50 SAR."

    assert check_numeric_policy(message=reply, follow_up_question=None, allowance=allowance)


def test_policy_figures_are_read_lexically_and_derive_nothing() -> None:
    assert set(policy_figures((DELIVERY,))) == {Decimal(4), Decimal(7)}
