"""What M11B-3B built, and what it must not have.

The authority claim this phase makes is narrow and worth stating precisely:
application-only contracts may now carry a `product_id`, because they are what
application code produces *after* resolving a model's selector. The guard is
that none of them is reachable from anything a model reads or writes.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, get_args, get_origin

import pytest
from app.core.config import CustomerAgentSettings, Settings
from app.schemas.agent_decision import CustomerAgentDecision, CustomerStateProposal
from app.schemas.agent_turn import CustomerResponse, DecisionInput, TurnGrounding
from app.schemas.agent_view import AgentStateView
from app.schemas.refinement import SearchRefinementDelta
from app.schemas.resolution import (
    ResolvedProductReference,
    ResolvedRelativePrice,
    SimilarSearchSeed,
)
from app.schemas.response import ResponseGroundingView, ResponseInput
from pydantic import BaseModel

APP = Path(__file__).parents[2] / "app"

BUILT_IN_3B = {
    "app/services/reference_resolver.py": "ProductReferenceResolver",
    "app/services/relative_price.py": "RelativePriceResolver",
    "app/services/comparison.py": "ProductComparisonService",
    "app/services/similar_search.py": "SimilarSearchBuilder",
    "app/services/grounding_builder.py": "to_grounded_product",
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

MODEL_FACING = (
    DecisionInput,
    TurnGrounding,
    AgentStateView,
    CustomerAgentDecision,
    CustomerStateProposal,
    SearchRefinementDelta,
    CustomerResponse,
    # The response layer answers to the same policy as every other model-facing
    # contract. A weaker guard for it would be a second authority rule, and the
    # looser one always wins in practice.
    ResponseInput,
    ResponseGroundingView,
)

APPLICATION_ONLY = (
    ResolvedProductReference,
    ResolvedRelativePrice,
    SimilarSearchSeed,
)


def _walk(model: type[BaseModel], seen: set[type] | None = None) -> list[Any]:
    seen = seen if seen is not None else set()
    if model in seen:
        return []
    seen.add(model)
    found: list[Any] = []
    for name, field in model.model_fields.items():
        found.append((name, field.annotation))
        for nested in _nested(field.annotation):
            found.extend(_walk(nested, seen))
    return found


def _nested(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    models: list[type[BaseModel]] = []
    for argument in get_args(annotation):
        models.extend(_nested(argument))
    if get_origin(annotation) is not None and not get_args(annotation):
        return models
    return models


# ── what this phase built ───────────────────────────────────────────────────


@pytest.mark.parametrize(("module", "symbol"), BUILT_IN_3B.items())
def test_the_phases_capability_exists(module: str, symbol: str) -> None:
    assert symbol in (APP.parent / module).read_text()


def test_the_comparison_maximum_is_configuration() -> None:
    assert "comparison_max_products" in CustomerAgentSettings.model_fields
    assert Settings.model_fields["customer_agent"] is not None


# ── what it must not have built ─────────────────────────────────────────────


@pytest.mark.parametrize("symbol", NOT_YET_REACHABLE)
def test_bundle_refinement_does_not_exist_yet(symbol: str) -> None:
    for module in APP.rglob("*.py"):
        assert symbol not in module.read_text(), f"{module.name} defines {symbol}"


def test_no_agent_module_or_public_route_exists() -> None:
    """The decision prompt arrived with M11B-4; these have not."""
    assert not (APP / "agents").exists()
    assert "chat.py" not in {p.name for p in (APP / "api/routes").glob("*.py")}


def test_query_understanding_is_still_the_only_provider_call_site() -> None:
    callers = [
        module.relative_to(APP).as_posix()
        for module in APP.rglob("*.py")
        if "responses.parse" in module.read_text()
    ]

    assert callers == ["integrations/llm.py"]


# ── application-only ids stay application-only ──────────────────────────────


def test_the_new_contracts_do_carry_product_ids() -> None:
    """The guard below is meaningless unless there is something to contain."""
    assert "product_id" in ResolvedProductReference.model_fields
    assert "reference_product_id" in ResolvedRelativePrice.model_fields
    assert "reference_product_id" in SimilarSearchSeed.model_fields


@pytest.mark.parametrize("model", MODEL_FACING, ids=lambda m: m.__name__)
@pytest.mark.parametrize("internal", APPLICATION_ONLY, ids=lambda m: m.__name__)
def test_no_application_only_type_is_reachable_from_a_model_contract(
    model: type[BaseModel], internal: type[BaseModel]
) -> None:
    rendered = {str(annotation) for _, annotation in _walk(model)}

    assert not any(internal.__name__ in text for text in rendered), model.__name__


@pytest.mark.parametrize("model", MODEL_FACING, ids=lambda m: m.__name__)
def test_model_contracts_still_carry_no_product_id(model: type[BaseModel]) -> None:
    for name, _ in _walk(model):
        assert "product_id" not in name, f"{model.__name__}.{name}"


@pytest.mark.parametrize("model", MODEL_FACING, ids=lambda m: m.__name__)
def test_model_contracts_still_carry_no_retailer_identity(
    model: type[BaseModel],
) -> None:
    for name, _ in _walk(model):
        for forbidden in ("store_id", "retailer", "tenant"):
            assert forbidden not in name, f"{model.__name__}.{name}"


def test_the_resolution_module_is_never_imported_by_a_model_contract() -> None:
    """A model-facing schema importing it would be the first step to leaking one.

    `schemas/agent_turn.py` is deliberately absent from this list. M11B-5 put
    `CustomerTurnResult` there, which carries an `AgentStateV1` and therefore
    product ids on purpose, so the module is no longer purely model-facing and
    an import check over it would prove nothing either way. The guarantee for
    that module is the field-level walk below, which is stronger: it follows
    what each model-facing type can actually reach.
    """
    for name in (
        "schemas/agent_decision.py",
        "schemas/agent_view.py",
        "schemas/refinement.py",
        "schemas/conversation.py",
    ):
        tree = ast.parse((APP / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "resolution" not in (node.module or ""), name


# ── the 3B services author nothing and decide nothing ───────────────────────


SERVICES = tuple(BUILT_IN_3B)


@pytest.mark.parametrize("module", SERVICES)
def test_no_service_calls_a_provider(module: str) -> None:
    tree = ast.parse((APP.parent / module).read_text())

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots = {(node.module or "").split(".")[0]}
        else:
            continue
        assert not roots & {"openai", "httpx", "requests", "redis"}, module


@pytest.mark.parametrize("module", SERVICES)
def test_no_service_owns_search_policy(module: str) -> None:
    """Relaxation, ranking and presentation belong elsewhere."""
    source = (APP.parent / module).read_text()

    for forbidden in (
        "ControlledRelaxationService",
        "SemanticRankingService",
        "select_for_presentation",
        "RelaxationPlanner",
    ):
        assert forbidden not in source, f"{module}: {forbidden}"


@pytest.mark.parametrize("module", SERVICES)
def test_no_service_writes_state(module: str) -> None:
    source = (APP.parent / module).read_text()

    assert "apply_update" not in source
    assert "commit_search_results" not in source


def test_the_composer_still_knows_nothing_of_product_facts() -> None:
    """3A stays pure; the relative resolver rewrites the delta before it runs."""
    source = (APP / "services/refinement_composer.py").read_text()

    for forbidden in ("get_by_ids", "ProductRepository", "RelativePriceResolver"):
        assert forbidden not in source, forbidden
