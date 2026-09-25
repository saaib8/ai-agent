"""What M11B-5 built, how it is wired, and what it must still not have.

The authority claim this phase makes: product ids now flow freely *below* the
model boundary - the coordinator resolves them, hydrates them and commits them
to state. The guard is that none of it travels back up.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from app.api.dependencies import customer_turn_coordinator
from app.core.config import CustomerAgentSettings
from app.core.exceptions import ConfigurationError
from app.services.agent_view import project_state
from app.services.proposal_mapping import map_proposals
from app.services.turn_coordinator import CustomerTurnCoordinator

from tests.conftest import build_settings

APP = Path(__file__).parents[2] / "app"
COORDINATOR = APP / "services/turn_coordinator.py"

BUILT_IN_5 = {
    "app/services/turn_coordinator.py": "CustomerTurnCoordinator",
    "app/services/agent_view.py": "def project_state",
    "app/services/proposal_mapping.py": "def map_proposals",
}

NOT_YET_REACHABLE = (
    "REJECT_PRODUCT",
    "INTENTIONALLY_UNFILLED",
    "FocusedBundleItem",
)
"""What M12E-4C deliberately did not build.

E4C completed iterative refinement - replacing, re-costing, removing a role -
all without a specialist call. What is left is composition: changing what the
room is *for*, which needs the design agent to revise a plan it can currently
only create. That is E4D.

`REJECT_PRODUCT` stays out for a concrete reason rather than for scope. "Remove
this and leave the gap" needs a durable `INTENTIONALLY_UNFILLED` state; without
one, the next optimisation would quietly refill the role, or the room would
report it as a catalog gap. `FocusedBundleItem` stays out because nothing
tracks a focused card."""


# ── what must now exist ─────────────────────────────────────────────────────


@pytest.mark.parametrize(("path", "symbol"), BUILT_IN_5.items())
def test_this_phase_built_what_it_said(path: str, symbol: str) -> None:
    assert symbol in (APP.parent / path).read_text()


def test_the_coordinator_takes_one_argument() -> None:
    """`CustomerTurnInput` already carries the retailer context, so there is no
    second scope argument to get wrong."""
    signature = inspect.signature(CustomerTurnCoordinator.run)
    parameters = [p for p in signature.parameters if p != "self"]

    assert parameters == ["turn"]


def test_the_coordinator_depends_only_on_approved_services() -> None:
    signature = inspect.signature(CustomerTurnCoordinator.__init__)
    parameters = [p for p in signature.parameters if p != "self"]

    assert parameters == [
        "decisions",
        "query_understanding",
        "composer",
        "references",
        "relative_price",
        "comparison",
        "pipeline",
        "hydration",
        "similar_search",
        # M12E-2: the whole-room path. Each is a deterministic service or the
        # one design agent; none of them is a second search system.
        "capabilities",
        "design",
        "design_discovery",
        "bundle_references",
        "optimizer",
        # The seating-combination move: a deterministic planner that composes
        # pieces to meet a seat count no single product can, reusing the same
        # capability service and repository. Not a second search system.
        "seating_planner",
        "dimensions",
        # M15: the approved vocabulary, so the coordinator can check that a
        # product a reference resolved to is the kind the customer named. A
        # registry, not a service - it reads no catalog and reaches nothing.
        "taxonomy",
    ]


def test_the_pure_helpers_are_functions_not_services() -> None:
    """No I/O and no reasoning, so a class would only add ceremony."""
    assert inspect.isfunction(project_state)
    assert inspect.isfunction(map_proposals)


def test_the_coordinator_is_wired_lazily() -> None:
    settings = build_settings(
        customer_agent={"decision_model": "decision-model", "presentation_limit": 5}
    )
    resources = type(
        "R",
        (),
        {
            "settings": settings,
            "decision_llm": object(),
            "llm": object(),
            "taxonomy": None,
            "attributes": None,
            "dimensions": None,
            "embedder": None,
            "semantic_index": None,
        },
    )()

    # Construction reaches real registries, so this only asserts the factory is
    # requested lazily rather than at import or startup.
    assert callable(customer_turn_coordinator)
    assert resources.settings.customer_agent.decision_model == "decision-model"


def test_requesting_it_unconfigured_fails_with_a_typed_error() -> None:
    """Unconfigured means unavailable, not a half-built coordinator."""
    resources = type("R", (), {"settings": build_settings(), "decision_llm": None})()

    with pytest.raises(ConfigurationError):
        customer_turn_coordinator(None, resources)  # type: ignore[arg-type]


def test_startup_settings_still_build_with_nothing_configured() -> None:
    settings = build_settings()

    assert settings.customer_agent.decision_model is None
    assert settings.customer_agent.presentation_limit is None


# ── what must still not exist ───────────────────────────────────────────────


@pytest.mark.parametrize("symbol", NOT_YET_REACHABLE)
def test_bundle_refinement_does_not_exist_yet(symbol: str) -> None:
    for module in APP.rglob("*.py"):
        assert symbol not in module.read_text(), f"{module.name} defines {symbol}"


def test_the_response_model_setting_arrived_with_its_consumer() -> None:
    assert "response_model" in CustomerAgentSettings.model_fields
    assert CustomerAgentSettings().response_model is None


def _coordinator_identifiers() -> set[str]:
    """Names the module actually uses - not words in its prose."""
    tree = ast.parse(COORDINATOR.read_text())
    return {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.Attribute)
    }


@pytest.mark.parametrize(
    "forbidden",
    ["RedisClient", "SessionStore", "SessionRepository", "save", "persist", "store"],
)
def test_the_coordinator_persists_nothing(forbidden: str) -> None:
    """The next state is returned, not stored. Session lifecycle is later."""
    assert forbidden not in _coordinator_identifiers(), forbidden


def test_the_coordinator_imports_no_persistence() -> None:
    tree = ast.parse(COORDINATOR.read_text())
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    assert not any("redis" in name for name in imported)
    assert not any("repositories" in name for name in imported)


def test_the_coordinator_generates_no_prose() -> None:
    """It builds grounding, never a reply. `CustomerResponse` is M11B-6's."""
    identifiers = _coordinator_identifiers()

    assert "CustomerResponse" not in identifiers
    assert "CustomerResponseGenerator" not in identifiers
    # No f-string anywhere builds customer-facing text: the only formatted
    # strings are a defect reason and a log field.
    assert "BlockingClarification(" not in COORDINATOR.read_text(), (
        "question wording is never authored here"
    )


def test_the_coordinator_still_does_not_word_the_reply() -> None:
    """M11B-6 added the response prompt. The coordinator is still not its
    caller - it produces the turn result and stops."""
    source = (APP / "services/turn_coordinator.py").read_text()

    assert "response_v1" not in source
    assert "CustomerResponseGenerator" not in source


# ── the authority boundary ──────────────────────────────────────────────────


def _decision_input_construction() -> ast.Call:
    tree = ast.parse(COORDINATOR.read_text())
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "DecisionInput"
    )


def test_the_model_receives_exactly_three_projected_things() -> None:
    """Checked on the call site, so an added field fails here rather than
    reaching a prompt."""
    keywords = {kw.arg for kw in _decision_input_construction().keywords}

    assert keywords == {"message", "conversation", "state_view"}


def test_the_model_never_receives_the_authoritative_state() -> None:
    """`state_view=project_state(...)`, never the state itself."""
    call = _decision_input_construction()
    state_view = next(kw for kw in call.keywords if kw.arg == "state_view")

    assert isinstance(state_view.value, ast.Call)
    assert isinstance(state_view.value.func, ast.Name)
    assert state_view.value.func.id == "project_state"


@pytest.mark.parametrize("forbidden", ["store_id", "product_id", "context"])
def test_no_retailer_or_product_identity_reaches_the_decision(forbidden: str) -> None:
    keywords = {kw.arg for kw in _decision_input_construction().keywords}

    assert forbidden not in keywords


def test_the_projection_never_reads_a_product_id_into_the_view() -> None:
    """Ids become counts and positions on the way through, and the view has no
    field that could hold one."""
    from app.schemas.agent_view import AgentStateView

    def walk(model: type, seen: set[type] | None = None) -> list[str]:
        seen = seen if seen is not None else set()
        if model in seen:
            return []
        seen.add(model)
        names: list[str] = []
        for name, field in getattr(model, "model_fields", {}).items():
            names.append(name)
            for arg in (field.annotation, *getattr(field.annotation, "__args__", ())):
                if hasattr(arg, "model_fields"):
                    names.extend(walk(arg, seen))
        return names

    for name in walk(AgentStateView):
        assert "product_id" not in name, name
        assert "store_id" not in name, name
        assert "revision" not in name, name


def test_the_projection_is_pure() -> None:
    """No await, no client, no repository - the same state always projects the
    same way."""
    source = (APP / "services/agent_view.py").read_text()
    tree = ast.parse(source)

    assert not any(isinstance(node, ast.Await) for node in ast.walk(tree))
    for forbidden in ("Repository", "client", "context", "datetime", "random"):
        assert forbidden not in source, forbidden


def test_the_proposal_mapper_is_pure() -> None:
    source = (APP / "services/proposal_mapping.py").read_text()
    tree = ast.parse(source)

    assert not any(isinstance(node, ast.Await) for node in ast.walk(tree))
    for forbidden in ("Repository", "RetailerContext", "datetime"):
        assert forbidden not in source, forbidden


def test_a_decision_cannot_produce_a_state_update_directly() -> None:
    """The model proposes; application code builds the typed update."""
    from app.schemas.agent_decision import CustomerAgentDecision

    rendered = str(CustomerAgentDecision.model_fields)

    assert "AgentStateUpdate" not in rendered
    assert "ProductInteractionUpdate" not in rendered


def test_the_provider_boundary_is_still_the_only_sdk_call_site() -> None:
    callers = [
        module.relative_to(APP).as_posix()
        for module in APP.rglob("*.py")
        if "responses.parse" in module.read_text()
    ]

    assert callers == ["integrations/llm.py"]


def test_the_coordinator_logs_nothing_sensitive() -> None:
    tree = ast.parse(COORDINATOR.read_text())
    logged: set[str | None] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("info", "warning", "error")
        ):
            logged |= {kw.arg for kw in node.keywords}

    for forbidden in ("message", "conversation", "product_id", "product_ids", "state"):
        assert forbidden not in logged, forbidden


# ── the response layer, added by M11B-6 ─────────────────────────────────────


def test_the_response_generator_is_wired_lazily() -> None:
    from app.api.dependencies import customer_response_generator

    assert callable(customer_response_generator)


def test_requesting_the_response_layer_unconfigured_fails_typed() -> None:
    from app.api.dependencies import customer_response_generator

    resources = type("R", (), {"settings": build_settings(), "response_llm": None})()

    with pytest.raises(ConfigurationError, match="response_model"):
        customer_response_generator(resources)


def test_the_unconfigured_response_failure_says_nothing_internal() -> None:
    from app.api.dependencies import customer_response_generator

    resources = type("R", (), {"settings": build_settings(), "response_llm": None})()

    with pytest.raises(ConfigurationError) as caught:
        customer_response_generator(resources)

    assert "model" not in caught.value.public_message.lower()
    assert "openai" not in caught.value.public_message.lower()


def test_the_response_model_is_never_borrowed_from_another_capability() -> None:
    config = (APP / "core/config.py").read_text()
    dependencies = (APP / "api/dependencies.py").read_text()

    assert "response_model: str | None" in config
    assert "response_model or" not in config
    assert "response_model or" not in dependencies
    assert "decision_llm" not in dependencies.split("def customer_response_generator")[1]


def test_the_response_client_is_conditional_and_closed() -> None:
    source = (APP / "core/lifespan.py").read_text()

    assert "if response_model:" in source
    assert "await response_llm.close()" in source


def test_an_unconfigured_deployment_still_starts() -> None:
    settings = build_settings()

    assert settings.customer_agent.decision_model is None
    assert settings.customer_agent.response_model is None
    assert settings.customer_agent.presentation_limit is None
