"""Runtime sequencing for one chat exchange.

A thin orchestrator, and thin is the design rather than an apology for it. Every
node here does one thing: call a `ChatRuntime` phase and record what it
returned. There is no branching on the customer's intent, no action routing, no
taxonomy, no search, no ranking and no optimisation - all of that is
deterministic domain code that was built and proved without a graph, and moving
any of it into a node would hide it inside orchestration (CLAUDE.md 18).

So the graph is linear, which is the honest shape: a turn loads, runs, is
worded, is persisted and is answered. What varies inside a turn already varies
inside the coordinator, where it is tested.

Failure needs no edges either. Every deliberate failure in this service is a
typed exception carrying its own status and public message, so a node that
cannot proceed raises and the registered handler answers. Encoding the same
outcomes as conditional edges would be a second, weaker copy of that.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.orchestration.state import ChatGraphState
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.retailer import RetailerContext
from app.services.chat_runtime import ChatRuntime

LOAD_SESSION = "load_session"
RUN_CUSTOMER_TURN = "run_customer_turn"
RENDER_CUSTOMER_RESPONSE = "render_customer_response"
PERSIST_SESSION = "persist_session"
BUILD_PUBLIC_RESPONSE = "build_public_response"

NODE_ORDER = (
    LOAD_SESSION,
    RUN_CUSTOMER_TURN,
    RENDER_CUSTOMER_RESPONSE,
    PERSIST_SESSION,
    BUILD_PUBLIC_RESPONSE,
)
"""The only order these phases may run in.

Loading precedes everything because a stale client revision must be refused
before any provider is paid. Persisting follows wording because what is stored
is the reply the customer was actually given. Answering follows persisting
because a reply that could not be committed describes a session that no longer
exists (M13 26, 32, 33).
"""


@dataclass(frozen=True, slots=True)
class ChatGraphContext:
    """What one invocation is driven with.

    The runtime is per-request - it holds a database session - while the graph
    is per-process. So it travels as invocation context rather than as graph
    state: a service object is not something a turn *produced*, and putting it
    in the state would make every node's input include a live collaborator
    (M13 2, 3).
    """

    chat: ChatRuntime


ChatStateGraph = StateGraph[ChatGraphState, ChatGraphContext, ChatGraphState, ChatGraphState]
"""The graph's four type parameters, written out once.

Spelling them makes the context schema part of the type rather than something
inferred as `None`, which is what a bare `StateGraph` annotation would give -
and the nodes would then typecheck against a context they cannot receive.
"""


def build_chat_graph() -> ChatStateGraph:
    """The exchange as five nodes, uncompiled. Shape only, no dependencies."""
    graph: ChatStateGraph = StateGraph(ChatGraphState, context_schema=ChatGraphContext)

    graph.add_node(LOAD_SESSION, _load_session)
    graph.add_node(RUN_CUSTOMER_TURN, _run_customer_turn)
    graph.add_node(RENDER_CUSTOMER_RESPONSE, _render_customer_response)
    graph.add_node(PERSIST_SESSION, _persist_session)
    graph.add_node(BUILD_PUBLIC_RESPONSE, _build_public_response)

    graph.add_edge(START, LOAD_SESSION)
    for current, following in pairwise(NODE_ORDER):
        graph.add_edge(current, following)
    graph.add_edge(BUILD_PUBLIC_RESPONSE, END)
    return graph


class ChatGraphRunner:
    """A compiled graph, and the runtime each invocation drives it with.

    Compiled once at startup and reused: building a graph per request would
    rebuild the same five nodes for every customer message (M13 3).
    """

    def __init__(self) -> None:
        self._compiled = build_chat_graph().compile()

    async def run(
        self, runtime: ChatRuntime, request: ChatRequest, context: RetailerContext
    ) -> ChatResponse:
        """One exchange, through the graph.

        The runtime travels in the invocation rather than being captured at
        compile time, because it holds a per-request database session.
        """
        final = await self._compiled.ainvoke(
            {"request": request, "context": context},
            context=ChatGraphContext(chat=runtime),
        )
        reply = final["reply"]
        assert isinstance(reply, ChatResponse), "the last node builds the reply"
        return reply


# ── the nodes ───────────────────────────────────────────────────────────────
#
# Each calls exactly one phase. If a node ever needs a second call to say what
# it did, that is a phase the runtime is missing rather than logic to put here.


async def _load_session(
    state: ChatGraphState, runtime: Runtime[ChatGraphContext]
) -> dict[str, object]:
    return {"session": await runtime.context.chat.load_session(state["request"])}


async def _run_customer_turn(
    state: ChatGraphState, runtime: Runtime[ChatGraphContext]
) -> dict[str, object]:
    engine = runtime.context.chat
    turn = engine.turn_input(state["request"], state["context"], state["session"])
    return {"turn": turn, "result": await engine.run_turn(turn)}


async def _render_customer_response(
    state: ChatGraphState, runtime: Runtime[ChatGraphContext]
) -> dict[str, object]:
    engine = runtime.context.chat
    result = state["result"]
    return {
        "response": await engine.render(state["turn"], result),
        "presentation": engine.presentation(result),
    }


async def _persist_session(
    state: ChatGraphState, runtime: Runtime[ChatGraphContext]
) -> dict[str, object]:
    return {
        "revision": await runtime.context.chat.persist(
            state["request"], state["session"], state["result"], state["response"]
        )
    }


async def _build_public_response(
    state: ChatGraphState, runtime: Runtime[ChatGraphContext]
) -> dict[str, object]:
    return {
        "reply": runtime.context.chat.public_response(
            state["request"],
            state["revision"],
            state["response"],
            state["presentation"],
        )
    }
