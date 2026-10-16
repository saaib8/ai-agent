"""Faysal's side of a turn: the store's own support, offered beside the reply.

Every turn about the store carries the store's support - with an answer from
its documents, and in place of one that could not be looked up - and a tapped
"Contact support" reaches it with no model involved. The contacts are the
scoped store's own; nothing here lets a model name or write one.
"""

from __future__ import annotations

from app.core.exceptions import StoreKnowledgeUnavailableError
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.agent_turn import CustomerResponse
from app.schemas.grounding import TurnFailureCode
from app.schemas.response import ResponseGroundingView, ResponseOutcomeKind
from app.schemas.retailer import RetailerContext
from app.schemas.support import ContactSupportAction, SupportContacts, SupportGrounding
from app.services.chat_runtime import ChatRuntime
from app.services.response_generator import _ends_on_a_question
from app.services.response_view import route_response

from tests.unit.test_store_info_turn import QUESTION, FakeStoreKnowledge, _asks
from tests.unit.test_turn_coordinator import _coordinator, _state, _turn

CONTACTS = SupportContacts(email="support@store.sa", whatsapp="966551234567")


class FakeSupport:
    def __init__(self, contacts: SupportContacts = CONTACTS, *, handover: bool = False) -> None:
        self.contacts = contacts
        self.handover = handover
        self.calls: list[tuple[RetailerContext, bool]] = []

    async def offer(self, context: RetailerContext, *, requested: bool = False) -> SupportGrounding:
        self.calls.append((context, requested))
        return SupportGrounding(contacts=self.contacts, requested=requested, handover=self.handover)


async def test_an_answer_about_the_store_carries_its_support() -> None:
    support = FakeSupport()
    coordinator, _ = _coordinator(_asks(), store_knowledge=FakeStoreKnowledge(), support=support)

    result = await coordinator.run(_turn(_state(), QUESTION))

    assert result.grounding.store_knowledge is not None
    assert result.grounding.support == SupportGrounding(contacts=CONTACTS)
    assert support.calls == [(RetailerContext(store_id=50), False)]


async def test_an_outage_points_to_the_stores_support_when_it_has_some() -> None:
    knowledge = FakeStoreKnowledge(error=StoreKnowledgeUnavailableError(reason="down"))
    coordinator, _ = _coordinator(_asks(), store_knowledge=knowledge, support=FakeSupport())

    result = await coordinator.run(_turn(_state(), QUESTION))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.STORE_INFO_UNAVAILABLE_WITH_SUPPORT
    assert result.grounding.support is not None


async def test_an_outage_without_contacts_keeps_the_plain_wording() -> None:
    knowledge = FakeStoreKnowledge(error=StoreKnowledgeUnavailableError(reason="down"))
    support = FakeSupport(SupportContacts())
    coordinator, _ = _coordinator(_asks(), store_knowledge=knowledge, support=support)

    result = await coordinator.run(_turn(_state(), QUESTION))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.STORE_INFO_UNAVAILABLE


async def test_contact_support_reaches_the_store_without_a_model() -> None:
    support = FakeSupport()
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER), support=support
    )
    tapped = _turn(_state(), "Contact support").model_copy(
        update={"support_action": ContactSupportAction()}
    )

    result = await coordinator.run(tapped)

    assert result.grounding.support == SupportGrounding(contacts=CONTACTS, requested=True)
    assert support.calls == [(RetailerContext(store_id=50), True)]
    assert parts["decisions"].inputs == [], "a tap is the decision; no model reads it"
    assert result.state == tapped.state, "reaching support changes nothing in the session"


async def test_without_the_support_agent_nothing_offers_support() -> None:
    coordinator, _ = _coordinator(_asks(), store_knowledge=FakeStoreKnowledge())

    result = await coordinator.run(_turn(_state(), QUESTION))

    assert result.grounding.support is None


async def test_the_answer_says_its_support_is_beneath_it() -> None:
    coordinator, _ = _coordinator(
        _asks(), store_knowledge=FakeStoreKnowledge(), support=FakeSupport()
    )

    view = route_response(await coordinator.run(_turn(_state(), QUESTION))).primary

    assert isinstance(view, ResponseGroundingView)
    assert view.kind is ResponseOutcomeKind.POLICY_ANSWER
    assert view.support_offered is True


async def test_contact_support_is_its_own_reply() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER), support=FakeSupport()
    )
    tapped = _turn(_state(), "Contact support").model_copy(
        update={"support_action": ContactSupportAction()}
    )

    view = route_response(await coordinator.run(tapped)).primary

    assert isinstance(view, ResponseGroundingView)
    assert view.kind is ResponseOutcomeKind.SUPPORT_CONTACT
    assert view.support_offered is True


async def test_a_store_without_contacts_offers_no_buttons() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER), support=FakeSupport(SupportContacts())
    )
    tapped = _turn(_state(), "Contact support").model_copy(
        update={"support_action": ContactSupportAction()}
    )

    result = await coordinator.run(tapped)
    view = route_response(result).primary

    assert isinstance(view, ResponseGroundingView)
    assert view.support_offered is False
    presentation = ChatRuntime.presentation(result)
    assert presentation is None or presentation.support is None


async def test_the_stores_channels_are_drawn_beneath_the_reply() -> None:
    coordinator, _ = _coordinator(
        _asks(), store_knowledge=FakeStoreKnowledge(), support=FakeSupport()
    )

    presentation = ChatRuntime.presentation(await coordinator.run(_turn(_state(), QUESTION)))

    assert presentation is not None
    assert presentation.support is not None
    assert [c.url for c in presentation.support.channels] == [
        "https://wa.me/966551234567",
        "mailto:support@store.sa",
    ]
    assert presentation.products == ()
    assert [c.handover for c in presentation.support.channels] == [False, False]


async def test_channels_take_the_shoppers_request_where_the_platform_sends_it() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER), support=FakeSupport(handover=True)
    )
    tapped = _turn(_state(), "Contact support").model_copy(
        update={"support_action": ContactSupportAction()}
    )

    presentation = ChatRuntime.presentation(await coordinator.run(tapped))

    assert presentation is not None and presentation.support is not None
    assert [c.handover for c in presentation.support.channels] == [True, True]
    assert presentation.support.channels[0].url == "https://wa.me/966551234567"


async def test_faysal_adds_no_shopping_question_after_an_answer() -> None:
    """Even with products shown earlier, an answer about the store ends on the
    answer: the support channels are its next step, not "narrow these down"."""
    coordinator, _ = _coordinator(
        _asks(), store_knowledge=FakeStoreKnowledge(), support=FakeSupport()
    )
    base = _state()
    shopping = base.model_copy(
        update={
            "product_interaction": base.product_interaction.model_copy(
                update={"presented_product_ids": (20, 21)}
            )
        }
    )

    result = await coordinator.run(_turn(shopping, QUESTION))
    answer = CustomerResponse(message="Delivery to Jeddah takes 4 to 7 working days.")

    assert result.next_step is None
    assert _ends_on_a_question(answer, result) == answer
