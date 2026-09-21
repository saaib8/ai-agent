"""What the model may see, and what it may never control.

These are the phase's reason for existing. They walk the real pydantic field
graph rather than grepping source, so a forbidden field cannot hide inside a
nested model, a tuple, an optional or a union - which is exactly where it
would end up if it were added carelessly.
"""

from __future__ import annotations

import typing
from typing import Any, get_args, get_origin

import pytest
from app.schemas.agent_decision import (
    CustomerAgentDecision,
    CustomerStateProposal,
    DerivedCommerceProposal,
    NewSearchProposal,
    ProductInteractionIntent,
)
from app.schemas.agent_turn import (
    CustomerResponse,
    CustomerTurnInput,
    DecisionInput,
    TurnGrounding,
)
from app.schemas.agent_view import AgentStateView
from app.schemas.comparison import ProductComparisonResult
from app.schemas.grounding import GroundedProduct, SearchExecutionGrounding
from app.schemas.refinement import SearchRefinementDelta
from pydantic import BaseModel

# Everything a model reads or writes.
MODEL_FACING = (
    DecisionInput,
    AgentStateView,
    CustomerAgentDecision,
    CustomerStateProposal,
    DerivedCommerceProposal,
    NewSearchProposal,
    SearchRefinementDelta,
    ProductInteractionIntent,
    CustomerResponse,
)

# What the response layer is handed. Verified facts, no service objects.
GROUNDING_FACING = (GroundedProduct, SearchExecutionGrounding, ProductComparisonResult)

RETAILER_IDENTITY = ("store_id", "store", "retailer", "tenant")
PRODUCT_IDENTITY = (
    "product_id",
    "product_ids",
    "presented_product_ids",
    "selected_product_ids",
    "focused_product_id",
    "bundle_product_ids",
    "locked_product_ids",
    # V2 bundle identity. The names changed; the rule did not - a model may
    # neither name a product nor target a bundle line.
    "bundle_items",
    "line_id",
    "next_bundle_line_id",
)


def _walk(model: type[BaseModel], seen: set[type] | None = None) -> list[tuple[str, Any]]:
    """Every (field name, annotation) reachable from a model, nested included."""
    seen = seen if seen is not None else set()
    if model in seen:
        return []
    seen.add(model)

    found: list[tuple[str, Any]] = []
    for name, field in model.model_fields.items():
        found.append((name, field.annotation))
        for nested in _nested_models(field.annotation):
            found.extend(_walk(nested, seen))
    return found


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    models: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    for argument in get_args(annotation):
        models.extend(_nested_models(argument))
    if get_origin(annotation) is not None and not get_args(annotation):
        return models
    return models


def _names(model: type[BaseModel]) -> set[str]:
    return {name for name, _ in _walk(model)}


def _types(model: type[BaseModel]) -> set[str]:
    rendered = set()
    for _, annotation in _walk(model):
        rendered.add(str(annotation))
    return rendered


# ── the walker must actually walk ───────────────────────────────────────────


def test_the_field_walker_reaches_nested_models() -> None:
    """A guard built on a broken walker would pass by finding nothing."""
    names = _names(DecisionInput)

    assert "state_view" in names
    assert "active_search" in names  # one level down
    assert "commerce_category" in names  # two levels down
    assert "messages" in names
    assert len(names) > 25


# ── retailer scope ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("model", MODEL_FACING, ids=lambda m: m.__name__)
def test_no_retailer_identity_is_model_visible(model: type[BaseModel]) -> None:
    for name in _names(model):
        for forbidden in RETAILER_IDENTITY:
            assert forbidden not in name, f"{model.__name__}.{name}"


@pytest.mark.parametrize("model", MODEL_FACING, ids=lambda m: m.__name__)
def test_retailer_context_is_not_reachable_from_model_input(
    model: type[BaseModel],
) -> None:
    assert not any("RetailerContext" in t for t in _types(model)), model.__name__


def test_the_runtime_input_does_carry_retailer_scope() -> None:
    """The boundary is only meaningful if the other side of it exists."""
    assert "context" in CustomerTurnInput.model_fields
    assert "RetailerContext" in str(CustomerTurnInput.model_fields["context"].annotation)


def test_the_two_inputs_are_different_types() -> None:
    """Passing the wrong one is a type error, not a review oversight."""
    assert set(CustomerTurnInput.model_fields) != set(DecisionInput.model_fields)
    assert "state" in CustomerTurnInput.model_fields
    assert "state" not in DecisionInput.model_fields


# ── product identity ────────────────────────────────────────────────────────


@pytest.mark.parametrize("model", MODEL_FACING, ids=lambda m: m.__name__)
def test_no_authoritative_product_id_is_model_facing(model: type[BaseModel]) -> None:
    """Substring, not equality.

    An earlier version compared names exactly and let `reference_product_id`
    through - which is precisely how an id would arrive: attached to something
    that sounds like a reference.
    """
    for name in _names(model):
        assert "product_id" not in name, f"{model.__name__}.{name}"
        for forbidden in PRODUCT_IDENTITY:
            assert forbidden != name, f"{model.__name__}.{name}"


@pytest.mark.parametrize("model", MODEL_FACING, ids=lambda m: m.__name__)
def test_no_raw_catalog_object_is_model_facing(model: type[BaseModel]) -> None:
    rendered = _types(model)
    for forbidden in ("ProductRow", "ProductCandidate", "EligibleProduct"):
        assert not any(forbidden in t for t in rendered), f"{model.__name__}: {forbidden}"


def test_grounded_products_carry_no_product_id() -> None:
    """Prose cites a turn-local handle; the id stays with the application."""
    for name in _names(GroundedProduct):
        assert "product_id" not in name


# ── state authority ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("model", MODEL_FACING, ids=lambda m: m.__name__)
def test_no_state_update_is_model_output(model: type[BaseModel]) -> None:
    rendered = _types(model)
    for forbidden in ("AgentStateUpdate", "AgentStateV1", "ProductInteractionUpdate"):
        assert not any(forbidden in t for t in rendered), f"{model.__name__}: {forbidden}"


def test_the_decision_cannot_carry_a_state_update() -> None:
    assert "state_update" not in CustomerAgentDecision.model_fields
    assert "AgentStateUpdate" not in str(typing.get_type_hints(CustomerAgentDecision))


# ── service and provider objects ────────────────────────────────────────────


@pytest.mark.parametrize(
    "model", (*MODEL_FACING, *GROUNDING_FACING), ids=lambda m: m.__name__
)
def test_no_service_or_provider_object_reaches_a_contract(
    model: type[BaseModel],
) -> None:
    rendered = _types(model)
    for forbidden in (
        "ControlledSearchResult",
        "SemanticRankingResult",
        "ProductSearchRequest",
        "ProductSearchResult",
        "RelaxedCandidate",
        "Session",
        "Exception",
    ):
        assert not any(forbidden in t for t in rendered), f"{model.__name__}: {forbidden}"


def test_grounding_carries_no_similarity_score() -> None:
    """Similarity is a distance between embeddings, not a fact about a product."""
    for model in GROUNDING_FACING:
        for name in _names(model):
            assert "similarity" not in name, f"{model.__name__}.{name}"


# ── durable search text ─────────────────────────────────────────────────────


def test_semantic_text_is_never_a_contract_field() -> None:
    """It is per-execution wording; a durable copy would outlive its search."""
    for model in (*MODEL_FACING, *GROUNDING_FACING, TurnGrounding):
        assert "semantic_text" not in _names(model), model.__name__


# ── English only ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model", (*MODEL_FACING, *GROUNDING_FACING, TurnGrounding), ids=lambda m: m.__name__
)
def test_no_arabic_or_language_field_exists_in_m11(model: type[BaseModel]) -> None:
    for name in _names(model):
        assert "arabic" not in name, f"{model.__name__}.{name}"
        assert "language" not in name, f"{model.__name__}.{name}"
        assert "locale" not in name, f"{model.__name__}.{name}"


def test_grounded_products_display_english() -> None:
    assert "name_english" in GroundedProduct.model_fields
    assert "name_arabic" not in GroundedProduct.model_fields


# ── immutability and closure ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    (*MODEL_FACING, *GROUNDING_FACING, CustomerTurnInput, TurnGrounding),
    ids=lambda m: m.__name__,
)
def test_contracts_are_frozen_and_reject_unknown_fields(
    model: type[BaseModel],
) -> None:
    """An ignored unexpected field is a model output nobody validated."""
    assert model.model_config.get("frozen") is True, model.__name__
    assert model.model_config.get("extra") == "forbid", model.__name__
