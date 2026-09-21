"""The reducer: bounded typed updates, applied deterministically.

The property most of these defend is the quiet one — an update that does not
mention a domain provably cannot change it. That is what lets "show me cheaper
ones" keep a preference recorded three turns earlier, and it is exactly what
full-state replacement would lose.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.core.exceptions import InvalidRequestError
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    CustomerPreferenceState,
    DerivedCommerceState,
    ProductInteractionState,
    PurchaseStage,
    RoomProjectState,
)
from app.schemas.agent_updates import (
    ActiveSearchUpdate,
    AddItems,
    AgentStateUpdate,
    ClearSemanticIntent,
    CustomerPreferenceUpdate,
    DerivedCommerceUpdate,
    ProductInteractionUpdate,
    RemoveItems,
    ReplaceItems,
    RoomProjectUpdate,
    SetSemanticIntent,
)
from app.schemas.discovery import PriceConstraint, ProductSearchRequest
from app.schemas.query import ConstraintStrength, SemanticPreference
from app.services.agent_state import apply_update, commit_search_results
from app.taxonomy.attributes import AttributeFamily
from pydantic import ValidationError

SAR = "SAR"


def _request(subcategory: str = "sofa") -> ProductSearchRequest:
    return ProductSearchRequest(
        commerce_category="seating", commerce_subcategory=subcategory
    )


def _preference(value: str) -> SemanticPreference:
    return SemanticPreference(
        family=AttributeFamily.STYLE,
        raw_value=value,
        strength=ConstraintStrength.PREFERRED,
    )


def _with_search(**kwargs: object) -> AgentStateV1:
    return apply_update(
        AgentStateV1(),
        AgentStateUpdate(active_search=ActiveSearchUpdate(request=_request(), **kwargs)),
    )


# ── omission is untouched ───────────────────────────────────────────────────


def test_an_empty_update_changes_nothing() -> None:
    state = _with_search(semantic_intent=SetSemanticIntent(value="cosy"))

    assert apply_update(state, AgentStateUpdate()) == state


def test_a_one_domain_update_leaves_the_others_identical() -> None:
    state = AgentStateV1(
        customer_preferences=CustomerPreferenceState(
            semantic_preferences=(_preference("modern"),)
        ),
        active_search=ActiveSearchState(request=_request(), revision=1,
                                        semantic_intent="cosy"),
        product_interaction=ProductInteractionState(selected_product_ids=(5,)),
        room_project=RoomProjectState(room_type="living room"),
        derived_commerce=DerivedCommerceState(purchase_stage=PurchaseStage.EXPLORING),
    )

    after = apply_update(
        state,
        AgentStateUpdate(
            derived_commerce=DerivedCommerceUpdate(
                purchase_stage=PurchaseStage.CONSIDERING
            )
        ),
    )

    assert after.customer_preferences == state.customer_preferences
    assert after.active_search == state.active_search
    assert after.product_interaction == state.product_interaction
    assert after.room_project == state.room_project
    assert after.derived_commerce.purchase_stage is PurchaseStage.CONSIDERING


def test_the_input_state_is_never_mutated() -> None:
    state = _with_search(semantic_intent=SetSemanticIntent(value="cosy"))
    before = state.model_dump()

    apply_update(
        state,
        AgentStateUpdate(
            active_search=ActiveSearchUpdate(semantic_intent=SetSemanticIntent(value="sleek"))
        ),
    )

    assert state.model_dump() == before


# ── tuple operations ────────────────────────────────────────────────────────


def test_add_appends_without_clearing() -> None:
    state = AgentStateV1(
        customer_preferences=CustomerPreferenceState(
            semantic_preferences=(_preference("modern"),)
        )
    )

    after = apply_update(
        state,
        AgentStateUpdate(
            customer_preferences=CustomerPreferenceUpdate(
                semantic_preferences=AddItems(items=(_preference("boho"),))
            )
        ),
    )

    assert [p.raw_value for p in after.customer_preferences.semantic_preferences] == [
        "modern", "boho",
    ]


def test_add_does_not_duplicate_an_existing_item() -> None:
    existing = _preference("modern")
    state = AgentStateV1(
        customer_preferences=CustomerPreferenceState(semantic_preferences=(existing,))
    )

    after = apply_update(
        state,
        AgentStateUpdate(
            customer_preferences=CustomerPreferenceUpdate(
                semantic_preferences=AddItems(items=(existing,))
            )
        ),
    )

    assert len(after.customer_preferences.semantic_preferences) == 1


def test_remove_takes_only_what_it_names() -> None:
    state = AgentStateV1(
        product_interaction=ProductInteractionState(selected_product_ids=(1, 2, 3))
    )

    after = apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                selected_product_ids=RemoveItems(items=(2,))
            )
        ),
    )

    assert after.product_interaction.selected_product_ids == (1, 3)


def test_replace_swaps_the_whole_list() -> None:
    state = AgentStateV1(
        product_interaction=ProductInteractionState(selected_product_ids=(1, 2, 3))
    )

    after = apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                selected_product_ids=ReplaceItems(items=(9,))
            )
        ),
    )

    assert after.product_interaction.selected_product_ids == (9,)


def test_an_omitted_tuple_is_not_cleared() -> None:
    """The failure this whole architecture exists to prevent."""
    state = AgentStateV1(
        room_project=RoomProjectState(
            bundle_product_ids=(1, 2), locked_product_ids=(1,)
        )
    )

    after = apply_update(
        state, AgentStateUpdate(room_project=RoomProjectUpdate(room_type="bedroom"))
    )

    assert after.room_project is not None
    assert after.room_project.bundle_product_ids == (1, 2)
    assert after.room_project.locked_product_ids == (1,)


# ── semantic intent continuity ──────────────────────────────────────────────


def test_the_three_turn_continuity_scenario() -> None:
    """cosy -> unrelated turn -> sleek. The reason semantic_intent exists."""
    state = _with_search(semantic_intent=SetSemanticIntent(value="cosy"))
    assert state.active_search is not None
    assert state.active_search.semantic_intent == "cosy"

    # Turn 2: "show me cheaper ones" — a price change, nothing about wording.
    cheaper = apply_update(
        state,
        AgentStateUpdate(
            active_search=ActiveSearchUpdate(
                request=ProductSearchRequest(
                    commerce_category="seating",
                    commerce_subcategory="sofa",
                    price=PriceConstraint.at_most(Decimal("3000"), SAR),
                )
            )
        ),
    )
    assert cheaper.active_search is not None
    assert cheaper.active_search.semantic_intent == "cosy", "must survive"

    # Turn 3: "actually something sleek instead".
    sleek = apply_update(
        cheaper,
        AgentStateUpdate(
            active_search=ActiveSearchUpdate(
                semantic_intent=SetSemanticIntent(value="sleek")
            )
        ),
    )
    assert sleek.active_search is not None
    assert sleek.active_search.semantic_intent == "sleek"
    assert sleek.active_search.request.price is not None, "price survived too"


def test_clearing_semantic_intent_empties_it() -> None:
    state = _with_search(semantic_intent=SetSemanticIntent(value="cosy"))

    after = apply_update(
        state,
        AgentStateUpdate(
            active_search=ActiveSearchUpdate(semantic_intent=ClearSemanticIntent())
        ),
    )

    assert after.active_search is not None
    assert after.active_search.semantic_intent is None


def test_an_absent_intent_operation_preserves_the_value() -> None:
    """`None` is untouched, never clear — the ambiguity the tagged ops remove."""
    state = _with_search(semantic_intent=SetSemanticIntent(value="cosy"))

    after = apply_update(
        state, AgentStateUpdate(active_search=ActiveSearchUpdate(semantics=None))
    )

    assert after.active_search is not None
    assert after.active_search.semantic_intent == "cosy"


# ── revision ownership ──────────────────────────────────────────────────────


def test_an_update_contract_cannot_carry_a_revision() -> None:
    """Agents must not be able to claim a search executed."""
    assert "revision" not in ActiveSearchUpdate.model_fields
    with pytest.raises(ValidationError):
        ActiveSearchUpdate(revision=9)  # type: ignore[call-arg]


def test_presented_results_cannot_be_set_directly() -> None:
    for field in ("presented_product_ids", "presented_search_revision"):
        assert field not in ProductInteractionUpdate.model_fields


@pytest.mark.parametrize(
    "update",
    [
        ActiveSearchUpdate(semantic_intent=SetSemanticIntent(value="sleek")),
        ActiveSearchUpdate(semantic_intent=ClearSemanticIntent()),
        ActiveSearchUpdate(request=_request("sectional-sofa")),
        ActiveSearchUpdate(semantic_preferences=AddItems(items=(_preference("boho"),))),
    ],
)
def test_changing_criteria_never_advances_the_revision(
    update: ActiveSearchUpdate,
) -> None:
    """Criteria changing is not a search executing."""
    state = _with_search(semantic_intent=SetSemanticIntent(value="cosy"))

    after = apply_update(state, AgentStateUpdate(active_search=update))

    assert after.active_search is not None
    assert after.active_search.revision == 0


def test_an_unrelated_domain_update_never_advances_the_revision() -> None:
    state = _with_search()

    after = apply_update(
        state,
        AgentStateUpdate(
            derived_commerce=DerivedCommerceUpdate(purchase_stage=PurchaseStage.EXPLORING)
        ),
    )

    assert after.active_search is not None
    assert after.active_search.revision == 0


def test_a_new_search_starts_with_no_committed_results() -> None:
    state = _with_search()

    assert state.active_search is not None
    assert state.active_search.revision == 0, "nothing committed yet"
    assert state.product_interaction.presented_search_revision is None
    assert state.product_interaction.presented_product_ids == ()


def test_the_first_commit_produces_revision_one() -> None:
    after = commit_search_results(_with_search(), (11, 22))

    assert after.active_search is not None
    assert after.active_search.revision == 1
    assert after.product_interaction.presented_search_revision == 1


def test_only_a_commit_advances_the_revision() -> None:
    """No agent-proposable update can move the lineage."""
    state = _with_search()

    proposed = apply_update(
        state,
        AgentStateUpdate(
            customer_preferences=CustomerPreferenceUpdate(
                semantic_preferences=AddItems(items=(_preference("boho"),))
            ),
            active_search=ActiveSearchUpdate(
                semantic_intent=SetSemanticIntent(value="airy")
            ),
            product_interaction=ProductInteractionUpdate(
                selected_product_ids=AddItems(items=(5,))
            ),
            room_project=RoomProjectUpdate(room_type="bedroom"),
            derived_commerce=DerivedCommerceUpdate(
                purchase_stage=PurchaseStage.CONSIDERING
            ),
        ),
    )

    assert proposed.active_search is not None
    assert proposed.active_search.revision == 0, "every domain touched, lineage still 0"
    assert commit_search_results(proposed, (1,)).active_search.revision == 1  # type: ignore[union-attr]


def test_an_agent_update_cannot_commit_search_results() -> None:
    """The ownership boundary: proposing state is not executing a search."""
    assert "record_search_results" not in AgentStateUpdate.model_fields
    with pytest.raises(ValidationError):
        AgentStateUpdate(record_search_results={"product_ids": (1,)})  # type: ignore[call-arg]


def test_results_and_revision_commit_atomically() -> None:
    state = _with_search()

    after = commit_search_results(state, (11, 22, 33))

    assert after.product_interaction.presented_product_ids == (11, 22, 33)
    assert after.active_search is not None
    assert (
        after.product_interaction.presented_search_revision
        == after.active_search.revision
    )


def test_a_new_commit_replaces_the_previous_result_set() -> None:
    state = commit_search_results(_with_search(), (1, 2))

    after = commit_search_results(state, (7, 8))

    assert after.product_interaction.presented_product_ids == (7, 8)
    assert after.active_search is not None
    assert after.active_search.revision == 2
    assert after.product_interaction.presented_search_revision == 2


def test_positional_references_survive_until_a_new_search_commits() -> None:
    state = commit_search_results(_with_search(), (11, 22))

    after = apply_update(
        state,
        AgentStateUpdate(
            active_search=ActiveSearchUpdate(semantic_intent=SetSemanticIntent(value="airy"))
        ),
    )

    assert after.product_interaction.presented_product_ids == (11, 22)
    assert after.product_interaction.presented_search_revision == 1


def test_a_commit_clears_a_focus_on_the_previous_result_set() -> None:
    state = commit_search_results(_with_search(), (11, 22))
    state = apply_update(
        state,
        AgentStateUpdate(product_interaction=ProductInteractionUpdate(focused_product_id=22)),
    )
    assert state.product_interaction.focused_product_id == 22

    after = commit_search_results(state, (90,))

    assert after.product_interaction.focused_product_id is None


def test_committing_results_without_a_search_is_refused() -> None:
    with pytest.raises(InvalidRequestError):
        commit_search_results(AgentStateV1(), (1,))


# ── validation cannot be bypassed ───────────────────────────────────────────


def test_an_update_producing_an_invalid_state_is_rejected() -> None:
    """Locked outside the bundle must fail at the transition, not later."""
    state = AgentStateV1(room_project=RoomProjectState(bundle_product_ids=(1,)))

    with pytest.raises(ValidationError):
        apply_update(
            state,
            AgentStateUpdate(
                room_project=RoomProjectUpdate(locked_product_ids=AddItems(items=(99,)))
            ),
        )


def test_an_update_focusing_an_unknown_product_is_rejected() -> None:
    with pytest.raises(ValidationError):
        apply_update(
            AgentStateV1(),
            AgentStateUpdate(
                product_interaction=ProductInteractionUpdate(focused_product_id=404)
            ),
        )


def test_the_result_is_a_fully_revalidated_state() -> None:
    state = apply_update(
        AgentStateV1(),
        AgentStateUpdate(active_search=ActiveSearchUpdate(request=_request())),
    )

    assert isinstance(state, AgentStateV1)
    assert state.schema_version == "agent_state_v1"


def test_a_first_search_without_a_request_is_refused() -> None:
    with pytest.raises(InvalidRequestError):
        apply_update(
            AgentStateV1(),
            AgentStateUpdate(
                active_search=ActiveSearchUpdate(
                    semantic_intent=SetSemanticIntent(value="cosy")
                )
            ),
        )


def test_the_reducer_performs_no_io() -> None:
    """Pure: no database, no provider, no clock."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[2] / "app/services/agent_state.py").read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom | ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    for forbidden in ("openai", "pinecone", "redis", "sqlalchemy", "httpx", "datetime", "random"):
        assert not any(forbidden in name for name in imported), forbidden


# ── scalar clears ───────────────────────────────────────────────────────────


def test_room_scalars_can_be_cleared_explicitly() -> None:
    state = AgentStateV1(
        room_project=RoomProjectState(
            room_type="living room",
            budget=PriceConstraint.at_most(Decimal("12000"), SAR),
        )
    )

    after = apply_update(
        state,
        AgentStateUpdate(
            room_project=RoomProjectUpdate(clear_room_type=True, clear_budget=True)
        ),
    )

    assert after.room_project is not None
    assert after.room_project.room_type is None
    assert after.room_project.budget is None


def test_the_purchase_stage_can_be_reset_to_unassessed() -> None:
    state = AgentStateV1(
        derived_commerce=DerivedCommerceState(purchase_stage=PurchaseStage.CONSIDERING)
    )

    after = apply_update(
        state,
        AgentStateUpdate(derived_commerce=DerivedCommerceUpdate(clear_purchase_stage=True)),
    )

    assert after.derived_commerce.purchase_stage is None


def test_a_commit_preserves_selected_products() -> None:
    state = apply_update(
        _with_search(),
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                selected_product_ids=AddItems(items=(77,))
            )
        ),
    )

    after = commit_search_results(state, (1, 2))

    assert after.product_interaction.selected_product_ids == (77,)


def test_a_commit_preserves_every_other_domain() -> None:
    state = apply_update(
        _with_search(semantic_intent=SetSemanticIntent(value="cosy")),
        AgentStateUpdate(room_project=RoomProjectUpdate(room_type="living room")),
    )

    after = commit_search_results(state, (1,))

    assert after.active_search is not None
    assert after.active_search.semantic_intent == "cosy"
    assert after.room_project == state.room_project
    assert after.customer_preferences == state.customer_preferences


def test_a_presented_revision_of_zero_is_rejected() -> None:
    """A presented result set implies a committed search."""
    from app.schemas.agent_state import ActiveSearchState as _Search

    with pytest.raises(ValidationError):
        AgentStateV1(
            active_search=_Search(request=_request(), revision=0),
            product_interaction=ProductInteractionState(
                presented_product_ids=(1,), presented_search_revision=0
            ),
        )
