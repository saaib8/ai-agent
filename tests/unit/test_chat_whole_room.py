"""A room built over several requests.

The whole-room behaviour was proved within a process in M12. What is new here
is that each step is a separate HTTP request, so everything the customer built
has to survive being written to a session and read back: the durable plan, the
need ids, the locks, the acquisitions and the bundle revision.

That is the failure this file is here to catch. A room that round-trips as
*something* but not as *itself* would keep working for a turn or two and then
resolve "keep the second one" against a room whose lines had quietly changed
identity.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.core.config import SessionSettings
from app.core.exceptions import LLMUnavailableError
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    BundleInteractionIntent,
    BundleInteractionOp,
    BundleReplacementIntent,
    BundleReplacementMode,
    CustomerAgentDecision,
)
from app.schemas.agent_state import BundleItemStatus, RoomProjectState
from app.schemas.agent_turn import CustomerResponse
from app.schemas.bundle import BundleLine, BundleStatus, RoomBundle
from app.schemas.bundle_reference import BundleItemOrdinal, DesignNeedCategoryMatch
from app.services.chat_runtime import ChatRuntime
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.test_chat_api import (
    GRAPH,
    FakeResponses,
    FakeRetailers,
    FakeSessionStore,
)
from tests.unit.test_design_revision import (
    category_need,
    chosen,
    plan_of,
    product,
)
from tests.unit.test_turn_coordinator import (
    FakeCapabilities,
    FakeDesign,
    FakeHydration,
    FakeOptimizer,
    _coordinator,
)
from tests.unit.test_whole_room_e2e import bundle_of

STORE = 50
SESSION = "room-1"


class RoomHarness:
    """A scripted conversation, one coordinator per request."""

    def __init__(
        self,
        *steps: tuple[CustomerAgentDecision, Any, Any],
        available: tuple[int, ...] = (10, 11, 77),
        responses: FakeResponses | None = None,
    ) -> None:
        self.steps = list(steps)
        self.sessions = FakeSessionStore()
        self.responses = responses or FakeResponses()
        self.available = available
        self.parts: list[Any] = []

    def _runtime(self) -> ChatRuntime:
        decision, plan, outcome = self.steps.pop(0)
        coordinator, parts = _coordinator(
            decision,
            capabilities=FakeCapabilities(
                (("seating", "sofa"), ("lighting", "floor-lamp"), ("decor", "carpet"))
            ),
            hydration=FakeHydration(available=self.available),
            design=FakeDesign(plan) if plan is not None else FakeDesign(),
            optimizer=FakeOptimizer(outcome) if outcome is not None else None,
        )
        self.parts.append(parts)
        return ChatRuntime(
            coordinator,
            self.responses,  # type: ignore[arg-type]
            self.sessions,  # type: ignore[arg-type]
            SessionSettings(max_history_messages=20),
        )

    def client(self) -> AsyncClient:
        from app.api.dependencies import (
            chat_graph,
            chat_runtime,
            retailer_context_provider,
        )
        from app.api.errors import register_exception_handlers
        from app.api.routes.chat import router

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(router, prefix="/v1")
        app.dependency_overrides[chat_runtime] = self._runtime
        # The real compiled graph: these tests drive the runtime through it.
        app.dependency_overrides[chat_graph] = lambda: GRAPH
        app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers({STORE})
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    def room(self) -> RoomProjectState:
        project = self.sessions.saved[(STORE, SESSION)].state.room_project
        assert isinstance(project, RoomProjectState)
        return project


def say(message: str, **extra: Any) -> dict[str, Any]:
    return {"session_id": SESSION, "store_id": STORE, "message": message, **extra}


def a_handoff() -> CustomerAgentDecision:
    return CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF)


def a_refinement(op: BundleInteractionOp, **kwargs: Any) -> CustomerAgentDecision:
    kwargs.setdefault("selector", BundleItemOrdinal(ordinal=1))
    return CustomerAgentDecision(
        action=AgentAction.BUNDLE_REFINE,
        bundle_interaction=BundleInteractionIntent(op=op, **kwargs),
    )


# ── the room, built across requests ═════════════════════════════════════════


async def test_a_room_survives_being_written_and_read_back() -> None:
    """Two roles planned in one request are two roles in the next."""
    plan = plan_of(category_need("seating", "sofa"), category_need("lighting", "floor-lamp"))
    harness = RoomHarness(
        (a_handoff(), plan, bundle_of((10, 0, "1000.00"), (11, 1, "400.00"))),
        (a_refinement(BundleInteractionOp.LOCK), None, None),
    )

    async with harness.client() as client:
        first = await client.post("/v1/chat", json=say("furnish my living room"))
        assert first.json()["presentation"]["room"] is not None

        await client.post("/v1/chat", json=say("keep the first one"))

    # The second request's coordinator was handed the room the first committed.
    turn = harness.responses.calls[1][0]
    room = turn.state.room_project
    assert [n.commerce_category for n in room.design_needs] == ["seating", "lighting"]
    assert [i.product_id for i in room.bundle_items] == [10, 11]


async def test_need_ids_are_the_same_ones_after_a_round_trip() -> None:
    """Identity, not just shape: a reference made next turn must land on the
    same durable role."""
    plan = plan_of(category_need("seating", "sofa"))
    harness = RoomHarness(
        (a_handoff(), plan, chosen(10)),
        (a_refinement(BundleInteractionOp.LOCK), None, None),
    )

    async with harness.client() as client:
        await client.post("/v1/chat", json=say("furnish my living room"))
        after_first = [n.need_id for n in harness.room().design_needs]
        line_ids = [i.line_id for i in harness.room().bundle_items]

        await client.post("/v1/chat", json=say("keep that sofa"))

    assert [n.need_id for n in harness.room().design_needs] == after_first
    assert [i.line_id for i in harness.room().bundle_items] == line_ids


async def test_a_lock_placed_in_one_request_holds_in_the_next() -> None:
    plan = plan_of(category_need("seating", "sofa"))
    # The replan's outcome carries the lock, as a real optimiser's always does:
    # a locked line is an input it must place, not one it may omit.
    replanned = RoomBundle(
        lines=(
            BundleLine(
                need_index=None,
                product=product(10),
                quantity=1,
                locked=True,
                acquisition=BundleAcquisition.TO_BUY,
                relaxation_depth=None,
            ),
            BundleLine(
                need_index=0,
                product=product(77, category="decor", subcategory="carpet"),
                quantity=1,
                locked=False,
                acquisition=BundleAcquisition.TO_BUY,
                relaxation_depth=0,
            ),
        ),
        status=BundleStatus.COMPLETE,
        new_spend_total=Decimal("2000.00"),
        currency="SAR",
    )
    harness = RoomHarness(
        (a_handoff(), plan, chosen(10)),
        (a_refinement(BundleInteractionOp.LOCK), None, None),
        (a_handoff(), plan_of(category_need("decor", "carpet")), replanned),
    )

    async with harness.client() as client:
        await client.post("/v1/chat", json=say("furnish my living room"))
        await client.post("/v1/chat", json=say("keep that sofa"))
        assert harness.room().bundle_items[0].status is BundleItemStatus.LOCKED

        await client.post("/v1/chat", json=say("add a rug"))

    kept = [i for i in harness.room().bundle_items if i.product_id == 10]
    assert len(kept) == 1, "the locked piece survived the replan"
    assert kept[0].status is BundleItemStatus.LOCKED
    assert kept[0].need_id is None, "its old role is gone, its identity is not"


async def test_what_they_already_own_is_remembered_between_requests() -> None:
    plan = plan_of(category_need("seating", "sofa"))
    harness = RoomHarness(
        (a_handoff(), plan, chosen(10)),
        (
            a_refinement(
                BundleInteractionOp.SET_ACQUISITION,
                acquisition=BundleAcquisition.ALREADY_OWNED,
            ),
            None,
            None,
        ),
        (CustomerAgentDecision(action=AgentAction.ANSWER), None, None),
    )

    async with harness.client() as client:
        await client.post("/v1/chat", json=say("furnish my living room"))
        await client.post("/v1/chat", json=say("I already own that sofa"))
        await client.post("/v1/chat", json=say("what else do I need?"))

    line = harness.room().bundle_items[0]
    assert line.acquisition is BundleAcquisition.ALREADY_OWNED
    assert line.status is BundleItemStatus.LOCKED, "owning it fixes it in the room"


async def test_replacing_a_product_across_requests_keeps_the_role() -> None:
    plan = plan_of(category_need("seating", "sofa"))
    harness = RoomHarness(
        (a_handoff(), plan, chosen(10)),
        (
            a_refinement(
                BundleInteractionOp.REPLACE_PRODUCT,
                replacement=BundleReplacementIntent(mode=BundleReplacementMode.ALTERNATIVE),
            ),
            None,
            chosen(77),
        ),
    )

    async with harness.client() as client:
        await client.post("/v1/chat", json=say("furnish my living room"))
        original = [n.need_id for n in harness.room().design_needs]

        await client.post("/v1/chat", json=say("show me another sofa"))

    assert [n.need_id for n in harness.room().design_needs] == original
    assert [i.product_id for i in harness.room().bundle_items] == [77]


async def test_removing_a_role_across_requests_leaves_the_rest() -> None:
    plan = plan_of(category_need("seating", "sofa"), category_need("lighting", "floor-lamp"))
    harness = RoomHarness(
        (a_handoff(), plan, bundle_of((10, 0, "1000.00"), (11, 1, "400.00"))),
        (
            CustomerAgentDecision(
                action=AgentAction.BUNDLE_REFINE,
                bundle_interaction=BundleInteractionIntent(
                    op=BundleInteractionOp.REMOVE_NEED,
                    need_selector=DesignNeedCategoryMatch(commerce_category="lighting"),
                ),
            ),
            None,
            chosen(10),
        ),
    )

    async with harness.client() as client:
        await client.post("/v1/chat", json=say("furnish my living room"))
        await client.post("/v1/chat", json=say("take the lamp out"))

    assert [n.commerce_category for n in harness.room().design_needs] == ["seating"]


async def test_the_bundle_revision_moves_with_the_room_not_the_session() -> None:
    """Two different counters: one describes the room, one the session record.

    The session revision advances on every persisted turn; the bundle revision
    only when the room's content changes. A turn that changes nothing about the
    room still persists a conversation.
    """
    plan = plan_of(category_need("seating", "sofa"))
    harness = RoomHarness(
        (a_handoff(), plan, chosen(10)),
        (CustomerAgentDecision(action=AgentAction.ANSWER), None, None),
    )

    async with harness.client() as client:
        first = await client.post("/v1/chat", json=say("furnish my living room"))
        bundle_after_first = harness.room().bundle_revision

        second = await client.post("/v1/chat", json=say("thanks, that looks good"))

    assert first.json()["session_revision"] == 1
    assert second.json()["session_revision"] == 2, "the session moved"
    assert harness.room().bundle_revision == bundle_after_first, "the room did not"


async def test_the_room_the_client_is_shown_is_the_room_that_was_stored() -> None:
    """Card order comes from committed state, so "the second one" next turn
    means the second card they were shown."""
    plan = plan_of(category_need("seating", "sofa"), category_need("lighting", "floor-lamp"))
    harness = RoomHarness(
        (a_handoff(), plan, bundle_of((10, 0, "1000.00"), (11, 1, "400.00"))),
    )

    async with harness.client() as client:
        payload = (await client.post("/v1/chat", json=say("furnish my living room"))).json()

    shown = payload["presentation"]["room"]["items"]
    assert [item["grounding_ref"] for item in shown] == [1, 2]
    assert [i.product_id for i in harness.room().bundle_items] == [10, 11]
    assert shown[0]["unit_price"] == "1000.00"
    assert payload["presentation"]["room"]["totals"]["new_spend_total"] == "1400.00"


# ── the reply that had to fall back ═════════════════════════════════════════


class FailingResponses:
    """The response model is unavailable; the deterministic wording stands in.

    This is what `CustomerResponseGenerator` already does internally. The double
    exists so the test can be explicit about *which* text was persisted.
    """

    FALLBACK = "I have put a room together for you."

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def generate(self, turn: Any, result: Any) -> CustomerResponse:
        self.calls.append((turn, result))
        return CustomerResponse(message=self.FALLBACK)


async def test_a_fallback_reply_is_what_gets_stored_and_returned() -> None:
    """What actually happened is what is persisted (M13 12, 41).

    The turn succeeded and the room is real; only the wording fell back. So the
    room commits, the fallback text becomes the assistant's line in history,
    the revision advances, and the next turn reads that as truth.
    """
    plan = plan_of(category_need("seating", "sofa"))
    harness = RoomHarness(
        (a_handoff(), plan, chosen(10)),
        (CustomerAgentDecision(action=AgentAction.ANSWER), None, None),
        responses=FailingResponses(),  # type: ignore[arg-type]
    )

    async with harness.client() as client:
        first = await client.post("/v1/chat", json=say("furnish my living room"))
        second = await client.post("/v1/chat", json=say("thanks"))

    assert first.json()["response"]["message"] == FailingResponses.FALLBACK
    assert first.json()["session_revision"] == 1

    # The room committed, and the fallback is the history the next turn saw.
    assert [i.product_id for i in harness.room().bundle_items] == [10]
    turn = harness.responses.calls[1][0]
    assert [m.content for m in turn.conversation.messages] == [
        "furnish my living room",
        FailingResponses.FALLBACK,
    ]
    assert second.json()["session_revision"] == 2


async def test_a_turn_that_failed_before_a_reply_persists_nothing() -> None:
    """A provider failure with no valid decision leaves the session untouched.

    Not an empty turn recorded in history: the customer said something, nothing
    happened, and a retry should start exactly where they were (M13 13).
    """
    from tests.unit.test_turn_coordinator import FakeDecisions

    harness = RoomHarness((a_handoff(), None, None))

    def broken_runtime() -> ChatRuntime:
        coordinator, _ = _coordinator(
            a_handoff(),
            decision_error=LLMUnavailableError(provider="openai"),
        )
        return ChatRuntime(
            coordinator,
            harness.responses,  # type: ignore[arg-type]
            harness.sessions,  # type: ignore[arg-type]
            SessionSettings(),
        )

    from app.api.dependencies import (
        chat_graph,
        chat_runtime,
        retailer_context_provider,
    )
    from app.api.errors import register_exception_handlers
    from app.api.routes.chat import router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[chat_runtime] = broken_runtime
    app.dependency_overrides[chat_graph] = lambda: GRAPH
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers({STORE})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reply = await client.post("/v1/chat", json=say("furnish my living room"))

    assert reply.status_code >= 400
    assert harness.sessions.saved == {}, "no state, no history, no revision"
    assert FakeDecisions is not None
