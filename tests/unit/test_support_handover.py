"""What the store's support is handed when a shopper's request is sent to it.

The shopper's own words, oldest first and cut short, read for the scoped store
only - another store's session id finds nothing - and nothing in the session
changes. Where the platform does not send requests, the route refuses.
"""

from __future__ import annotations

from typing import cast

from app.api.dependencies import retailer_context_provider, support_handover_service
from app.api.errors import register_exception_handlers
from app.api.routes.support import router as support_router
from app.repositories.sessions import SessionStore
from app.schemas.conversation import ConversationContext, ConversationMessage, ConversationRole
from app.schemas.retailer import RetailerContext
from app.schemas.session import new_session
from app.schemas.support import HANDOVER_MESSAGE_CHARS, HANDOVER_MESSAGES
from app.services.support_handover import SupportHandoverService, recent_messages
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.test_chat_api import OTHER_STORE, STORE, FakeRetailers, FakeSessionStore

SESSION = "shopper-1"
USER, ASSISTANT = ConversationRole.USER, ConversationRole.ASSISTANT


def _chat(*turns: tuple[ConversationRole, str]) -> ConversationContext:
    return ConversationContext(
        messages=tuple(ConversationMessage(role=role, content=text) for role, text in turns)
    )


async def _stored(sessions: FakeSessionStore, conversation: ConversationContext) -> None:
    envelope = new_session().advanced(state=new_session().state, conversation=conversation)
    assert await sessions.save_if_revision(STORE, SESSION, expected_revision=0, envelope=envelope)


def test_only_the_shoppers_own_words_are_handed_over_oldest_first() -> None:
    chat = _chat(
        (USER, "show me grey sofas"),
        (ASSISTANT, "Here are five grey sofas."),
        (USER, "my order arrived   damaged"),
    )

    assert recent_messages(chat) == ("show me grey sofas", "my order arrived damaged")


def test_only_the_last_messages_go_and_each_is_cut_short() -> None:
    said = [(USER, f"message {n}") for n in range(HANDOVER_MESSAGES + 3)]
    long = (USER, "x" * (HANDOVER_MESSAGE_CHARS + 50))

    messages = recent_messages(_chat(*said, long))

    assert len(messages) == HANDOVER_MESSAGES
    assert messages[0] == "message 4"
    assert len(messages[-1]) == HANDOVER_MESSAGE_CHARS
    assert messages[-1].endswith("…")


async def test_another_stores_session_hands_over_nothing() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _chat((USER, "my sofa is broken")))
    service = SupportHandoverService(cast(SessionStore, sessions))

    own = await service.handover(RetailerContext(store_id=STORE), SESSION)
    other = await service.handover(RetailerContext(store_id=OTHER_STORE), SESSION)

    assert own.messages == ("my sofa is broken",)
    assert other.messages == ()


def _app(service: SupportHandoverService | None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(support_router, prefix="/v1")
    app.dependency_overrides[support_handover_service] = lambda: service
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()
    return app


async def test_the_route_reads_the_conversation_and_changes_nothing() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _chat((USER, "do you deliver to Dammam?")))
    before = await sessions.load(STORE, SESSION)
    app = _app(SupportHandoverService(cast(SessionStore, sessions)))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        answer = await client.post(
            "/v1/support/handover", json={"session_id": SESSION, "store_id": STORE}
        )

    assert answer.status_code == 200
    assert answer.json() == {"messages": ["do you deliver to Dammam?"]}
    assert sessions.saves == 1
    assert (await sessions.load(STORE, SESSION)) == before


async def test_the_route_refuses_where_the_platform_does_not_send_requests() -> None:
    async with AsyncClient(transport=ASGITransport(app=_app(None)), base_url="http://t") as client:
        answer = await client.post(
            "/v1/support/handover", json={"session_id": SESSION, "store_id": STORE}
        )

    assert answer.status_code == 404
    assert answer.json()["error"]["code"] == "support_handover_off"
