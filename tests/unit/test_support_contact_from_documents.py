"""A store with no support channels set is reached through its documents' own contact.

The PDF says "call 920000777"; the portal has no WhatsApp or email. Faysal gives
the line the store wrote rather than saying the store shared none - on a tapped
"Contact support" and beside an answer about the store alike.
"""

from __future__ import annotations

from app.core.exceptions import StoreKnowledgeUnavailableError
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.response import ResponseGroundingView, ResponseOutcomeKind
from app.schemas.retailer import RetailerContext
from app.schemas.store_knowledge import KnowledgePassage, StoreKnowledgeGrounding
from app.schemas.support import CONTACT_LOOKUP_QUESTION, ContactSupportAction, SupportContacts
from app.services.response_view import route_response

from tests.unit.test_store_info_turn import DELIVERY, QUESTION, _asks
from tests.unit.test_support_turn import CONTACTS, FakeSupport
from tests.unit.test_turn_coordinator import _coordinator, _state, _turn

CARE = KnowledgePassage(
    title="policies.pdf", page=1, text="Customer care: call 920000777 or email help@cityw.example."
)


class KnowledgeByQuestion:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    async def lookup(self, context: RetailerContext, question: str) -> StoreKnowledgeGrounding:
        self.calls.append(question)
        if self.error is not None and question == CONTACT_LOOKUP_QUESTION:
            raise self.error
        found = (CARE,) if question == CONTACT_LOOKUP_QUESTION else (DELIVERY, CARE)
        return StoreKnowledgeGrounding(question=question, passages=found)


def _tap():
    return _turn(_state(), "Contact support").model_copy(
        update={"support_action": ContactSupportAction()}
    )


async def test_a_tap_with_no_channels_carries_the_documents_contact() -> None:
    knowledge = KnowledgeByQuestion()
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        store_knowledge=knowledge,
        support=FakeSupport(SupportContacts()),
    )

    view = route_response(await coordinator.run(_tap())).primary

    assert isinstance(view, ResponseGroundingView)
    assert view.kind is ResponseOutcomeKind.SUPPORT_CONTACT
    assert view.support_offered is False
    assert view.policy_passages == (CARE,)
    assert knowledge.calls == [CONTACT_LOOKUP_QUESTION]
    assert parts["decisions"].inputs == [], "still no model reads the tap"


async def test_a_tap_with_channels_looks_nothing_up() -> None:
    knowledge = KnowledgeByQuestion()
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        store_knowledge=knowledge,
        support=FakeSupport(CONTACTS),
    )

    view = route_response(await coordinator.run(_tap())).primary

    assert isinstance(view, ResponseGroundingView)
    assert view.support_offered is True
    assert view.policy_passages == ()
    assert knowledge.calls == []


async def test_a_failed_contact_search_still_answers_the_tap() -> None:
    knowledge = KnowledgeByQuestion(error=StoreKnowledgeUnavailableError(reason="down"))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        store_knowledge=knowledge,
        support=FakeSupport(SupportContacts()),
    )

    view = route_response(await coordinator.run(_tap())).primary

    assert isinstance(view, ResponseGroundingView)
    assert view.kind is ResponseOutcomeKind.SUPPORT_CONTACT
    assert view.policy_passages == ()


async def test_a_store_answer_with_no_channels_adds_the_contact_once() -> None:
    knowledge = KnowledgeByQuestion()
    coordinator, _ = _coordinator(
        _asks(), store_knowledge=knowledge, support=FakeSupport(SupportContacts())
    )

    view = route_response(await coordinator.run(_turn(_state(), QUESTION))).primary

    assert isinstance(view, ResponseGroundingView)
    assert view.kind is ResponseOutcomeKind.POLICY_ANSWER
    assert view.policy_passages == (DELIVERY, CARE), "the answer first, no passage twice"
    assert knowledge.calls == [QUESTION, CONTACT_LOOKUP_QUESTION]


async def test_a_store_answer_with_channels_searches_only_the_question() -> None:
    knowledge = KnowledgeByQuestion()
    coordinator, _ = _coordinator(_asks(), store_knowledge=knowledge, support=FakeSupport())

    await coordinator.run(_turn(_state(), QUESTION))

    assert knowledge.calls == [QUESTION]


async def test_without_the_support_agent_nothing_extra_is_searched() -> None:
    knowledge = KnowledgeByQuestion()
    coordinator, _ = _coordinator(_asks(), store_knowledge=knowledge)

    await coordinator.run(_turn(_state(), QUESTION))

    assert knowledge.calls == [QUESTION]
