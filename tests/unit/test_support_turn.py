"""Faysal's side of a turn: the store's own support, offered beside the reply.

Every turn about the store carries the store's support - with an answer from
its documents, and in place of one that could not be looked up - and a tapped
"Contact support" reaches it with no model involved. The contacts are the
scoped store's own; nothing here lets a model name or write one.
"""

from __future__ import annotations

from app.core.exceptions import StoreKnowledgeUnavailableError
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.grounding import TurnFailureCode
from app.schemas.retailer import RetailerContext
from app.schemas.support import ContactSupportAction, SupportContacts, SupportGrounding

from tests.unit.test_store_info_turn import QUESTION, FakeStoreKnowledge, _asks
from tests.unit.test_turn_coordinator import _coordinator, _state, _turn

CONTACTS = SupportContacts(email="support@store.sa", whatsapp="966551234567")


class FakeSupport:
    def __init__(self, contacts: SupportContacts = CONTACTS) -> None:
        self.contacts = contacts
        self.calls: list[tuple[RetailerContext, bool]] = []

    async def offer(self, context: RetailerContext, *, requested: bool = False) -> SupportGrounding:
        self.calls.append((context, requested))
        return SupportGrounding(contacts=self.contacts, requested=requested)


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
