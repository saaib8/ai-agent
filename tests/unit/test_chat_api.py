"""The chat endpoint, and a conversation that spans requests.

These run the real route, the real `ChatRuntime` and the real
`CustomerTurnCoordinator`. What is faked is only what reaches outside the
process: the providers, the catalog and Redis. So the multi-turn tests prove
something a single-turn test cannot - that state written at the end of one HTTP
request is the state a later request resolves "the second one" against.

The session store is faked rather than the Redis client, because the store's
own compare-and-set is proved against a real server in
`tests/integration/test_session_store_live.py`. A fake that re-implemented
`WATCH` would only be testing the fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from app.api.dependencies import (
    chat_graph,
    chat_runtime,
    retailer_context_provider,
)
from app.api.errors import register_exception_handlers
from app.api.routes.chat import router as chat_router
from app.core.config import SessionSettings
from app.core.exceptions import (
    SessionStateInvalidError,
    SessionStoreUnavailableError,
    StoreNotFoundError,
)
from app.orchestration.graph import ChatGraphRunner
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    ProductInteractionIntent,
    ProductInteractionOp,
)
from app.schemas.agent_turn import CustomerResponse
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.retailer import RetailerContext
from app.schemas.session import SessionEnvelope
from app.services.chat_runtime import ChatRuntime
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.test_turn_coordinator import (
    FakeHydration,
    FakePipeline,
    _coordinator,
    _resolved,
)

GRAPH = ChatGraphRunner()
"""One compiled graph for the whole module, as the process has one.

Compiling per test would be the thing `test_the_graph_is_compiled_once`
forbids, and would make these tests pass for a shape the service never uses.
"""

STORE, OTHER_STORE = 50, 51
SESSION = "sess-1"


# ── doubles for what leaves the process ═════════════════════════════════════


class FakeSessionStore:
    """An in-memory session store with the same contract as the real one.

    Compare-and-set is honoured here too, because the runtime's conflict
    behaviour depends on it - but the *mechanism* is proved against real Redis
    elsewhere.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.saved: dict[tuple[int, str], SessionEnvelope] = {}
        self.error = error
        self.loads = 0
        self.saves = 0

    async def load(self, store_id: int, session_id: str) -> SessionEnvelope | None:
        self.loads += 1
        if self.error is not None:
            raise self.error
        return self.saved.get((store_id, session_id))

    async def save_if_revision(
        self,
        store_id: int,
        session_id: str,
        *,
        expected_revision: int,
        envelope: SessionEnvelope,
    ) -> bool:
        self.saves += 1
        current = self.saved.get((store_id, session_id))
        stored_revision = current.session_revision if current else 0
        if stored_revision != expected_revision:
            return False
        self.saved[(store_id, session_id)] = envelope
        return True


class FakeResponses:
    """Wording, without a provider. Records what it was asked to word."""

    def __init__(self, message: str = "Here you are.") -> None:
        self.message = message
        self.calls: list[Any] = []

    async def generate(self, turn: Any, result: Any) -> CustomerResponse:
        self.calls.append((turn, result))
        return CustomerResponse(message=self.message)


class FakeRetailers:
    def __init__(self, known: set[int] | None = None) -> None:
        self.known = known if known is not None else {STORE, OTHER_STORE}

    async def resolve(self, store_id: int) -> RetailerContext:
        if store_id not in self.known:
            raise StoreNotFoundError(requested_store_id=store_id)
        return RetailerContext(store_id=store_id)


def a_search() -> CustomerAgentDecision:
    """A plain new search. No durable proposal attached: nothing in these
    tests turns on one, and a search does not need one."""
    return CustomerAgentDecision(action=AgentAction.SEARCH)


def a_selection(position: int) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.ANSWER,
        interaction=ProductInteractionIntent(
            op=ProductInteractionOp.SELECT,
            reference=PresentedOrdinal(position=position),
        ),
    )


class Harness:
    """One app, one session store, and a decision script to step through."""

    def __init__(
        self,
        *decisions: CustomerAgentDecision,
        product_ids: tuple[int, ...] = (10, 11, 12),
        store: FakeSessionStore | None = None,
        responses: FakeResponses | None = None,
        retailers: FakeRetailers | None = None,
    ) -> None:
        self.script = list(decisions)
        self.sessions = store or FakeSessionStore()
        self.responses = responses or FakeResponses()
        self.retailers = retailers or FakeRetailers()
        self.product_ids = product_ids
        self.coordinators: list[Any] = []
        self.parts: list[Any] = []

    def _runtime(self) -> ChatRuntime:
        # One coordinator per request, exactly as the real dependency builds
        # it. The decision for this turn comes off the script.
        decision = self.script.pop(0) if self.script else a_search()
        coordinator, parts = _coordinator(
            decision,
            # Query understanding is a provider step of its own; a search turn
            # needs one resolved, and these tests are not about interpretation.
            interpretation=_resolved(),
            pipeline=FakePipeline(ids=self.product_ids),
            hydration=FakeHydration(available=self.product_ids),
        )
        self.coordinators.append(coordinator)
        self.parts.append(parts)
        return ChatRuntime(
            coordinator,
            self.responses,  # type: ignore[arg-type]
            self.sessions,  # type: ignore[arg-type]
            SessionSettings(max_history_messages=6),
        )

    def app(self) -> FastAPI:
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(chat_router, prefix="/v1")
        app.dependency_overrides[chat_runtime] = self._runtime
        # The real compiled graph: these tests drive the runtime through it.
        app.dependency_overrides[chat_graph] = lambda: GRAPH
        app.dependency_overrides[retailer_context_provider] = lambda: self.retailers
        return app


@pytest_asyncio.fixture
async def client_for() -> AsyncIterator[Any]:
    clients: list[AsyncClient] = []

    async def build(harness: Harness) -> AsyncClient:
        client = AsyncClient(transport=ASGITransport(app=harness.app()), base_url="http://test")
        clients.append(client)
        return client

    try:
        yield build
    finally:
        for client in clients:
            await client.aclose()


def body(message: str, **extra: Any) -> dict[str, Any]:
    return {"session_id": SESSION, "store_id": STORE, "message": message, **extra}


# ── one turn ════════════════════════════════════════════════════════════════


async def test_a_first_turn_answers_and_opens_the_session(client_for: Any) -> None:
    harness = Harness(a_search())
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("show me modern sofas"))

    assert reply.status_code == 200
    payload = reply.json()
    assert payload["session_id"] == SESSION
    assert payload["session_revision"] == 1, "a persisted turn is revision one"
    assert payload["response"]["message"] == "Here you are."
    assert len(payload["presentation"]["products"]) == 3


async def test_the_reply_carries_the_products_the_application_rendered(
    client_for: Any,
) -> None:
    """Prices and links come from the catalog read, not from any model."""
    harness = Harness(a_search())
    client = await client_for(harness)

    payload = (await client.post("/v1/chat", json=body("sofas"))).json()

    first = payload["presentation"]["products"][0]
    assert first["grounding_ref"] == 1
    assert first["presented_ordinal"] == 1
    assert first["price_amount"] is not None
    assert first["product_url"].startswith("https://")


@pytest.mark.parametrize(
    "forbidden",
    ["product_id", "store_id", "state", "decision", "grounding", "line_id", "need_id"],
)
async def test_no_internal_identity_reaches_the_caller(client_for: Any, forbidden: str) -> None:
    harness = Harness(a_search())
    client = await client_for(harness)

    raw = (await client.post("/v1/chat", json=body("sofas"))).text

    assert f'"{forbidden}"' not in raw, forbidden


async def test_an_answer_with_nothing_to_draw_sends_no_presentation(
    client_for: Any,
) -> None:
    harness = Harness(CustomerAgentDecision(action=AgentAction.ANSWER))
    client = await client_for(harness)

    payload = (await client.post("/v1/chat", json=body("what do you sell?"))).json()

    assert payload["presentation"] is None
    assert payload["session_revision"] == 1


# ── a conversation across requests ══════════════════════════════════════════


async def test_the_second_turn_sees_the_first_turns_state_and_words(
    client_for: Any,
) -> None:
    """The M13 claim: state and history survive the round trip (M13 35)."""
    harness = Harness(a_search(), a_selection(position=2))
    client = await client_for(harness)

    first = (await client.post("/v1/chat", json=body("show me modern sofas"))).json()
    second = (await client.post("/v1/chat", json=body("I like the second one"))).json()

    assert first["session_revision"] == 1
    assert second["session_revision"] == 2, "one increment per persisted turn"

    # The second turn was handed the first turn's conversation and state.
    turn = harness.responses.calls[1][0]
    assert [m.content for m in turn.conversation.messages] == [
        "show me modern sofas",
        "Here you are.",
    ]
    assert turn.state.product_interaction.presented_product_ids == (10, 11, 12)
    assert turn.message == "I like the second one", "the current message is separate"


async def test_an_ordinal_is_resolved_against_the_restored_state(
    client_for: Any,
) -> None:
    """ "The second one" is settled against the *previous* turn's products.

    The resolver itself is faked here, so what this proves is the input it was
    given: the selector, and a state carrying the ids the earlier request
    persisted. That the resolver then picks the right one of them is M11B's,
    and tested there against the real resolver.
    """
    harness = Harness(a_search(), a_selection(position=2))
    client = await client_for(harness)

    await client.post("/v1/chat", json=body("show me modern sofas"))
    await client.post("/v1/chat", json=body("I like the second one"))

    selector, state = harness.parts[1]["references"].calls[0]
    assert selector == PresentedOrdinal(position=2)
    assert state.product_interaction.presented_product_ids == (10, 11, 12)

    # And whatever it resolved to was recorded as the selection.
    stored = harness.sessions.saved[(STORE, SESSION)]
    assert len(stored.state.product_interaction.selected_product_ids) == 1


async def test_history_is_not_duplicated_across_turns(client_for: Any) -> None:
    harness = Harness(a_search(), a_search(), a_search())
    client = await client_for(harness)

    for message in ("one", "two", "three"):
        await client.post("/v1/chat", json=body(message))

    stored = harness.sessions.saved[(STORE, SESSION)]
    assert [m.content for m in stored.conversation.messages] == [
        "one",
        "Here you are.",
        "two",
        "Here you are.",
        "three",
        "Here you are.",
    ]


async def test_history_is_bounded_and_keeps_whole_exchanges(
    client_for: Any,
) -> None:
    """Six messages allowed, so the oldest exchanges fall out in pairs."""
    harness = Harness(*[a_search() for _ in range(5)])
    client = await client_for(harness)

    for message in ("one", "two", "three", "four", "five"):
        await client.post("/v1/chat", json=body(message))

    messages = harness.sessions.saved[(STORE, SESSION)].conversation.messages
    assert len(messages) == 6
    assert [m.content for m in messages] == [
        "three",
        "Here you are.",
        "four",
        "Here you are.",
        "five",
        "Here you are.",
    ]
    assert messages[0].role.value == "user", "never an orphaned reply"


# ── staleness and conflict ══════════════════════════════════════════════════


async def test_a_stale_client_revision_is_refused(client_for: Any) -> None:
    harness = Harness(a_search(), a_search())
    client = await client_for(harness)

    await client.post("/v1/chat", json=body("one"))  # session is now revision 1
    reply = await client.post(
        "/v1/chat", json=body("keep the second one", expected_session_revision=0)
    )

    assert reply.status_code == 409
    assert reply.json()["error"]["code"] == "session_conflict"


async def test_a_stale_revision_costs_no_provider_call(client_for: Any) -> None:
    """Refused before the decision model, the catalog and the response model.

    Both correctness and cost: a turn resolved against a session that moved on
    would answer a question the customer did not ask (M13 32).
    """
    harness = Harness(a_search(), a_search())
    client = await client_for(harness)

    await client.post("/v1/chat", json=body("one"))
    assert len(harness.parts[0]["decisions"].inputs) == 1, "the first turn did ask"

    await client.post("/v1/chat", json=body("two", expected_session_revision=0))

    # A second coordinator was built, and asked nothing.
    assert len(harness.parts) == 2
    assert harness.parts[1]["decisions"].inputs == [], "no decision model"
    assert harness.parts[1]["pipeline"].calls == [], "no catalog search"
    assert len(harness.responses.calls) == 1, "nothing was worded"


async def test_a_matching_revision_proceeds(client_for: Any) -> None:
    harness = Harness(a_search(), a_search())
    client = await client_for(harness)

    await client.post("/v1/chat", json=body("one"))
    reply = await client.post("/v1/chat", json=body("two", expected_session_revision=1))

    assert reply.status_code == 200
    assert reply.json()["session_revision"] == 2


async def test_an_expired_session_with_a_client_revision_is_a_conflict(
    client_for: Any,
) -> None:
    """Not a fresh start: their screen shows a session that no longer exists."""
    harness = Harness(a_search())
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("keep that one", expected_session_revision=4))

    assert reply.status_code == 409
    assert harness.sessions.saves == 0


async def test_a_first_request_with_no_client_revision_is_a_new_session(
    client_for: Any,
) -> None:
    harness = Harness(a_search())
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("hello"))

    assert reply.status_code == 200
    assert reply.json()["session_revision"] == 1


async def test_a_lost_race_returns_a_conflict_not_the_computed_answer(
    client_for: Any,
) -> None:
    """The loser's reply resolved words against a room that has since changed.

    Returning it would tell the customer their change took effect when the
    stored session says otherwise (M13 33).
    """
    harness = Harness(a_search())
    client = await client_for(harness)

    # Someone else commits revision 1 while this request is mid-flight.
    class Interfering(FakeSessionStore):
        async def load(self, store_id: int, session_id: str) -> SessionEnvelope | None:
            loaded = await super().load(store_id, session_id)
            from app.schemas.session import new_session

            self.saved[(store_id, session_id)] = new_session().advanced(
                state=new_session().state, conversation=new_session().conversation
            )
            return loaded

    harness.sessions = Interfering()
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("show me sofas"))

    assert reply.status_code == 409
    assert reply.json()["error"]["code"] == "session_conflict"


# ── failing safely ══════════════════════════════════════════════════════════


async def test_an_unreachable_session_store_fails_rather_than_forgetting(
    client_for: Any,
) -> None:
    """Never a stateless fallback: it would reinterpret every reference."""
    harness = Harness(a_search(), store=FakeSessionStore(error=SessionStoreUnavailableError()))
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("show me sofas"))

    assert reply.status_code == 503
    assert "redis" not in reply.text.lower()


async def test_unreadable_session_state_is_refused_not_reset(
    client_for: Any,
) -> None:
    harness = Harness(a_search(), store=FakeSessionStore(error=SessionStateInvalidError()))
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("show me sofas"))

    assert reply.status_code == 409
    assert reply.json()["error"]["code"] == "session_state_invalid"


async def test_an_unknown_store_is_refused_before_any_session_work(
    client_for: Any,
) -> None:
    harness = Harness(a_search())
    client = await client_for(harness)

    reply = await client.post(
        "/v1/chat", json={"session_id": SESSION, "store_id": 999, "message": "hi"}
    )

    assert reply.status_code == 404
    assert harness.sessions.loads == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"store_id": STORE, "message": "hi"},
        {"session_id": SESSION, "message": "hi"},
        {"session_id": SESSION, "store_id": STORE},
        {"session_id": SESSION, "store_id": STORE, "message": ""},
        {"session_id": SESSION, "store_id": STORE, "message": "   "},
        {"session_id": "bad:id", "store_id": STORE, "message": "hi"},
        {"session_id": SESSION, "store_id": 0, "message": "hi"},
        {"session_id": SESSION, "store_id": STORE, "message": "hi", "extra": 1},
        {
            "session_id": SESSION,
            "store_id": STORE,
            "message": "hi",
            "expected_session_revision": -1,
        },
    ],
)
async def test_a_malformed_request_is_refused_without_echoing_it(
    client_for: Any, payload: dict[str, Any]
) -> None:
    harness = Harness(a_search())
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=payload)

    assert reply.status_code == 422
    assert harness.sessions.loads == 0


# ── retailer isolation ══════════════════════════════════════════════════════


async def test_one_session_id_under_two_stores_is_two_conversations(
    client_for: Any,
) -> None:
    """Structural: different keys, so it holds whatever the catalogs contain."""
    harness = Harness(a_search(), a_search())
    client = await client_for(harness)

    ours = await client.post(
        "/v1/chat", json={"session_id": SESSION, "store_id": STORE, "message": "hi"}
    )
    theirs = await client.post(
        "/v1/chat",
        json={"session_id": SESSION, "store_id": OTHER_STORE, "message": "hi"},
    )

    assert ours.json()["session_revision"] == 1
    assert theirs.json()["session_revision"] == 1, "a separate conversation"
    assert set(harness.sessions.saved) == {(STORE, SESSION), (OTHER_STORE, SESSION)}


async def test_each_store_gets_its_own_scope(client_for: Any) -> None:
    harness = Harness(a_search(), a_search())
    client = await client_for(harness)

    await client.post("/v1/chat", json={"session_id": SESSION, "store_id": STORE, "message": "hi"})
    await client.post(
        "/v1/chat",
        json={"session_id": SESSION, "store_id": OTHER_STORE, "message": "hi"},
    )

    scopes = [call[0].context.store_id for call in harness.responses.calls]
    assert scopes == [STORE, OTHER_STORE]


# ── the runtime adds no provider work (M13 43) ══════════════════════════════


@pytest.mark.parametrize(
    ("label", "decision"),
    [
        ("answer", CustomerAgentDecision(action=AgentAction.ANSWER)),
        ("search", CustomerAgentDecision(action=AgentAction.SEARCH)),
    ],
)
async def test_one_turn_asks_each_model_once(
    client_for: Any, label: str, decision: CustomerAgentDecision
) -> None:
    """Orchestration sequences phases; it is not a second reasoning layer.

    One decision call and one wording call per exchange, whatever the action -
    the same counts the engine made before there was a runtime around it.
    """
    harness = Harness(decision)
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("something"))

    assert reply.status_code == 200
    assert len(harness.parts[0]["decisions"].inputs) == 1, label
    assert len(harness.responses.calls) == 1, label


async def test_a_second_turn_does_not_replay_the_first(client_for: Any) -> None:
    """Each request is one exchange. Nothing re-runs a committed turn."""
    harness = Harness(a_search(), a_search())
    client = await client_for(harness)

    await client.post("/v1/chat", json=body("one"))
    await client.post("/v1/chat", json=body("two"))

    assert [len(p["decisions"].inputs) for p in harness.parts] == [1, 1]
    assert len(harness.responses.calls) == 2
    assert harness.sessions.saves == 2
