"""What M11B-4 built, how it is wired, and what it must still not have.

The configuration question this phase had to answer is the same one
`presentation_limit` answered: a capability with no approved model identifier
must not borrow one, and must not stop every other deployment from starting.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from app.api.dependencies import customer_agent_decision_service
from app.core.config import CustomerAgentSettings, Settings
from app.core.exceptions import ConfigurationError
from app.services.customer_decision import CustomerAgentDecisionService
from app.taxonomy.attributes import load_catalog_attributes

from tests.conftest import build_settings

APP = Path(__file__).parents[2] / "app"

BUILT_IN_4 = {
    "app/services/customer_decision.py": "CustomerAgentDecisionService",
    "app/prompts/customer_commerce/v1.py": "INSTRUCTIONS",
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


@pytest.mark.parametrize(("path", "symbol"), BUILT_IN_4.items())
def test_this_phase_built_what_it_said(path: str, symbol: str) -> None:
    assert symbol in (APP.parent / path).read_text()


def test_the_decision_service_is_wired_through_dependency_injection() -> None:
    settings = build_settings(customer_agent={"decision_model": "decision-model"})
    resources = type(
        "R",
        (),
        {
            "settings": settings,
            "decision_llm": object(),
            "attributes": load_catalog_attributes(),
            "rooms": None,
        },
    )()

    service = customer_agent_decision_service(resources)

    assert isinstance(service, CustomerAgentDecisionService)


# ── what must still not exist ───────────────────────────────────────────────


@pytest.mark.parametrize("symbol", NOT_YET_REACHABLE)
def test_bundle_refinement_does_not_exist_yet(symbol: str) -> None:
    for module in APP.rglob("*.py"):
        assert symbol not in module.read_text(), f"{module.name} defines {symbol}"


def test_the_decision_and_response_models_are_separate_settings() -> None:
    """Added by M11B-6. Neither may stand in for the other."""
    assert "decision_model" in CustomerAgentSettings.model_fields
    assert "response_model" in CustomerAgentSettings.model_fields


def test_the_decision_service_touches_no_session_storage() -> None:
    source = (APP / "services/customer_decision.py").read_text()

    for forbidden in ("Redis", "redis", "session"):
        assert forbidden not in source, forbidden


def test_the_provider_boundary_is_still_the_only_sdk_call_site() -> None:
    """A second agent must not mean a second provider surface."""
    callers = [
        module.relative_to(APP).as_posix()
        for module in APP.rglob("*.py")
        if "responses.parse" in module.read_text()
    ]

    assert callers == ["integrations/llm.py"]


# ── configuration: absent means unconfigured, never "borrow one" ────────────


def test_the_decision_model_has_no_default() -> None:
    assert CustomerAgentSettings().decision_model is None


def test_an_empty_decision_model_is_rejected() -> None:
    """Absent is a state; blank is a mistake, and would reach the provider."""
    with pytest.raises(ValueError, match="decision_model"):
        CustomerAgentSettings(decision_model="")


def test_settings_accept_a_configured_decision_model() -> None:
    settings = build_settings(customer_agent={"decision_model": "decision-model"})

    assert settings.customer_agent.decision_model == "decision-model"


def test_startup_settings_build_without_a_decision_model() -> None:
    """The reason this is lazy: no public customer route exists yet, so an
    unconfigured deployment must still start and serve health and search."""
    settings = build_settings()

    assert settings.customer_agent.decision_model is None
    assert isinstance(settings, Settings)


def test_requesting_the_service_unconfigured_fails_with_a_typed_error() -> None:
    resources = type("R", (), {"settings": build_settings(), "decision_llm": None})()

    with pytest.raises(ConfigurationError, match="decision_model"):
        customer_agent_decision_service(resources)


def test_the_unconfigured_failure_says_nothing_internal() -> None:
    resources = type("R", (), {"settings": build_settings(), "decision_llm": None})()

    with pytest.raises(ConfigurationError) as caught:
        customer_agent_decision_service(resources)

    assert "model" not in caught.value.public_message.lower()
    assert "openai" not in caught.value.public_message.lower()


def test_lifespan_builds_each_agent_client_only_when_configured() -> None:
    """Conditional, like the embedder - so an unconfigured process opens no
    extra client, and a configured one opens exactly one (CLAUDE.md 24)."""
    source = (APP / "core/lifespan.py").read_text()
    tree = ast.parse(source)
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "OpenAIStructuredClient"
    ]

    assert len(constructions) == 5, (
        "query understanding, decision, response, interior design, finder vision"
    )
    assert "if decision_model:" in source
    assert "if response_model:" in source
    assert "if design_model:" in source
    assert "if finder is not None:" in source
    assert "await finder_vision.close()" in source


def test_the_decision_client_is_closed_on_shutdown() -> None:
    source = (APP / "core/lifespan.py").read_text()

    assert "await decision_llm.close()" in source


def test_the_decision_model_is_never_defaulted_to_the_generic_one() -> None:
    """No code path may substitute `llm.model` when `decision_model` is unset."""
    config = (APP / "core/config.py").read_text()
    dependencies = (APP / "api/dependencies.py").read_text()

    assert "decision_model: str | None" in config
    assert "decision_model or" not in config
    assert "decision_model or" not in dependencies
    assert "settings.llm.model" not in dependencies


def test_the_configured_model_is_what_the_decision_client_carries() -> None:
    """The identifier reaches the provider through the client, not a parameter,
    so it cannot be overridden per call."""
    settings = build_settings(customer_agent={"decision_model": "decision-model"})
    overridden = settings.llm.model_copy(update={"model": settings.customer_agent.decision_model})

    assert overridden.model == "decision-model"
    assert settings.llm.model == "test-model"
    assert overridden.api_key is settings.llm.api_key


@pytest.mark.parametrize(
    "invented",
    ["decision_temperature", "decision_max_tokens", "decision_reasoning_effort"],
)
def test_no_decision_specific_provider_settings_were_invented(invented: str) -> None:
    """The generic provider settings already cover these (CLAUDE.md 31)."""
    assert invented not in (APP / "core/config.py").read_text()


def test_the_settings_still_carry_only_what_has_consumers() -> None:
    fields: dict[str, Any] = dict(CustomerAgentSettings.model_fields)

    assert set(fields) == {
        "comparison_max_products",
        "presentation_limit",
        "decision_model",
        "response_model",
    }
