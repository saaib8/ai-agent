"""The chat endpoint, and a conversation that spans requests.

These run the real route, the real graph, the real `ChatRuntime`, the real
agent team and the real tools. What is faked is only what reaches outside the
process: the chat models (scripted), the catalog and Redis. So the multi-turn
tests prove something a single-turn test cannot - that state written at the
end of one HTTP request is the state a later request resolves "the second
one" against.

The session store is faked rather than the Redis client, because the store's
own compare-and-set is proved against a real server in
`tests/integration/test_session_store_live.py`. A fake that re-implemented
`WATCH` would only be testing the fake.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from app.agents.reply import FALLBACK_REPLY
from app.agents.team import AgentTeam
from app.api.dependencies import (
    chat_graph,
    chat_runtime,
    click_tracking_service,
    retailer_context_provider,
)
from app.api.errors import register_exception_handlers
from app.api.routes.chat import router as chat_router
from app.core.config import AgentsSettings, HistorySummarySettings, SessionSettings
from app.core.exceptions import (
    LLMUnavailableError,
    SessionStateInvalidError,
    SessionStoreUnavailableError,
    StoreNotFoundError,
)
from app.orchestration.graph import ChatGraphRunner
from app.repositories.sessions import shopper_of
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.resolution import (
    ReferenceFailureReason,
    ReferenceUnresolved,
    ResolvedProductReference,
)
from app.schemas.retailer import RetailerContext
from app.schemas.session import SessionEnvelope, ShopperCart
from app.services.chat_runtime import ChatRuntime
from app.services.click_tracking import ClickTrackingService, tracking_url
from app.services.history_summary import HistorySummarizer
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from tests.unit.agents.fake_chat_model import ScriptedChatModel, call, step
from tests.unit.agents.fakes import FakeHydration, FakePipeline, agent_services, real_loop
from tests.unit.click_ids import FakeClickIds, no_click_ids

NO_SUMMARY = HistorySummarySettings(enabled=False)
"""A small window that drops its oldest messages, as before summaries."""

GRAPH = ChatGraphRunner()
"""One compiled graph for the whole module, as the process has one."""

STORE, OTHER_STORE = 50, 51
SESSION = "sess-1"
REPLY = "Here you are."


# ── doubles for what leaves the process ═════════════════════════════════════


class FakeSessionStore:
    """An in-memory session store with the same contract as the real one.

    Compare-and-set is honoured here too, because the runtime's conflict
    behaviour depends on it - but the *mechanism* is proved against real Redis
    elsewhere.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.saved: dict[tuple[int, str], SessionEnvelope] = {}
        self.carts: dict[tuple[int, str], ShopperCart] = {}
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
        shopper = shopper_of(session_id)
        if shopper is not None:
            interaction = envelope.state.product_interaction
            self.carts[(store_id, shopper)] = ShopperCart(
                selected_product_ids=interaction.selected_product_ids,
                cart_quantities=interaction.cart_quantities,
            )
        return True

    async def shopper_cart(self, store_id: int, session_id: str) -> ShopperCart | None:
        shopper = shopper_of(session_id)
        return self.carts.get((store_id, shopper)) if shopper is not None else None


class FakeRetailers:
    def __init__(self, known: set[int] | None = None) -> None:
        self.known = known if known is not None else {STORE, OTHER_STORE}

    async def resolve(self, store_id: int) -> RetailerContext:
        if store_id not in self.known:
            raise StoreNotFoundError(requested_store_id=store_id)
        return RetailerContext(store_id=store_id)


class ScreenReferences:
    """ "The second one" counted into the list the restored state presented."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, AgentStateV1]] = []

    async def resolve(self, selector: Any, state: AgentStateV1, context: Any) -> Any:
        self.calls.append((selector, state))
        presented = state.product_interaction.presented_product_ids
        if not isinstance(selector, PresentedOrdinal) or not presented:
            return ReferenceUnresolved(reason=ReferenceFailureReason.NO_PRESENTED_RESULTS)
        if selector.position > len(presented):
            return ReferenceUnresolved(reason=ReferenceFailureReason.ORDINAL_OUT_OF_RANGE)
        return ResolvedProductReference(product_id=presented[selector.position - 1])


# ── what the agent does each turn ═══════════════════════════════════════════

Turn = list[AIMessage]
"""Sara's steps for one turn; the supervisor always hands the turn to her."""


def _reply(text: str = REPLY) -> AIMessage:
    return step(call("respond", "r", message=text))


def a_search(subcategory: str = "sofa") -> Turn:
    search = {"commerce_category": "seating", "commerce_subcategory": subcategory}
    return [step(call("search_products", "s", search=search)), _reply()]


def an_answer(text: str = REPLY) -> Turn:
    return [_reply(text)]


def a_selection(position: int) -> Turn:
    which = {"kind": "presented_ordinal", "position": position}
    return [step(call("add_to_cart", "a", items=[{"which": which}])), _reply()]


class Harness:
    """One app, one session store, one agent team, and a script of turns."""

    def __init__(
        self,
        *turns: Turn,
        product_ids: tuple[int, ...] = (10, 11, 12),
        store: FakeSessionStore | None = None,
        retailers: FakeRetailers | None = None,
        arabic_replies: bool = False,
        click_ids: FakeClickIds | None = None,
        summarizer: HistorySummarizer | None = None,
        session_settings: SessionSettings | None = None,
    ) -> None:
        self.supervisor = ScriptedChatModel(
            script=[step(call("transfer_to_sara", f"t{n}", need="shopping")) for n in turns]
        )
        self.sara = ScriptedChatModel(script=[s for turn in turns for s in turn])
        models = {a: ScriptedChatModel(script=[]) for a in AgentPersona}
        models[AgentPersona.SARA] = self.sara
        self.team = AgentTeam(self.supervisor, models, AgentsSettings(), real_loop())
        self.click_ids = click_ids
        self.arabic_replies = arabic_replies
        self.sessions = store or FakeSessionStore()
        self.retailers = retailers or FakeRetailers()
        self.pipeline = FakePipeline(ids=product_ids)
        self.hydration = FakeHydration(available=product_ids)
        self.references = ScreenReferences()
        self.runtimes = 0
        self.summarizer = summarizer
        self.session_settings = session_settings or SessionSettings(
            max_history_messages=6, summary=NO_SUMMARY
        )

    def _runtime(self) -> ChatRuntime:
        """One runtime per request, exactly as the real dependency builds it."""
        self.runtimes += 1
        return ChatRuntime(
            self.team,
            agent_services(
                pipeline=self.pipeline,
                hydration=self.hydration,
                references=self.references,  # type: ignore[arg-type]
            ),
            self.sessions,  # type: ignore[arg-type]
            self.session_settings,
            arabic_replies=self.arabic_replies,
            summarizer=self.summarizer,
        )

    def app(self) -> FastAPI:
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(chat_router, prefix="/v1")
        app.dependency_overrides[chat_runtime] = self._runtime
        app.dependency_overrides[chat_graph] = lambda: GRAPH
        app.dependency_overrides[retailer_context_provider] = lambda: self.retailers
        click_ids = self.click_ids
        app.dependency_overrides[click_tracking_service] = (
            no_click_ids if click_ids is None else lambda: ClickTrackingService(click_ids)
        )
        return app

    def told(self, model_call: int) -> list[str]:
        """Every message Sara's n-th model call was given, as text."""
        return [str(m.content) for m in self.sara.seen[model_call]]


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
    assert payload["response"]["message"] == REPLY
    assert payload["agent"] == "sara"
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


async def test_each_card_links_to_its_page_with_the_zory_click_id(client_for: Any) -> None:
    """The link a customer opens carries the product's public id, attached
    after the turn, in `product_url` itself - as Zory's own links do."""
    plain = Harness(a_search())
    urls = [
        p["product_url"]
        for p in (await (await client_for(plain)).post("/v1/chat", json=body("sofas"))).json()[
            "presentation"
        ]["products"]
    ]
    click_ids = FakeClickIds({url: f"click-{n}" for n, url in enumerate(urls)})
    harness = Harness(a_search(), click_ids=click_ids)

    payload = (await (await client_for(harness)).post("/v1/chat", json=body("sofas"))).json()

    products = payload["presentation"]["products"]
    assert [p["product_url"] for p in products] == [
        tracking_url(url, f"click-{n}") for n, url in enumerate(urls)
    ]
    assert [store for _, store in click_ids.lookups] == [STORE]


async def test_a_card_whose_page_has_no_click_id_keeps_its_page(client_for: Any) -> None:
    payload = (
        await (await client_for(Harness(a_search()))).post("/v1/chat", json=body("sofas"))
    ).json()
    assert all("zory_click_id" not in p["product_url"] for p in payload["presentation"]["products"])


@pytest.mark.parametrize(
    "forbidden",
    ["product_id", "store_id", "state", "decision", "grounding", "line_id", "need_id"],
)
async def test_no_internal_identity_reaches_the_caller(client_for: Any, forbidden: str) -> None:
    harness = Harness(a_search())
    client = await client_for(harness)

    raw = (await client.post("/v1/chat", json=body("sofas"))).text

    assert f'"{forbidden}"' not in raw, forbidden


async def test_an_answer_with_nothing_to_draw_has_no_presentation(client_for: Any) -> None:
    harness = Harness(an_answer("We sell furniture for every room."))
    client = await client_for(harness)

    payload = (await client.post("/v1/chat", json=body("what do you sell?"))).json()

    assert payload["presentation"] is None
    assert payload["session_revision"] == 1
    assert harness.pipeline.calls == [], "nothing was searched"


async def test_a_turn_the_agents_cannot_finish_gets_the_fixed_reply_and_keeps_nothing(
    client_for: Any,
) -> None:
    """A search ran, then the agent produced nothing usable, twice - even when
    told the turn stopped: the customer is answered with the fixed reply and
    the search is not kept (CLAUDE.md 21.1)."""
    search = a_search()[0]
    harness = Harness([search, step(), step()])
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("sofas"))

    assert reply.status_code == 200
    assert reply.json()["response"]["message"] == FALLBACK_REPLY["en"]
    assert reply.json()["presentation"] is None
    stored = harness.sessions.saved[(STORE, SESSION)]
    assert stored.state.product_interaction.presented_product_ids == ()
    assert stored.state.active_search is None


async def test_a_stopped_turn_is_worded_by_the_agent_and_keeps_its_work(
    client_for: Any,
) -> None:
    """A search ran, then the agent's next step was unusable: it is told the
    turn stopped, says what it did, and the search it ran is kept."""
    search = a_search()[0]
    harness = Harness([search, step(), _reply("I found these sofas.")])
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("sofas"))

    assert reply.json()["response"]["message"] == "I found these sofas."
    stored = harness.sessions.saved[(STORE, SESSION)]
    assert stored.state.active_search is not None


# ── a conversation across requests ══════════════════════════════════════════


async def test_the_second_turn_sees_the_first_turns_state_and_words(
    client_for: Any,
) -> None:
    """State and history survive the round trip."""
    harness = Harness(a_search(), a_selection(position=2))
    client = await client_for(harness)

    first = (await client.post("/v1/chat", json=body("show me modern sofas"))).json()
    second = (await client.post("/v1/chat", json=body("I like the second one"))).json()

    assert first["session_revision"] == 1
    assert second["session_revision"] == 2, "one increment per persisted turn"
    # Sara's first call of the second turn (after two calls in the first).
    told = harness.told(2)
    assert told[1:3] == ["show me modern sofas", REPLY], "the earlier exchange, in order"
    assert told[-1].endswith("The customer says:\nI like the second one")


async def test_an_ordinal_is_resolved_against_the_restored_state(
    client_for: Any,
) -> None:
    """ "The second one" is settled against the *previous* turn's products,
    and what it resolved to goes into the cart."""
    harness = Harness(a_search(), a_selection(position=2))
    client = await client_for(harness)

    await client.post("/v1/chat", json=body("show me modern sofas"))
    reply = await client.post("/v1/chat", json=body("add the second one"))

    selector, state = harness.references.calls[0]
    assert selector == PresentedOrdinal(position=2)
    assert state.product_interaction.presented_product_ids == (10, 11, 12)
    stored = harness.sessions.saved[(STORE, SESSION)]
    assert stored.state.product_interaction.selected_product_ids == (11,)
    assert len(reply.json()["picks"]) == 1


async def test_history_is_not_duplicated_across_turns(client_for: Any) -> None:
    harness = Harness(an_answer(), an_answer(), an_answer())
    client = await client_for(harness)

    for message in ("one", "two", "three"):
        await client.post("/v1/chat", json=body(message))

    stored = harness.sessions.saved[(STORE, SESSION)]
    assert [m.content for m in stored.conversation.messages] == [
        "one",
        REPLY,
        "two",
        REPLY,
        "three",
        REPLY,
    ]


async def test_history_is_bounded_and_keeps_whole_exchanges(
    client_for: Any,
) -> None:
    """Six messages allowed, so the oldest exchanges fall out in pairs."""
    harness = Harness(*[an_answer() for _ in range(5)])
    client = await client_for(harness)

    for message in ("one", "two", "three", "four", "five"):
        await client.post("/v1/chat", json=body(message))

    messages = harness.sessions.saved[(STORE, SESSION)].conversation.messages
    assert len(messages) == 6
    assert [m.content for m in messages] == [
        "three",
        REPLY,
        "four",
        REPLY,
        "five",
        REPLY,
    ]
    assert messages[0].role.value == "user", "never an orphaned reply"


SUMMARISED = SessionSettings(
    max_history_messages=6,
    summary=HistorySummarySettings(fold_messages=2, hard_cap_messages=8),
)
"""Six messages kept word for word; the oldest exchange folded on reaching them."""


class SummaryClient:
    """The summarising model: each call answers the next summary, or fails."""

    model = "summary-test"

    def __init__(self, *answers: str | Exception, delay_s: float = 0) -> None:
        self.answers = list(answers)
        self.inputs: list[str] = []
        self.delay_s = delay_s

    async def parse(self, *, instructions: str, user_input: str, schema: type[Any]) -> Any:
        self.inputs.append(user_input)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return schema(summary=answer)


async def test_reaching_the_window_folds_the_oldest_exchange_into_the_summary(
    client_for: Any,
) -> None:
    client_model = SummaryClient("The customer said hello.", "The customer said hello twice.")
    harness = Harness(
        *[an_answer() for _ in range(4)],
        summarizer=HistorySummarizer(client_model, SUMMARISED),  # type: ignore[arg-type]
        session_settings=SUMMARISED,
    )
    client = await client_for(harness)

    for message in ("one", "two", "three"):
        await client.post("/v1/chat", json=body(message))

    stored = harness.sessions.saved[(STORE, SESSION)].conversation
    assert stored.earlier_summary == "The customer said hello."
    assert [m.content for m in stored.messages] == ["two", REPLY, "three", REPLY]
    assert len(client_model.inputs) == 1, "nothing is summarised before the window"
    assert "[customer] one" in client_model.inputs[0]

    await client.post("/v1/chat", json=body("four"))
    told = harness.told(-1)
    assert any("Earlier in this chat" in t and "said hello." in t for t in told)
    stored = harness.sessions.saved[(STORE, SESSION)].conversation
    assert stored.earlier_summary == "The customer said hello twice."
    assert "Summary so far:\nThe customer said hello." in client_model.inputs[1]


async def test_a_summary_that_fails_drops_nothing(client_for: Any) -> None:
    unavailable = SummaryClient(*[LLMUnavailableError(provider="openai")] * 2)
    harness = Harness(
        *[an_answer() for _ in range(4)],
        summarizer=HistorySummarizer(unavailable, SUMMARISED),  # type: ignore[arg-type]
        session_settings=SUMMARISED,
    )
    client = await client_for(harness)

    for message in ("one", "two", "three", "four"):
        response = await client.post("/v1/chat", json=body(message))
        assert response.status_code == 200

    stored = harness.sessions.saved[(STORE, SESSION)].conversation
    assert stored.earlier_summary is None
    assert stored.messages[0].content == "one", "kept up to the hard cap"
    assert len(stored.messages) == 8


@pytest.mark.parametrize(
    "client_model",
    [
        SummaryClient(KeyError("defect"), KeyError("defect")),
        SummaryClient("Too late.", "Too late.", delay_s=5),
    ],
    ids=["a defect", "too slow"],
)
async def test_the_summary_never_holds_back_or_breaks_the_reply(
    client_for: Any, client_model: SummaryClient
) -> None:
    """A finished reply is saved and answered whatever the summary does: a
    defect is logged, a slow summary is left for the next turn."""
    settings = SessionSettings(
        max_history_messages=6,
        summary=HistorySummarySettings(
            fold_messages=2, hard_cap_messages=8, wait_after_turn_s=0.05
        ),
    )
    harness = Harness(
        *[an_answer() for _ in range(3)],
        summarizer=HistorySummarizer(client_model, settings),  # type: ignore[arg-type]
        session_settings=settings,
    )
    client = await client_for(harness)

    for message in ("one", "two", "three"):
        response = await client.post("/v1/chat", json=body(message))
        assert response.status_code == 200

    stored = harness.sessions.saved[(STORE, SESSION)].conversation
    assert stored.earlier_summary is None
    assert [m.content for m in stored.messages] == ["one", REPLY, "two", REPLY, "three", REPLY]


async def test_each_reply_is_stored_under_the_agent_that_gave_it(client_for: Any) -> None:
    harness = Harness(an_answer())
    client = await client_for(harness)

    await client.post("/v1/chat", json=body("hi"))

    said = harness.sessions.saved[(STORE, SESSION)].conversation.messages[-1]
    assert said.agent is AgentPersona.SARA


# ── staleness and conflict ══════════════════════════════════════════════════


async def test_a_stale_client_revision_is_refused(client_for: Any) -> None:
    harness = Harness(an_answer(), an_answer())
    client = await client_for(harness)

    await client.post("/v1/chat", json=body("one"))  # session is now revision 1
    reply = await client.post(
        "/v1/chat", json=body("keep the second one", expected_session_revision=0)
    )

    assert reply.status_code == 409
    assert reply.json()["error"]["code"] == "session_conflict"


async def test_a_stale_revision_costs_no_model_or_catalog_call(client_for: Any) -> None:
    """Refused before the supervisor, any agent and the catalog: a turn
    resolved against a session that moved on would answer a question the
    customer did not ask."""
    harness = Harness(a_search(), a_search())
    client = await client_for(harness)

    await client.post("/v1/chat", json=body("one"))
    assert len(harness.supervisor.seen) == 1
    assert len(harness.pipeline.calls) == 1

    await client.post("/v1/chat", json=body("two", expected_session_revision=0))

    assert harness.runtimes == 2, "a second runtime was built"
    assert len(harness.supervisor.seen) == 1, "no supervisor call"
    assert len(harness.sara.seen) == 2, "no agent call"
    assert len(harness.pipeline.calls) == 1, "no catalog search"


async def test_a_matching_revision_proceeds(client_for: Any) -> None:
    harness = Harness(an_answer(), an_answer())
    client = await client_for(harness)

    await client.post("/v1/chat", json=body("one"))
    reply = await client.post("/v1/chat", json=body("two", expected_session_revision=1))

    assert reply.status_code == 200
    assert reply.json()["session_revision"] == 2


async def test_an_expired_session_with_a_client_revision_is_a_conflict(
    client_for: Any,
) -> None:
    """Not a fresh start: their screen shows a session that no longer exists."""
    harness = Harness(an_answer())
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("keep that one", expected_session_revision=4))

    assert reply.status_code == 409
    assert harness.sessions.saves == 0


async def test_a_first_request_with_no_client_revision_is_a_new_session(
    client_for: Any,
) -> None:
    harness = Harness(an_answer())
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("hello"))

    assert reply.status_code == 200
    assert reply.json()["session_revision"] == 1


async def test_a_lost_race_returns_a_conflict_not_the_computed_answer(
    client_for: Any,
) -> None:
    """The loser's reply resolved words against a session that has since
    changed. Returning it would tell the customer their change took effect
    when the stored session says otherwise."""

    class Interfering(FakeSessionStore):
        async def load(self, store_id: int, session_id: str) -> SessionEnvelope | None:
            loaded = await super().load(store_id, session_id)
            from app.schemas.session import new_session

            self.saved[(store_id, session_id)] = new_session().advanced(
                state=new_session().state, conversation=new_session().conversation
            )
            return loaded

    harness = Harness(a_search(), store=Interfering())
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("show me sofas"))

    assert reply.status_code == 409
    assert reply.json()["error"]["code"] == "session_conflict"


# ── failing safely ══════════════════════════════════════════════════════════


async def test_an_unreachable_session_store_fails_rather_than_forgetting(
    client_for: Any,
) -> None:
    """Never a stateless fallback: it would reinterpret every reference."""
    harness = Harness(an_answer(), store=FakeSessionStore(error=SessionStoreUnavailableError()))
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("show me sofas"))

    assert reply.status_code == 503
    assert "redis" not in reply.text.lower()
    assert harness.supervisor.seen == []


async def test_unreadable_session_state_is_refused_not_reset(
    client_for: Any,
) -> None:
    harness = Harness(an_answer(), store=FakeSessionStore(error=SessionStateInvalidError()))
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=body("show me sofas"))

    assert reply.status_code == 409
    assert reply.json()["error"]["code"] == "session_state_invalid"


async def test_an_unknown_store_is_refused_before_any_session_work(
    client_for: Any,
) -> None:
    harness = Harness(an_answer())
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
    harness = Harness(an_answer())
    client = await client_for(harness)

    reply = await client.post("/v1/chat", json=payload)

    assert reply.status_code == 422
    assert harness.sessions.loads == 0


# ── retailer isolation ══════════════════════════════════════════════════════


async def test_one_session_id_under_two_stores_is_two_conversations(
    client_for: Any,
) -> None:
    """Structural: different keys, so it holds whatever the catalogs contain."""
    harness = Harness(an_answer(), an_answer())
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
    """The tools search the store the request resolved, never one a model named."""
    harness = Harness(a_search(), a_search())
    client = await client_for(harness)

    await client.post("/v1/chat", json={"session_id": SESSION, "store_id": STORE, "message": "hi"})
    await client.post(
        "/v1/chat",
        json={"session_id": SESSION, "store_id": OTHER_STORE, "message": "hi"},
    )

    assert [c.store_id for c in harness.pipeline.contexts] == [STORE, OTHER_STORE]


# ── one exchange, one team run ══════════════════════════════════════════════


async def test_a_second_turn_does_not_replay_the_first(client_for: Any) -> None:
    """Each request is one exchange. Nothing re-runs a committed turn."""
    harness = Harness(a_search(), an_answer())
    client = await client_for(harness)

    await client.post("/v1/chat", json=body("one"))
    await client.post("/v1/chat", json=body("two"))

    assert len(harness.supervisor.seen) == 2
    assert len(harness.sara.seen) == 3, "two calls for the search turn, one for the answer"
    assert len(harness.pipeline.calls) == 1
    assert harness.sessions.saves == 2
