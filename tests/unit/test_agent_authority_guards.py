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
from app.schemas.agent_turn import CustomerResponse
from app.schemas.comparison import ProductComparisonResult
from app.schemas.grounding import GroundedProduct, SearchExecutionGrounding
from app.schemas.refinement import SearchRefinementDelta
from pydantic import BaseModel

# Everything a model reads or writes.
MODEL_FACING = (
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




# ── English reasoning, language only for presentation ───────────────────────

APPROVED_LANGUAGE_FIELDS = frozenset({"reply_language", "switch_reply_language"})
"""The language replies are written in (docs/arabic-replies-plan.md). Nothing
else on a model-facing contract names a language: decisions and search stay
English, and no Arabic product data reaches the decision model."""

APPROVED_ARABIC_DISPLAY_FIELDS = frozenset({"name_arabic"})
"""A grounded product's Arabic: the store's Arabic name, so a reply written in
Arabic names the product as its card does. The card's text in the reply's
language (`display`) is attached to the payload only. Never on what the
decision model reads."""


@pytest.mark.parametrize("model", MODEL_FACING, ids=lambda m: m.__name__)
def test_only_the_reply_language_is_a_language_field(model: type[BaseModel]) -> None:
    for name in _names(model):
        assert "arabic" not in name, f"{model.__name__}.{name}"
        assert "locale" not in name, f"{model.__name__}.{name}"
        if "language" in name:
            assert name in APPROVED_LANGUAGE_FIELDS, f"{model.__name__}.{name}"




def test_grounded_products_reason_in_english_and_display_arabic() -> None:
    """English is what every decision reads; the Arabic is display only, and
    nothing grounds a turn with it attached."""
    assert "name_english" in GroundedProduct.model_fields
    assert set(GroundedProduct.model_fields) >= APPROVED_ARABIC_DISPLAY_FIELDS
    assert GroundedProduct.model_fields["display"].default is None


# ── immutability and closure ────────────────────────────────────────────────




# ── both reasoning agents, and nothing left unguarded (M12F 6) ──────────────


DURABLE_IDENTITY = (
    "need_id",
    "next_design_need_id",
    "rejected_product_ids",
    "bundle_revision",
    "schema_version",
)
"""Persistence bookkeeping. A model that can see one can start reasoning about
it, and one that can emit one is authoring state."""












def test_the_design_specialist_reads_design_meaning_and_never_plan_records() -> None:
    """`CurrentDesignNeed` is the projection; `RoomDesignNeedState` is the
    record it was projected from, and carries the identity it dropped."""
    from app.schemas.design import InteriorDesignRequest

    rendered = " ".join(_types(InteriorDesignRequest))

    assert "CurrentDesignNeed" in rendered
    assert "RoomDesignNeedState" not in rendered


def test_the_customers_own_budget_is_still_allowed_through() -> None:
    """The guards above must not be passing by excluding everything."""
    from app.schemas.design import InteriorDesignRequest

    assert "budget" in _names(InteriorDesignRequest)
    assert "PriceConstraint" in " ".join(_types(InteriorDesignRequest))
