"""AgentStateV1 contracts and their invariants.

These tests defend three boundaries. State holds references, never catalog
facts. Retailer scope is not state. Raw conversation is not state. Everything
else here protects invariants that, if broken, would let a later layer resolve
"the second one" to the wrong product or widen a search nobody widened.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.agent_state import (
    AGENT_STATE_VERSION,
    MAX_SEMANTIC_INTENT_CHARS,
    ActiveSearchState,
    AgentStateV1,
    CustomerPreferenceState,
    DerivedCommerceState,
    ProductInteractionState,
    PurchaseStage,
    RoomProjectState,
)
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PriceConstraint,
    ProductSearchRequest,
)
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    SemanticPreference,
)
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.dimensions import DimensionRole
from pydantic import BaseModel, ValidationError

SAR = "SAR"


def _request(**kwargs: object) -> ProductSearchRequest:
    return ProductSearchRequest(
        commerce_category="seating", commerce_subcategory="sofa", **kwargs
    )


def _search(**kwargs: object) -> ActiveSearchState:
    kwargs.setdefault("request", _request())
    kwargs.setdefault("revision", 1)
    return ActiveSearchState(**kwargs)


def _preference(value: str = "warm neutral") -> SemanticPreference:
    return SemanticPreference(
        family=AttributeFamily.COLOR,
        raw_value=value,
        strength=ConstraintStrength.PREFERRED,
    )


# ── the root ────────────────────────────────────────────────────────────────


def test_a_default_state_is_valid_and_empty() -> None:
    state = AgentStateV1()

    assert state.schema_version == AGENT_STATE_VERSION
    assert state.active_search is None
    assert state.room_project is None
    assert state.customer_preferences.semantic_preferences == ()
    assert state.derived_commerce.purchase_stage is None


def test_an_unknown_schema_version_is_rejected() -> None:
    """A V1 blob must not be silently misread by V2 code."""
    with pytest.raises(ValidationError):
        AgentStateV1(schema_version="agent_state_v2")


@pytest.mark.parametrize(
    "model",
    [
        AgentStateV1,
        CustomerPreferenceState,
        ActiveSearchState,
        ProductInteractionState,
        RoomProjectState,
        DerivedCommerceState,
    ],
)
def test_every_state_model_is_frozen_and_forbids_extras(
    model: type[BaseModel],
) -> None:
    assert model.model_config["frozen"] is True
    assert model.model_config["extra"] == "forbid"


def test_an_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentStateV1(store_id=50)  # type: ignore[call-arg]


def test_state_cannot_be_mutated_in_place() -> None:
    state = AgentStateV1()

    with pytest.raises(ValidationError):
        state.derived_commerce = DerivedCommerceState()  # type: ignore[misc]


# ── active search ───────────────────────────────────────────────────────────


def test_a_real_search_request_is_accepted() -> None:
    state = _search(request=_request(price=PriceConstraint.at_most(Decimal("5000"), SAR)))

    assert state.request.commerce_subcategory == "sofa"
    assert state.revision == 1


def test_the_shared_dimension_correspondence_is_enforced() -> None:
    """The same rule ResolvedSearch uses, from the same implementation."""
    width = DimensionConstraint(
        role=DimensionRole.OVERALL_WIDTH,
        kind=DimensionConstraintKind.MAX,
        max_cm=Decimal("220"),
    )
    ok = _search(
        request=_request(dimensions=(width,)),
        semantics=ConstraintSemantics(
            dimensions=(
                DimensionConstraintSemantics(
                    role=DimensionRole.OVERALL_WIDTH,
                    strength=ConstraintStrength.APPROXIMATE,
                ),
            )
        ),
    )
    assert ok.request.dimensions[0].role is DimensionRole.OVERALL_WIDTH

    with pytest.raises(ValidationError):
        _search(request=_request(dimensions=(width,)))  # no recorded strength


def test_the_validator_is_the_shared_one() -> None:
    """One implementation, so ResolvedSearch and ActiveSearchState cannot drift."""
    import ast
    from pathlib import Path

    tree = ast.parse(
        (Path(__file__).parents[2] / "app/schemas/agent_state.py").read_text()
    )
    sources = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and any(a.name == "validate_dimension_correspondence" for a in node.names)
    }
    assert sources == {"app.schemas.query"}, "one implementation, imported not copied"


def test_revision_zero_means_nothing_committed_yet() -> None:
    """A search can exist before any results have been presented for it."""
    assert _search(revision=0).revision == 0


def test_a_negative_revision_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _search(revision=-1)


# ── semantic_intent ─────────────────────────────────────────────────────────


def test_semantic_intent_keeps_a_real_value() -> None:
    assert _search(semantic_intent="cosy").semantic_intent == "cosy"


def test_semantic_intent_is_stripped() -> None:
    assert _search(semantic_intent="  elegant  ").semantic_intent == "elegant"


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_semantic_intent_becomes_absent(blank: str) -> None:
    assert _search(semantic_intent=blank).semantic_intent is None


def test_an_over_long_semantic_intent_is_rejected() -> None:
    """The only thing stopping a durable property becoming a transcript."""
    with pytest.raises(ValidationError):
        _search(semantic_intent="x" * (MAX_SEMANTIC_INTENT_CHARS + 1))


def test_a_semantic_intent_at_the_bound_is_accepted() -> None:
    value = "x" * MAX_SEMANTIC_INTENT_CHARS
    assert _search(semantic_intent=value).semantic_intent == value


def test_semantic_intent_is_not_parsed_or_coerced() -> None:
    """Free text stays free text: no taxonomy coercion, no extraction."""
    intent = "cosy under 5000 SAR around 220cm"
    state = _search(semantic_intent=intent)

    assert state.semantic_intent == intent
    assert state.request.price is None
    assert state.request.dimensions == ()


# ── product interaction ─────────────────────────────────────────────────────


def test_presented_order_is_preserved() -> None:
    state = ProductInteractionState(presented_product_ids=(165800, 165645, 165913))

    assert state.presented_product_ids[1] == 165645, "'the second one'"


@pytest.mark.parametrize(
    "field", ["presented_product_ids", "selected_product_ids"]
)
def test_duplicate_product_ids_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        ProductInteractionState(**{field: (1, 2, 1)})


def test_a_focus_on_a_presented_product_is_accepted() -> None:
    state = ProductInteractionState(
        presented_product_ids=(1, 2, 3), focused_product_id=2
    )

    assert state.focused_product_id == 2


def test_a_focus_on_a_selected_product_is_accepted() -> None:
    """Selection need not come from the current result list."""
    state = ProductInteractionState(selected_product_ids=(9,), focused_product_id=9)

    assert state.focused_product_id == 9


def test_a_focus_on_an_unknown_product_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ProductInteractionState(presented_product_ids=(1, 2), focused_product_id=99)


def test_a_selection_need_not_be_presented() -> None:
    state = ProductInteractionState(presented_product_ids=(1,), selected_product_ids=(7,))

    assert state.selected_product_ids == (7,)


def test_a_presented_result_set_requires_a_committed_revision() -> None:
    """Revision 0 means nothing was committed, so nothing can be presented."""
    with pytest.raises(ValidationError):
        AgentStateV1(
            active_search=_search(revision=0),
            product_interaction=ProductInteractionState(
                presented_product_ids=(1,), presented_search_revision=0
            ),
        )


def test_a_presented_revision_must_match_the_active_search() -> None:
    state = AgentStateV1(
        active_search=_search(revision=3),
        product_interaction=ProductInteractionState(
            presented_product_ids=(1,), presented_search_revision=3
        ),
    )
    assert state.product_interaction.presented_search_revision == 3

    with pytest.raises(ValidationError):
        AgentStateV1(
            active_search=_search(revision=3),
            product_interaction=ProductInteractionState(
                presented_product_ids=(1,), presented_search_revision=2
            ),
        )


def test_a_presented_revision_without_an_active_search_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentStateV1(
            product_interaction=ProductInteractionState(presented_search_revision=1)
        )


def test_no_presented_results_means_no_presented_revision() -> None:
    state = AgentStateV1(active_search=_search())

    assert state.product_interaction.presented_product_ids == ()
    assert state.product_interaction.presented_search_revision is None


# ── room project ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["bundle_product_ids", "locked_product_ids"])
def test_room_product_ids_are_unique(field: str) -> None:
    with pytest.raises(ValidationError):
        RoomProjectState(**{field: (1, 1)})


def test_locked_products_must_be_in_the_bundle() -> None:
    ok = RoomProjectState(bundle_product_ids=(1, 2, 3), locked_product_ids=(1, 3))
    assert ok.locked_product_ids == (1, 3)

    with pytest.raises(ValidationError):
        RoomProjectState(bundle_product_ids=(1, 2), locked_product_ids=(9,))


def test_the_bundle_and_the_browsing_shortlist_are_independent() -> None:
    """Different concepts; the rename exists so they cannot be confused."""
    state = AgentStateV1(
        product_interaction=ProductInteractionState(selected_product_ids=(7, 8)),
        room_project=RoomProjectState(bundle_product_ids=(1, 2)),
    )

    assert state.product_interaction.selected_product_ids == (7, 8)
    assert state.room_project is not None
    assert state.room_project.bundle_product_ids == (1, 2)


def test_the_room_budget_reuses_the_price_contract() -> None:
    room = RoomProjectState(budget=PriceConstraint.at_most(Decimal("12000"), SAR))

    assert room.budget is not None
    assert room.budget.currency == SAR


def test_room_type_is_plain_context() -> None:
    """Free text, and nothing in this service filters on it."""
    assert RoomProjectState(room_type="living room").room_type == "living room"


# ── derived commerce ────────────────────────────────────────────────────────


@pytest.mark.parametrize("stage", list(PurchaseStage))
def test_every_purchase_stage_is_representable(stage: PurchaseStage) -> None:
    assert DerivedCommerceState(purchase_stage=stage).purchase_stage is stage


def test_an_unassessed_stage_is_none_not_exploring() -> None:
    assert DerivedCommerceState().purchase_stage is None
    assert DerivedCommerceState().purchase_stage is not PurchaseStage.EXPLORING


# ── boundary guards ─────────────────────────────────────────────────────────


def _state_field_names() -> set[str]:
    models = (
        AgentStateV1, CustomerPreferenceState, ActiveSearchState,
        ProductInteractionState, RoomProjectState, DerivedCommerceState,
    )
    return {name for model in models for name in model.model_fields}


@pytest.mark.parametrize("forbidden", ["store_id", "retailer_id", "tenant_id"])
def test_retailer_identity_is_never_state(forbidden: str) -> None:
    assert forbidden not in _state_field_names()


def _imported_names() -> set[str]:
    """What the state module actually imports, prose in docstrings excluded."""
    import ast
    from pathlib import Path

    tree = ast.parse(
        (Path(__file__).parents[2] / "app/schemas/agent_state.py").read_text()
    )
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_semantic_text_is_never_state() -> None:
    """The per-turn M9 artefact must not become durable truth."""
    assert "semantic_text" not in _state_field_names()
    assert "ResolvedSearch" not in _imported_names()


def test_no_raw_message_or_transcript_field_exists() -> None:
    names = _state_field_names()
    for forbidden in ("message", "messages", "transcript", "history", "conversation"):
        assert not any(forbidden in name for name in names), forbidden


def test_no_catalog_fact_object_is_nested_in_state() -> None:
    """State remembers references; PostgreSQL remembers facts."""
    imported = _imported_names()
    for forbidden in ("ProductCandidate", "ProductRow", "NormalisedDimensions"):
        assert forbidden not in imported, forbidden


def test_no_product_fact_field_names_exist() -> None:
    names = _state_field_names()
    for forbidden in ("name_english", "price_amount", "image_url", "product_url"):
        assert forbidden not in names, forbidden


def test_retailer_context_and_capabilities_are_not_state_fields() -> None:
    imported = _imported_names()
    assert "RetailerContext" not in imported
    assert "RetailerCatalogCapabilities" not in imported
