"""What the runtime layer may and may not do.

M13 added the first things in this service that a customer touches directly: a
public route and a store of conversations. Both are places where the careful
boundaries of M11 and M12 could quietly be undone - by a route that starts
deciding, by an orchestrator that starts reasoning, or by a session payload
that carries identity into a prompt.

These guards replace the per-milestone pins that said "no chat route exists
yet". That claim expired; the ones here do not.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from app.schemas.agent_turn import DecisionInput
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.session import SessionEnvelope
from app.services.chat_runtime import ChatRuntime
from pydantic import BaseModel

APP = Path(__file__).parents[2] / "app"
ROUTE = APP / "api/routes/chat.py"
RUNTIME = APP / "services/chat_runtime.py"
STORE = APP / "repositories/sessions.py"


def _identifiers(path: Path) -> set[str]:
    """Names a module actually uses - not words in its prose."""
    tree = ast.parse(path.read_text())
    return {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.Attribute)
    }


# ── the route is transport ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "forbidden",
    [
        "ProductRepository",
        "ProductSearchPipeline",
        "CustomerAgentDecisionService",
        "InteriorDesignAgent",
        "BundleOptimizer",
        "CustomerResponseGenerator",
        "SessionStore",
        "Redis",
        "route_response",
        "AgentStateV1",
    ],
)
def test_the_route_reaches_no_domain_service(forbidden: str) -> None:
    """Validate, resolve scope, delegate, return. Nothing else."""
    assert forbidden not in _identifiers(ROUTE), forbidden


def test_the_route_is_small_enough_to_be_obviously_transport() -> None:
    """A size check is crude, but a route that grows past a few statements has
    started doing something, and the thing it does is business logic."""
    tree = ast.parse(ROUTE.read_text())
    handler = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef))
    statements = [s for s in handler.body if not isinstance(s, ast.Expr)]

    assert len(statements) <= 3, "the handler is doing more than delegating"


def test_the_route_catches_nothing() -> None:
    """Every deliberate failure carries its own status and public message, so a
    handler that caught one could only make the answer less accurate."""
    tree = ast.parse(ROUTE.read_text())

    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Try)]


# ── the runtime orchestrates; it does not reason ────────────────────────────


@pytest.mark.parametrize(
    "forbidden",
    [
        "CustomerAgentDecision",
        "AgentAction",
        "InteriorDesignAgent",
        "InteriorDesignRequest",
        "BundleOptimizer",
        "ProductSearchPipeline",
        "CommerceTaxonomy",
        "route_response",
    ],
)
def test_the_runtime_makes_no_decision_of_its_own(forbidden: str) -> None:
    """It sequences phases. Which action a turn takes was settled in M11B, and
    how a reply is routed was settled in M11B-6 (M13 1, 22, 23)."""
    assert forbidden not in _identifiers(RUNTIME), forbidden


def test_the_runtime_holds_only_the_four_things_it_sequences() -> None:
    """A collaborator list is the cheapest description of what a layer does.

    The domain engine, the wording layer, the session store and the policy that
    bounds them. A repository, a taxonomy or a provider client appearing here
    would mean orchestration had started doing the work it orchestrates.
    """
    parameters = [
        name for name in inspect.signature(ChatRuntime.__init__).parameters if name != "self"
    ]

    assert parameters == ["coordinator", "responses", "sessions", "settings"]


def test_the_runtime_exposes_the_five_phases_a_graph_sequences() -> None:
    """Named phases, so orchestration wires them rather than reimplementing
    them - and so each is testable without a graph (M13 1)."""
    for phase in (
        "load_session",
        "turn_input",
        "run_turn",
        "render",
        "presentation",
        "persist",
        "public_response",
    ):
        assert hasattr(ChatRuntime, phase), phase


def test_the_runtime_calls_the_coordinator_exactly_once_per_turn() -> None:
    source = RUNTIME.read_text()

    assert source.count("self._coordinator.run(") == 1


def test_the_runtime_generates_one_response_per_turn() -> None:
    source = RUNTIME.read_text()

    assert source.count("self._responses.generate(") == 1


def test_the_coordinator_still_persists_nothing() -> None:
    """The engine returns the next state; the runtime stores it. If that ever
    reversed, a domain service would own a Redis connection."""
    coordinator = _identifiers(APP / "services/turn_coordinator.py")

    for forbidden in ("SessionStore", "SessionEnvelope", "RedisClient"):
        assert forbidden not in coordinator, forbidden


# ── session identity never reaches a model ──────────────────────────────────


def _reachable_names(model: type[BaseModel], seen: set[type] | None = None) -> set[str]:
    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)

    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        annotation = field.annotation
        for nested in _nested(annotation):
            names |= _reachable_names(nested, seen)
    return names


def _nested(annotation: object) -> list[type[BaseModel]]:
    from typing import get_args

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    for argument in get_args(annotation):
        found.extend(_nested(argument))
    return found


@pytest.mark.parametrize(
    "forbidden",
    ["session_id", "session_revision", "store_id", "envelope_version", "expected_"],
)
def test_no_runtime_identity_reaches_the_decision_model(forbidden: str) -> None:
    """`DecisionInput` is the whole of what that model sees.

    A session revision is a concurrency token. A model that could see one could
    start reasoning about it, and one that could emit one would be authoring
    runtime state (M13 44).
    """
    for name in _reachable_names(DecisionInput):
        assert forbidden not in name, f"DecisionInput.{name}"


def test_the_session_envelope_is_not_a_model_input() -> None:
    """Nothing a model reads is assembled from the stored envelope.

    The state inside it is projected to `AgentStateView` first, and the
    conversation travels as `ConversationContext`; the envelope itself, with
    its revision, goes nowhere near a prompt.
    """
    for module in (APP / "services").rglob("*.py"):
        source = module.read_text()
        if "SessionEnvelope" not in source:
            continue
        # Only the runtime handles envelopes, and it hands models neither.
        assert module.name == "chat_runtime.py", module.name


def test_the_envelope_carries_no_scope_of_its_own() -> None:
    """Scope lives in the key. A copy in the value could disagree with it."""
    names = set(SessionEnvelope.model_fields)

    assert "store_id" not in names
    assert "session_id" not in names


# ── the public contract ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "forbidden",
    [
        "product_id",
        "line_id",
        "need_id",
        "store_id",
        "bundle_revision",
        "rejected_product_ids",
        "schema_version",
        "prompt",
        "model",
    ],
)
def test_no_internal_identity_is_in_the_public_response(forbidden: str) -> None:
    for name in _reachable_names(ChatResponse):
        assert forbidden not in name, f"ChatResponse.{name}"


def test_the_public_response_still_carries_what_a_client_needs() -> None:
    """The guard above must not be passing by exposing nothing."""
    names = _reachable_names(ChatResponse)

    assert {"session_revision", "message", "products", "price_amount"} <= names


def test_the_request_asks_for_nothing_that_does_not_exist_yet() -> None:
    """No auth fields before there is authentication to check them."""
    names = set(ChatRequest.model_fields)

    assert names == {
        "session_id",
        "store_id",
        "message",
        "expected_session_revision",
    }


# ── what M13 still does not build ───────────────────────────────────────────


@pytest.mark.parametrize("forbidden", ["long_term_memory", "conversation_archive", "transcript"])
def test_no_archive_or_long_term_memory_is_implemented(forbidden: str) -> None:
    """V1 keeps a short technical session and nothing else (CLAUDE.md 19).

    Identifiers, not prose. And narrowly chosen: "summarise" was too broad to
    mean anything here - `search_pipeline._summarise` builds a deterministic
    relaxation summary and is not a conversation summariser. What actually
    rules that out is `test_history_is_bounded_without_a_model` below, which
    checks the runtime holds no provider at all.
    """
    for module in APP.rglob("*.py"):
        names = {name.lower() for name in _identifiers(module)}
        assert not {name for name in names if forbidden in name}, f"{module.name}: {forbidden}"


def test_no_object_store_client_is_imported() -> None:
    """No S3 conversation archive in V1. boto3 exists for Secrets Manager
    only, and only in stage and prod."""
    for module in APP.rglob("*.py"):
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node).startswith(
                ("boto3.client('s3'", 'boto3.client("s3"')
            ):
                pytest.fail(f"{module.name} builds an S3 client")


def test_history_is_bounded_without_a_model() -> None:
    """Trimming is a slice. A summariser would be a third reasoning step, and
    its invented wording would enter the context of every later turn."""
    assert "llm" not in _identifiers(RUNTIME)
    assert "client" not in _identifiers(RUNTIME)


def test_no_arbitrary_redis_access_is_exposed() -> None:
    """Sessions are the only thing this service stores in Redis, and the store
    offers two operations. There is no general key tool an agent could reach."""
    public = [
        name
        for name, _ in inspect.getmembers(
            __import__("app.repositories.sessions", fromlist=["SessionStore"]).SessionStore,
            inspect.isfunction,
        )
        if not name.startswith("_")
    ]

    assert sorted(public) == ["load", "save_if_revision"]


def test_the_session_key_is_built_in_exactly_one_place() -> None:
    """One function turns a store and session into a Redis address, and it
    validates the id every time rather than trusting its caller."""
    users = [
        module.relative_to(APP).as_posix()
        for module in APP.rglob("*.py")
        if "zory:store:" in module.read_text()
    ]

    assert users == ["repositories/sessions.py"]
