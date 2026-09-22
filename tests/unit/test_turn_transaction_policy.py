"""The turn transaction, pinned before a coordinator exists to get it wrong.

    old AgentStateV1 + decision + whatever deterministically succeeded
        = new AgentStateV1

No database, no lock, no persistence - the state is an immutable value, so the
"transaction" is entirely about which reducer calls happen in which order, and
which are skipped when something fails.

These exercise the real reducer. Where a coordinator would call it, the test
calls it in the approved sequence, so the policy is executable rather than
described in a comment.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    PurchaseStage,
)
from app.schemas.agent_updates import (
    ActiveSearchUpdate,
    AddItems,
    AgentStateUpdate,
    CustomerPreferenceUpdate,
    DerivedCommerceUpdate,
    ProductInteractionUpdate,
    RemoveItems,
    ReplaceItems,
    RoomProjectUpdate,
)
from app.schemas.discovery import PriceConstraint, ProductSearchRequest
from app.schemas.query import ConstraintStrength, SemanticPreference
from app.services.agent_state import (
    NO_RESULTS_REVISION,
    apply_update,
    commit_search_results,
)
from app.taxonomy.attributes import AttributeFamily

SOFAS = ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa")
RECLINERS = ProductSearchRequest(
    commerce_category="seating", commerce_subcategory="recliner"
)


def _preference(value: str) -> SemanticPreference:
    return SemanticPreference(
        family=AttributeFamily.STYLE,
        raw_value=value,
        canonical_value=value,
        strength=ConstraintStrength.PREFERRED,
    )


def _promote(state: AgentStateV1, request: ProductSearchRequest) -> AgentStateV1:
    """The approved promotion path: the reducer, never a direct install."""
    return apply_update(
        state, AgentStateUpdate(active_search=ActiveSearchUpdate(request=request))
    )


@pytest.fixture
def searched() -> AgentStateV1:
    """A conversation with one committed search, a focus and a selection."""
    state = commit_search_results(_promote(AgentStateV1(), SOFAS), (10, 11, 12))
    return apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                focused_product_id=11, selected_product_ids=AddItems(items=(11,))
            )
        ),
    )


# ── promotion and revision ──────────────────────────────────────────────────


def test_a_first_search_starts_at_the_no_results_revision() -> None:
    promoted = _promote(AgentStateV1(), SOFAS)

    assert promoted.active_search is not None
    assert promoted.active_search.revision == NO_RESULTS_REVISION
    assert promoted.product_interaction.presented_search_revision is None


def test_committing_advances_the_revision_exactly_once(
    searched: AgentStateV1,
) -> None:
    assert searched.active_search is not None
    before = searched.active_search.revision

    final = commit_search_results(_promote(searched, RECLINERS), (20, 21))

    assert final.active_search is not None
    assert final.active_search.revision == before + 1
    assert final.product_interaction.presented_search_revision == before + 1


def test_changing_criteria_alone_never_advances_the_revision(
    searched: AgentStateV1,
) -> None:
    """Criteria changing is not a search executing."""
    assert searched.active_search is not None
    promoted = _promote(searched, RECLINERS)

    assert promoted.active_search is not None
    assert promoted.active_search.revision == searched.active_search.revision


def test_the_update_contract_cannot_set_a_revision() -> None:
    """No agent, and no coordinator shortcut, may claim a search executed."""
    assert "revision" not in ActiveSearchUpdate.model_fields


def test_a_candidate_revision_must_match_the_committed_lineage(
    searched: AgentStateV1,
) -> None:
    """What `seed_new_task(revision=...)` must be given, so the candidate and
    the reducer cannot disagree about where the lineage stands."""
    assert searched.active_search is not None
    seed_revision = searched.active_search.revision

    candidate = ActiveSearchState(request=RECLINERS, revision=seed_revision)
    promoted = _promote(searched, RECLINERS)

    assert promoted.active_search is not None
    assert candidate.revision == promoted.active_search.revision


def test_the_seed_revision_is_zero_when_no_search_is_active() -> None:
    state = AgentStateV1()

    seed_revision = (
        state.active_search.revision if state.active_search else NO_RESULTS_REVISION
    )

    assert seed_revision == 0


# ── the intermediate must not escape ────────────────────────────────────────


def test_the_intermediate_pairs_new_criteria_with_the_old_presentation(
    searched: AgentStateV1,
) -> None:
    """It validates structurally - which is exactly why it needs a rule.

    `AgentStateV1` only checks that the revision matches, and promotion does
    not move the revision, so nothing stops this object existing. It is
    semantically wrong: the presented products belong to the previous criteria.
    A coordinator may hold it locally; it must never return it.
    """
    intermediate = _promote(searched, RECLINERS)

    assert intermediate.active_search is not None
    assert intermediate.active_search.request.commerce_subcategory == "recliner"
    assert intermediate.product_interaction.presented_product_ids == (10, 11, 12)


def test_committing_closes_the_intermediate_in_one_step(
    searched: AgentStateV1,
) -> None:
    """The only approved exit: promote then commit, with nothing returned in
    between."""
    final = commit_search_results(_promote(searched, RECLINERS), (20, 21))

    assert final.active_search is not None
    assert final.active_search.request.commerce_subcategory == "recliner"
    assert final.product_interaction.presented_product_ids == (20, 21)
    assert (
        final.product_interaction.presented_search_revision
        == final.active_search.revision
    )


# ── success, including zero results ─────────────────────────────────────────


def test_a_search_keeps_a_focus_on_something_they_chose(
    searched: AgentStateV1,
) -> None:
    """A choice outlives the search that surfaced it.

    The fixture's focus is on product 11, which is also selected. Clearing it
    left a customer who had picked a sofa, been offered tables and picked one
    of those with no answer to "show me the one I picked" - the cross-sell
    search wiped the focus each time (M20 1).
    """
    final = commit_search_results(_promote(searched, RECLINERS), (20, 21))

    assert final.product_interaction.focused_product_id == 11
    assert final.product_interaction.selected_product_ids == (11,)


def test_a_search_still_clears_a_focus_on_the_previous_results(
    searched: AgentStateV1,
) -> None:
    """The other half of the rule, and the reason it existed.

    Product 12 was presented and never chosen, so once new results replace the
    list it names nothing the customer can see.
    """
    browsing = apply_update(
        searched,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(focused_product_id=12)
        ),
    )

    final = commit_search_results(_promote(browsing, RECLINERS), (20, 21))

    assert final.product_interaction.focused_product_id is None


def test_a_zero_result_search_is_still_a_committed_search(
    searched: AgentStateV1,
) -> None:
    """It ran and matched nothing. Not a failure (CLAUDE.md 13.5)."""
    assert searched.active_search is not None
    before = searched.active_search.revision

    final = commit_search_results(_promote(searched, RECLINERS), ())

    assert final.active_search is not None
    assert final.active_search.revision == before + 1
    assert final.product_interaction.presented_product_ids == ()
    assert final.product_interaction.presented_search_revision == before + 1
    assert final.product_interaction.selected_product_ids == (11,)


# ── handled failure: independent updates survive ────────────────────────────


def _independent_updates() -> AgentStateUpdate:
    """What a decision produced that a failed search does not invalidate."""
    return AgentStateUpdate(
        customer_preferences=CustomerPreferenceUpdate(
            semantic_preferences=AddItems(items=(_preference("Modern"),))
        ),
        product_interaction=ProductInteractionUpdate(
            selected_product_ids=AddItems(items=(12,))
        ),
        room_project=RoomProjectUpdate(room_type="living room"),
        derived_commerce=DerivedCommerceUpdate(purchase_stage=PurchaseStage.CONSIDERING),
    )


def test_a_handled_search_failure_preserves_the_whole_search_lineage(
    searched: AgentStateV1,
) -> None:
    """Applied to the pre-search state, so the candidate is never promoted."""
    assert searched.active_search is not None
    final = apply_update(searched, _independent_updates())

    assert final.active_search is not None
    assert final.active_search.request.commerce_subcategory == "sofa"
    assert final.active_search.revision == searched.active_search.revision
    assert final.product_interaction.presented_product_ids == (10, 11, 12)
    assert (
        final.product_interaction.presented_search_revision
        == searched.product_interaction.presented_search_revision
    )
    assert final.product_interaction.focused_product_id == 11


def test_a_handled_search_failure_keeps_the_independent_updates(
    searched: AgentStateV1,
) -> None:
    """The reason a result object exists: raising would lose all of this."""
    final = apply_update(searched, _independent_updates())

    assert [p.canonical_value for p in final.customer_preferences.semantic_preferences] == [
        "Modern"
    ]
    assert final.product_interaction.selected_product_ids == (11, 12)
    assert final.room_project is not None
    assert final.room_project.room_type == "living room"
    assert final.derived_commerce.purchase_stage is PurchaseStage.CONSIDERING


def test_an_m7_failure_behaves_exactly_like_a_pipeline_failure(
    searched: AgentStateV1,
) -> None:
    """Same transaction: no candidate exists, so nothing is promoted, and the
    proposals came from a decision that succeeded."""
    final = apply_update(searched, _independent_updates())

    assert final.active_search is not None
    assert final.active_search.request.commerce_subcategory == "sofa"
    assert final.derived_commerce.purchase_stage is PurchaseStage.CONSIDERING


# ── interaction failure: value-first ────────────────────────────────────────


def test_a_failed_optional_interaction_does_not_block_the_search(
    searched: AgentStateV1,
) -> None:
    """Ambiguous SELECT beside a valid SEARCH: the selection is omitted, the
    search still commits, and the unresolved selection is reported separately.
    """
    before = searched.product_interaction.selected_product_ids

    # The interaction resolved to nothing, so no ProductInteractionUpdate.
    final = commit_search_results(_promote(searched, RECLINERS), (20, 21))

    assert final.product_interaction.selected_product_ids == before
    assert final.product_interaction.presented_product_ids == (20, 21)


def test_a_resolved_interaction_is_applied_before_the_search_commits(
    searched: AgentStateV1,
) -> None:
    """Pre-turn resolution, then commit - so a selection made from the old list
    survives the new one replacing it."""
    with_selection = apply_update(
        searched,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                selected_product_ids=AddItems(items=(10,))
            )
        ),
    )

    final = commit_search_results(_promote(with_selection, RECLINERS), (20, 21))

    assert final.product_interaction.selected_product_ids == (11, 10)
    assert final.product_interaction.presented_product_ids == (20, 21)


def test_a_deselect_also_survives_the_commit(searched: AgentStateV1) -> None:
    deselected = apply_update(
        searched,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                selected_product_ids=RemoveItems(items=(11,))
            )
        ),
    )

    final = commit_search_results(_promote(deselected, RECLINERS), (20, 21))

    assert final.product_interaction.selected_product_ids == ()


# ── proposals on a clarify turn ─────────────────────────────────────────────


def test_proposals_apply_on_a_clarify_turn(searched: AgentStateV1) -> None:
    """One fact needing clarification does not discard the others the customer
    stated in the same breath."""
    final = apply_update(
        searched,
        AgentStateUpdate(
            customer_preferences=CustomerPreferenceUpdate(
                semantic_preferences=ReplaceItems(items=(_preference("Japandi"),))
            ),
            derived_commerce=DerivedCommerceUpdate(purchase_stage=PurchaseStage.EXPLORING),
        ),
    )

    assert final.customer_preferences.semantic_preferences[0].canonical_value == (
        "Japandi"
    )
    assert final.derived_commerce.purchase_stage is PurchaseStage.EXPLORING
    assert final.active_search is not None
    assert final.product_interaction.presented_product_ids == (10, 11, 12)


# ── product detail focus ────────────────────────────────────────────────────


def test_product_detail_focus_is_the_ordinary_interaction_update(
    searched: AgentStateV1,
) -> None:
    """No second focus mechanism, and the resolved product is always focusable
    because every selector resolves out of presented or selected."""
    final = apply_update(
        searched,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(focused_product_id=12)
        ),
    )

    assert searched.active_search is not None
    assert final.active_search is not None
    assert final.product_interaction.focused_product_id == 12
    assert final.active_search.revision == searched.active_search.revision


# ── a budget the proposal could carry, once a currency is known ─────────────


def test_a_room_budget_with_a_currency_maps_cleanly(searched: AgentStateV1) -> None:
    final = apply_update(
        searched,
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                budget=PriceConstraint(currency="SAR", max_amount=Decimal("12000"))
            )
        ),
    )

    assert final.room_project is not None
    assert final.room_project.budget is not None
    assert final.room_project.budget.currency == "SAR"


def test_other_proposal_fields_survive_a_budget_clarification(
    searched: AgentStateV1,
) -> None:
    """The budget is withheld pending its currency; the room type is not."""
    final = apply_update(
        searched, AgentStateUpdate(room_project=RoomProjectUpdate(room_type="bedroom"))
    )

    assert final.room_project is not None
    assert final.room_project.room_type == "bedroom"
    assert final.room_project.budget is None
