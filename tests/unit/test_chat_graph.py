"""The graph, and how little it is allowed to do.

LangGraph's job here is sequencing. Every test in this file is really the same
assertion from a different angle: that a node calls one runtime phase and
records what it returned, and that nothing about a customer's intent is decided
in orchestration.

The topology is linear on purpose. Branching would mean the graph had opinions
about what a turn should do, and the coordinator already has those - tested
where the reasoning lives rather than where it is scheduled.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
from app.core.config import SessionSettings
from app.core.exceptions import SessionConflictError, SessionStoreUnavailableError
from app.orchestration.graph import (
    BUILD_PUBLIC_RESPONSE,
    LOAD_SESSION,
    NODE_ORDER,
    PERSIST_SESSION,
    RENDER_CUSTOMER_RESPONSE,
    RUN_CUSTOMER_TURN,
    ChatGraphContext,
    ChatGraphRunner,
    build_chat_graph,
)
from app.orchestration.state import ChatGraphState
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.retailer import RetailerContext
from app.services.chat_runtime import ChatRuntime
from langgraph.graph import END, START

from tests.unit.test_chat_api import (
    FakeResponses,
    FakeSessionStore,
)
from tests.unit.test_turn_coordinator import FakeHydration, FakePipeline, _coordinator, _resolved

GRAPH_MODULE = Path(__file__).parents[2] / "app/orchestration/graph.py"
STATE_MODULE = Path(__file__).parents[2] / "app/orchestration/state.py"

STORE = 50
REQUEST = ChatRequest(session_id="s-1", store_id=STORE, message="show me sofas")
CONTEXT = RetailerContext(store_id=STORE)


def a_runtime(
    *,
    sessions: FakeSessionStore | None = None,
    responses: FakeResponses | None = None,
    decision: CustomerAgentDecision | None = None,
) -> tuple[ChatRuntime, dict[str, Any]]:
    coordinator, parts = _coordinator(
        decision or CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(),
        pipeline=FakePipeline(ids=(10, 11)),
        hydration=FakeHydration(available=(10, 11)),
    )
    return (
        ChatRuntime(
            coordinator,
            responses or FakeResponses(),  # type: ignore[arg-type]
            sessions or FakeSessionStore(),  # type: ignore[arg-type]
            SessionSettings(),
        ),
        parts,
    )


# ── topology ════════════════════════════════════════════════════════════════


def test_the_graph_has_exactly_the_five_phases() -> None:
    """One node per phase. A sixth would be a phase the runtime does not have."""
    graph = build_chat_graph()

    assert set(graph.nodes) == set(NODE_ORDER)
    assert len(NODE_ORDER) == 5


def test_the_phases_run_in_the_one_safe_order() -> None:
    """Load before spending, persist before answering.

    Each of these edges is a rule: refuse a stale client before paying a
    provider, store what the customer was actually told, and never return a
    reply that could not be committed (M13 26, 32, 33).
    """
    assert NODE_ORDER == (
        LOAD_SESSION,
        RUN_CUSTOMER_TURN,
        RENDER_CUSTOMER_RESPONSE,
        PERSIST_SESSION,
        BUILD_PUBLIC_RESPONSE,
    )


def test_the_graph_is_a_straight_line() -> None:
    """No conditional edges. A branch here would be orchestration deciding
    something about the customer's intent, which is the coordinator's job."""
    compiled = build_chat_graph().compile()
    graph = compiled.get_graph()

    outgoing: dict[str, list[str]] = {}
    for edge in graph.edges:
        outgoing.setdefault(edge.source, []).append(edge.target)

    for source, targets in outgoing.items():
        assert len(targets) == 1, f"{source} branches to {targets}"
    assert outgoing[START] == [LOAD_SESSION]
    assert outgoing[BUILD_PUBLIC_RESPONSE] == [END]


def test_no_conditional_edge_is_declared() -> None:
    source = GRAPH_MODULE.read_text()

    assert "add_conditional_edges" not in source


# ── the graph is not a reasoning layer ══════════════════════════════════════


def _identifiers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.Attribute)
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        "CustomerAgentDecision",
        "AgentAction",
        "CustomerTurnCoordinator",
        "InteriorDesignAgent",
        "BundleOptimizer",
        "ProductSearchPipeline",
        "SessionStore",
        "route_response",
        "CommerceTaxonomy",
        "ProductRepository",
    ],
)
def test_the_graph_reaches_no_domain_service(forbidden: str) -> None:
    """It knows one collaborator - the runtime - and five method names."""
    assert forbidden not in _identifiers(GRAPH_MODULE), forbidden


def test_every_node_calls_exactly_one_phase() -> None:
    """A node needing two calls is a phase the runtime is missing."""
    tree = ast.parse(GRAPH_MODULE.read_text())
    phases = {
        "load_session",
        "turn_input",
        "run_turn",
        "render",
        "presentation",
        "persist",
        "public_response",
    }

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or not node.name.startswith("_"):
            continue
        called = {
            call.func.attr
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        }
        # `turn_input` is a pure builder, so the run node legitimately pairs it
        # with the call it builds for.
        assert len(called & phases) <= 2, f"{node.name} calls {called & phases}"


def test_the_state_carries_no_service_or_provider_object() -> None:
    """Execution data only. The runtime travels as invocation context, so no
    node's *input* includes a live collaborator (M13 2)."""
    annotations = ChatGraphState.__annotations__

    rendered = " ".join(str(value) for value in annotations.values())
    for forbidden in ("ChatRuntime", "Redis", "Session", "Repository", "Client"):
        assert forbidden not in rendered.replace("LoadedSession", ""), forbidden


def test_the_runtime_is_invocation_context_not_state() -> None:
    assert "runtime" not in ChatGraphState.__annotations__
    assert ChatGraphContext.__dataclass_fields__.keys() == {"chat"}


def test_the_state_records_no_reasoning() -> None:
    """No scratchpad, no plan the model wrote itself, no chain of thought."""
    for forbidden in ("thought", "scratch", "reasoning", "plan", "memory"):
        assert forbidden not in " ".join(ChatGraphState.__annotations__), forbidden


# ── compiling ═══════════════════════════════════════════════════════════════


def test_the_graph_is_compiled_once_per_runner() -> None:
    """Rebuilding five nodes for every customer message would be waste with no
    upside: the graph has no per-request content (M13 3)."""
    runner = ChatGraphRunner()

    first = runner._compiled
    second = runner._compiled

    assert first is second


def test_the_runner_takes_the_runtime_per_invocation() -> None:
    """The runtime holds a per-request database session, so it cannot be
    captured at compile time."""
    parameters = [
        name for name in inspect.signature(ChatGraphRunner.run).parameters if name != "self"
    ]

    assert parameters == ["runtime", "request", "context"]
    assert inspect.signature(ChatGraphRunner.__init__).parameters.keys() == {"self"}


# ── running it ══════════════════════════════════════════════════════════════


async def test_a_run_produces_the_public_reply() -> None:
    runtime, _ = a_runtime()

    reply = await ChatGraphRunner().run(runtime, REQUEST, CONTEXT)

    assert isinstance(reply, ChatResponse)
    assert reply.session_revision == 1
    assert reply.session_id == "s-1"


async def test_a_run_asks_each_model_once() -> None:
    """Orchestration adds no provider work (M13 43)."""
    responses = FakeResponses()
    runtime, parts = a_runtime(responses=responses)

    await ChatGraphRunner().run(runtime, REQUEST, CONTEXT)

    assert len(parts["decisions"].inputs) == 1
    assert len(responses.calls) == 1


async def test_a_failure_in_a_node_stops_the_walk() -> None:
    """Typed exceptions propagate; there is no error edge re-encoding them.

    Nothing after the failing node runs, so nothing is persisted and nothing
    is worded from a turn that did not happen.
    """
    responses = FakeResponses()
    sessions = FakeSessionStore(error=SessionStoreUnavailableError())
    runtime, parts = a_runtime(sessions=sessions, responses=responses)

    with pytest.raises(SessionStoreUnavailableError):
        await ChatGraphRunner().run(runtime, REQUEST, CONTEXT)

    assert parts["decisions"].inputs == [], "the turn never ran"
    assert responses.calls == [], "nothing was worded"


async def test_a_conflict_at_persist_discards_the_computed_reply() -> None:
    """The last node never runs, so no reply is returned for a session that
    moved on underneath this one."""
    sessions = FakeSessionStore()
    runtime, _ = a_runtime(sessions=sessions)
    await ChatGraphRunner().run(runtime, REQUEST, CONTEXT)

    # A second exchange that loads revision 1 but finds 2 by the time it writes.
    class Overtaken(FakeSessionStore):
        async def save_if_revision(self, *args: Any, **kwargs: Any) -> bool:
            return False

    overtaken = Overtaken()
    overtaken.saved = dict(sessions.saved)
    second, _ = a_runtime(sessions=overtaken)

    with pytest.raises(SessionConflictError):
        await ChatGraphRunner().run(second, REQUEST, CONTEXT)


async def test_the_walk_visits_every_node_in_order() -> None:
    """Observed, not asserted from the edge list: this is the order the nodes
    actually ran in."""
    runner = ChatGraphRunner()
    runtime, _ = a_runtime()

    visited: list[str] = []
    async for chunk in runner._compiled.astream(
        {"request": REQUEST, "context": CONTEXT},
        context=ChatGraphContext(chat=runtime),
        stream_mode="updates",
    ):
        visited.extend(chunk)

    assert visited == list(NODE_ORDER)
