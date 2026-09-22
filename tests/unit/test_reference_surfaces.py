"""Which product a customer is pointing at, when the screen has more than one
way to count.

Reconstructed from a real session. Five sofas were found, sofas 3 and 5 were
compared, and the customer said "I like the second one" meaning the second
*column*. Four things went wrong in a row, and this file is one section per
defect:

1. the contract could only express a position in the result list, so the
   comparison's own numbering had nowhere to go;
2. a later search replaced the sofas with centre tables, and "sofa 5" resolved
   against the new list - silently selecting a coffee table;
3. the correction never moved the focus, so "show me the one I selected" kept
   returning the product they had just rejected;
4. their opening budget, "under 2000 SAR", was recorded as a household of two.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    ProductInteractionIntent,
    ProductInteractionOp,
)
from app.schemas.agent_state import AgentStateV1, ProductInteractionState
from app.schemas.agent_updates import AgentStateUpdate, ProductInteractionUpdate
from app.schemas.product_reference import ComparedOrdinal, PresentedOrdinal
from app.schemas.resolution import ReferenceFailureReason
from app.services.agent_state import apply_update
from pydantic import ValidationError

SOFAS = (101, 102, 103, 104, 105)
COMPARED = (103, 105)
"""Sofas 3 and 5 - columns one and two of the table they were reading."""

TABLES = (201, 202, 203, 204, 205)


# ── 1. a comparison has a numbering of its own ──────────────────────────────


def test_the_state_records_the_comparison_in_column_order() -> None:
    state = apply_update(
        AgentStateV1(),
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(compared_product_ids=COMPARED)
        ),
    )

    assert state.product_interaction.compared_product_ids == COMPARED


def test_a_compared_ordinal_counts_columns_not_results() -> None:
    """The defect in one assertion.

    Column two of a comparison of sofas 3 and 5 is sofa 5. Counted in the
    result list, position two is sofa 2 - a product they never compared.
    """
    assert ComparedOrdinal(position=2).position == 2
    assert ComparedOrdinal(position=2).kind == "compared_ordinal"
    assert PresentedOrdinal(position=2).kind == "presented_ordinal"


def test_the_two_numberings_are_different_members_of_the_union() -> None:
    """Not a flag on one selector: a reader that forgot to check the flag
    would resolve against the wrong surface, which is the bug itself.

    Separate members make the resolver's match exhaustive, so a new surface
    cannot be silently handled as an old one.
    """
    from typing import get_args

    from app.schemas.product_reference import ProductReferenceSelector

    members = {member.__name__ for member in get_args(get_args(ProductReferenceSelector)[0])}

    assert {"PresentedOrdinal", "ComparedOrdinal"} <= members

    # Distinct tags, so a JSON payload names its surface unambiguously. The
    # types are distinct too - mypy proves that statically, which is why it is
    # not asserted here.
    tags = {
        member.model_fields["kind"].default
        for member in get_args(get_args(ProductReferenceSelector)[0])
    }
    assert len(tags) == len(members), "every surface has its own tag"


def test_a_comparison_outlives_the_results_behind_it() -> None:
    """A cross-sell search replaced the sofas while the table stayed on screen.

    "The one from the comparison" has to keep meaning the same product when
    that happens, so committing new results carries the comparison forward.
    """
    from app.schemas.agent_state import ActiveSearchState
    from app.schemas.discovery import ProductSearchRequest
    from app.services.agent_state import commit_search_results

    state = AgentStateV1(
        active_search=ActiveSearchState(
            request=ProductSearchRequest(commerce_category="seating"), revision=1
        ),
        product_interaction=ProductInteractionState(
            presented_product_ids=SOFAS,
            presented_search_revision=1,
            compared_product_ids=COMPARED,
        ),
    )

    after = commit_search_results(state, TABLES)

    assert after.product_interaction.presented_product_ids == TABLES
    assert after.product_interaction.compared_product_ids == COMPARED


def test_a_new_comparison_replaces_rather_than_merges() -> None:
    """Two tables at once would restore the ambiguity this removes."""
    state = apply_update(
        AgentStateV1(),
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(compared_product_ids=COMPARED)
        ),
    )
    later = apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(compared_product_ids=(101, 104))
        ),
    )

    assert later.product_interaction.compared_product_ids == (101, 104)


def test_an_unrelated_turn_leaves_the_comparison_alone() -> None:
    """It stays on screen until something replaces it."""
    state = apply_update(
        AgentStateV1(),
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(compared_product_ids=COMPARED)
        ),
    )
    later = apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(focused_product_id=None)
        ),
    )

    assert later.product_interaction.compared_product_ids == COMPARED


# ── 2. a position is not enough when the list changed ───────────────────────


def test_an_interaction_may_say_what_kind_it_points_at() -> None:
    intent = ProductInteractionIntent(
        op=ProductInteractionOp.SELECT,
        reference=PresentedOrdinal(position=5),
        expected_subcategory="sofa",
    )

    assert intent.expected_subcategory == "sofa"


def test_naming_no_kind_is_the_ordinary_case() -> None:
    """"The second one" names a position and nothing else, and must keep
    working without an expectation attached."""
    intent = ProductInteractionIntent(
        op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=2)
    )

    assert intent.expected_subcategory is None


def test_the_mismatch_has_its_own_reason() -> None:
    """Distinct from a position that does not exist. Position five existed;
    it was a centre table."""
    reasons = {reason.value for reason in ReferenceFailureReason}

    assert {"kind_mismatch", "ordinal_out_of_range", "no_comparison"} <= reasons


def test_the_registry_answers_whether_a_kind_is_approved() -> None:
    from app.taxonomy.registry import load_taxonomy

    taxonomy = load_taxonomy()

    assert taxonomy.is_subcategory("sofa")
    assert taxonomy.is_subcategory("center-table")
    assert not taxonomy.is_subcategory("luxury-couch")


# ── 3. choosing something makes it the one under discussion ─────────────────


def test_selecting_takes_focus() -> None:
    """"Show me the one I selected" followed a focus set turns earlier, so a
    customer who corrected their choice was shown what they had rejected."""
    from app.schemas.agent_state import ActiveSearchState
    from app.schemas.discovery import ProductSearchRequest
    from app.services.turn_coordinator import _interaction_update

    state = AgentStateV1(
        active_search=ActiveSearchState(
            request=ProductSearchRequest(commerce_category="seating"), revision=1
        ),
        product_interaction=ProductInteractionState(
            presented_product_ids=SOFAS,
            presented_search_revision=1,
            focused_product_id=102,
            selected_product_ids=(102,),
        ),
    )

    update = _interaction_update(ProductInteractionOp.SELECT, 105, state)
    after = apply_update(state, AgentStateUpdate(product_interaction=update))

    assert after.product_interaction.focused_product_id == 105
    assert 105 in after.product_interaction.selected_product_ids


def test_a_focus_may_rest_on_a_compared_product() -> None:
    """A compared product need not still be in the result list, so the state
    has to count it as something the customer has seen."""
    state = ProductInteractionState(
        presented_product_ids=TABLES,
        presented_search_revision=2,
        compared_product_ids=COMPARED,
        focused_product_id=105,
    )

    assert state.focused_product_id == 105


def test_a_focus_on_an_unseen_product_is_still_refused() -> None:
    with pytest.raises(ValidationError, match="presented or selected"):
        ProductInteractionState(
            presented_product_ids=SOFAS,
            presented_search_revision=1,
            focused_product_id=999,
        )


# ── 4. a price is not a household ───────────────────────────────────────────


def test_the_agent_is_told_a_figure_is_not_a_seat_count() -> None:
    """"I am looking for under 2000 SAR" was recorded as two people, and every
    recommendation afterwards was sized for a household nobody mentioned.

    Asserted against the **prompt**, because that is what the model actually
    reads: attribute docstrings on the proposal do not reach the provider's
    JSON schema, so a rule written only there would guide nothing.
    """
    from app.prompts.customer_commerce.v1 import INSTRUCTIONS

    flat = " ".join(INSTRUCTIONS.split())

    assert "WHAT THEY SAID, NOT WHAT IT RESEMBLES" in flat
    assert "A price is a price" in flat
    assert "If you are unsure which fact a number is, record none of them" in flat


def test_that_rule_is_documented_where_the_field_is_defined_too() -> None:
    """For whoever reads the contract next. It is documentation, not the
    guidance - the test above covers the part the model sees."""
    source = (
        Path(__file__).parents[2] / "app/schemas/agent_decision.py"
    ).read_text()

    assert "never from a figure that is not about people" in source.lower()


def test_the_seating_count_is_still_recordable_when_they_say_it() -> None:
    """The fix narrows what may be inferred; it does not stop the fact being
    recorded when it is actually stated."""
    from app.schemas.agent_decision import CustomerStateProposal

    proposal = CustomerStateProposal(regular_seating_count=5)

    assert proposal.regular_seating_count == 5


# ── the decision contract still holds ───────────────────────────────────────


def test_a_compared_ordinal_is_usable_wherever_a_reference_is() -> None:
    detail = CustomerAgentDecision(
        action=AgentAction.PRODUCT_DETAIL, reference=ComparedOrdinal(position=2)
    )

    assert isinstance(detail.reference, ComparedOrdinal)
    assert "product_id" not in detail.model_dump_json()


def test_no_selector_carries_a_product_id() -> None:
    for selector in (ComparedOrdinal(position=1), PresentedOrdinal(position=1)):
        assert "product_id" not in selector.model_dump_json()
        assert "id" not in set(type(selector).model_fields) - {"kind", "position"}


# ── the guard, through the coordinator ──────────────────────────────────────


async def _interaction_turn(
    intent: ProductInteractionIntent, *, presented: tuple[int, ...], resolved: int
) -> tuple[Any, dict[str, Any]]:
    """One turn whose only work is the interaction."""
    from app.schemas.resolution import ResolvedProductReference

    from tests.unit.test_turn_coordinator import (
        FakeHydration,
        FakeReferences,
        _coordinator,
        _state,
        _turn,
    )

    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER, interaction=intent),
        references=FakeReferences(default=ResolvedProductReference(product_id=resolved)),
        hydration=FakeHydration(available=presented),
    )
    result = await coordinator.run(_turn(_state(presented=presented)))
    return result, parts


async def test_a_position_that_resolved_to_another_kind_is_refused() -> None:
    """The defect, end to end.

    "Sofa 5" while the list holds centre tables: position five exists, so
    nothing is out of range, and the product at it is not a sofa. Refused and
    asked about, rather than selected (M15 2).
    """
    result, _ = await _interaction_turn(
        ProductInteractionIntent(
            op=ProductInteractionOp.SELECT,
            reference=PresentedOrdinal(position=5),
            expected_subcategory="carpet",
        ),
        presented=(10, 11),
        resolved=10,
    )

    assert 10 not in result.state.product_interaction.selected_product_ids
    assert result.grounding.deterministic_clarification is not None


async def test_the_matching_kind_goes_through() -> None:
    """The fixture's products are sofas, so naming one changes nothing."""
    result, _ = await _interaction_turn(
        ProductInteractionIntent(
            op=ProductInteractionOp.SELECT,
            reference=PresentedOrdinal(position=1),
            expected_subcategory="sofa",
        ),
        presented=(10, 11),
        resolved=10,
    )

    assert 10 in result.state.product_interaction.selected_product_ids


async def test_naming_nothing_still_selects() -> None:
    """"The second one" carries no expectation, and must not be refused for
    lacking one."""
    result, _ = await _interaction_turn(
        ProductInteractionIntent(
            op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=1)
        ),
        presented=(10, 11),
        resolved=10,
    )

    assert 10 in result.state.product_interaction.selected_product_ids


async def test_a_kind_the_registry_does_not_know_is_not_a_refusal() -> None:
    """The model must never introduce a taxonomy value, and the customer must
    not lose their turn because it did. An unapproved word is treated as no
    expectation - it can never *pass* the check, only skip it (CLAUDE.md 14.3).
    """
    result, _ = await _interaction_turn(
        ProductInteractionIntent(
            op=ProductInteractionOp.SELECT,
            reference=PresentedOrdinal(position=1),
            expected_subcategory="luxury-couch",
        ),
        presented=(10, 11),
        resolved=10,
    )

    assert 10 in result.state.product_interaction.selected_product_ids
