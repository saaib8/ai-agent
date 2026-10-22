"""The graph, and how little it is allowed to do.

LangGraph's job here is sequencing. Every test in this file is really the same
assertion from a different angle: that a node calls one runtime phase and
records what it returned, and that nothing about a customer's intent is decided
in orchestration.

The topology is linear on purpose. Branching would mean the graph had opinions
about what a turn should do, and the agents already have those - tested
where the reasoning lives rather than where it is scheduled.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
from app.core.exceptions import SessionConflictError, SessionStoreUnavailableError
from app.orchestration.graph import (
    BUILD_PUBLIC_RESPONSE,
    LOAD_SESSION,
    NODE_ORDER,
    PERSIST_SESSION,
    RUN_AGENTS,
    ChatGraphContext,
    ChatGraphRunner,
    build_chat_graph,
)
from app.orchestration.state import ChatGraphState
from app.schemas.agent_persona import AgentPersona
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.retailer import RetailerContext
from app.services.chat_runtime import ChatRuntime
from langgraph.graph import END, START

from tests.unit.test_chat_api import FakeSessionStore, Harness, a_search

GRAPH_MODULE = Path(__file__).parents[2] / "app/orchestration/graph.py"
STATE_MODULE = Path(__file__).parents[2] / "app/orchestration/state.py"

STORE = 50
REQUEST = ChatRequest(session_id="s-1", store_id=STORE, message="show me sofas")
CONTEXT = RetailerContext(store_id=STORE)


def a_runtime(*, sessions: FakeSessionStore | None = None) -> tuple[ChatRuntime, Harness]:
    harness = Harness(a_search(), store=sessions)
    return harness._runtime(), harness


# ── topology ════════════════════════════════════════════════════════════════


def test_the_graph_has_exactly_the_four_phases() -> None:
    """One node per phase. A fifth would be a phase the runtime does not have."""
    graph = build_chat_graph()

    assert set(graph.nodes) == set(NODE_ORDER)
    assert len(NODE_ORDER) == 4


def test_the_phases_run_in_the_one_safe_order() -> None:
    """Load before spending, persist before answering.

    Each of these edges is a rule: refuse a stale client before paying a
    provider, store what the customer was actually told, and never return a
    reply that could not be committed (M13 26, 32, 33).
    """
    assert NODE_ORDER == (LOAD_SESSION, RUN_AGENTS, PERSIST_SESSION, BUILD_PUBLIC_RESPONSE)


def test_the_graph_is_a_straight_line() -> None:
    """No conditional edges. A branch here would be orchestration deciding
    something about the customer's intent, which is the agents' job."""
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
        "AgentTeam",
        "AgentLoop",
        "AgentServices",
        "InteriorDesignAgent",
        "BundleOptimizer",
        "ProductSearchPipeline",
        "SessionStore",
        "CommerceTaxonomy",
        "ProductRepository",
    ],
)
def test_the_graph_reaches_no_domain_service(forbidden: str) -> None:
    """It knows one collaborator - the runtime - and four method names."""
    assert forbidden not in _identifiers(GRAPH_MODULE), forbidden


def test_every_node_calls_exactly_one_phase() -> None:
    """A node needing two calls is a phase the runtime is missing."""
    tree = ast.parse(GRAPH_MODULE.read_text())
    phases = {"load_session", "run_agents", "persist", "public_response"}

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or not node.name.startswith("_"):
            continue
        called = {
            call.func.attr
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        }
        assert len(called & phases) == 1, f"{node.name} calls {called & phases}"


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
    """Rebuilding four nodes for every customer message would be waste with no
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
    assert reply.agent is AgentPersona.SARA
    assert reply.presentation is not None and len(reply.presentation.products) == 3


async def test_a_run_adds_no_model_work() -> None:
    """Orchestration adds no provider work: one supervisor call, and the
    agent's own two (the search, then the reply)."""
    runtime, harness = a_runtime()

    await ChatGraphRunner().run(runtime, REQUEST, CONTEXT)

    assert len(harness.supervisor.seen) == 1
    assert len(harness.sara.seen) == 2


async def test_a_failure_in_a_node_stops_the_walk() -> None:
    """Typed exceptions propagate; there is no error edge re-encoding them.

    Nothing after the failing node runs, so no agent works and nothing is
    persisted from a turn that did not happen.
    """
    sessions = FakeSessionStore(error=SessionStoreUnavailableError())
    runtime, harness = a_runtime(sessions=sessions)

    with pytest.raises(SessionStoreUnavailableError):
        await ChatGraphRunner().run(runtime, REQUEST, CONTEXT)

    assert harness.supervisor.seen == [], "the turn never ran"
    assert sessions.saves == 0


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
