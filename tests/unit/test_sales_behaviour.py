"""What makes ZORY a salesperson rather than a search box.

Four claims, each of which the UAT found the service failing before this pass:

* a search turn may also ask something, and the subject is typed rather than
  written as prose by the decision model;
* how many people use a room is a durable requirement, not a furniture count,
  and it survives the turns between being said and being used;
* a piece the customer settles on earns the *next* furnishing role, chosen by
  the design specialist and found in the real catalog - one step, not a room;
* none of that gives any model a product, a price or an identity it did not
  already have.
"""

from __future__ import annotations

import pytest
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    CustomerStateProposal,
    DesignScope,
    FollowUpGoal,
    FollowUpPolicy,
)
from app.schemas.agent_state import AGENT_STATE_VERSION, AgentStateV1, RoomProjectState
from app.schemas.agent_updates import AgentStateUpdate, RoomProjectUpdate
from app.schemas.design import DesignTask, InteriorDesignRequest
from app.schemas.response import ResponseGroundingView
from app.services.agent_state import apply_update
from app.services.agent_view import project_state
from pydantic import ValidationError

# ── the question is typed, never prose ──────────────────────────────────────


def test_the_decision_names_a_subject_and_never_the_wording() -> None:
    """The split the goal exists for: the agent knows what is worth asking,
    the response model knows how to ask it."""
    goals = {g.value for g in FollowUpGoal}

    assert goals == {
        "budget",
        "room_size",
        "style",
        "seating_requirement",
        "use_case",
        "product_preference",
        "room_completion",
        # M16: a design answer should lead somewhere. "Shall I find rugs that
        # size?" is the step the answer earns, and ending without it leaves
        # the customer to start again themselves (CLAUDE.md 45).
        "product_search",
    }
    for goal in FollowUpGoal:
        assert " " not in goal.value, "a goal is a subject, not a sentence"


def test_a_silent_turn_carries_no_subject() -> None:
    """Refused rather than ignored: a goal on a turn that may not ask means the
    model meant to ask and the policy says otherwise."""
    with pytest.raises(ValidationError):
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            follow_up_policy=FollowUpPolicy.NONE,
            follow_up_goal=FollowUpGoal.BUDGET,
        )


def test_a_search_may_both_show_and_ask() -> None:
    """The whole point: not questions *or* products."""
    decision = CustomerAgentDecision(
        action=AgentAction.SEARCH, follow_up_goal=FollowUpGoal.SEATING_REQUIREMENT
    )

    assert decision.follow_up_policy is FollowUpPolicy.OPTIONAL
    assert decision.follow_up_goal is FollowUpGoal.SEATING_REQUIREMENT


# ── seating is a requirement, not a quantity ────────────────────────────────


def test_the_seating_requirement_is_durable_state() -> None:
    """History is trimmed; this is not. A customer who said "family of five"
    while giving a budget must not be asked again when the room is planned."""
    state = apply_update(
        AgentStateV1(),
        AgentStateUpdate(room_project=RoomProjectUpdate(regular_seating_count=5)),
    )

    room = state.room_project
    assert isinstance(room, RoomProjectState)
    assert room.regular_seating_count == 5
    assert state.schema_version == AGENT_STATE_VERSION == "agent_state_v5"


def test_it_survives_a_later_unrelated_update() -> None:
    """The turn that sets a budget must not clear the seating it was said with."""
    state = apply_update(
        AgentStateV1(),
        AgentStateUpdate(room_project=RoomProjectUpdate(regular_seating_count=5)),
    )
    later = apply_update(
        state, AgentStateUpdate(room_project=RoomProjectUpdate(room_type="living room"))
    )

    room = later.room_project
    assert isinstance(room, RoomProjectState)
    assert room.regular_seating_count == 5


def test_the_customer_states_it_and_nothing_infers_it() -> None:
    """Only from what they said - never from a room type, a product's capacity,
    or how many pieces are in their bundle."""
    assert "regular_seating_count" in CustomerStateProposal.model_fields
    assert "clear_regular_seating_count" in CustomerStateProposal.model_fields


def test_the_agent_can_see_it_so_it_does_not_ask_twice() -> None:
    state = apply_update(
        AgentStateV1(),
        AgentStateUpdate(room_project=RoomProjectUpdate(regular_seating_count=5)),
    )

    view = project_state(state).room_project
    assert view is not None
    assert view.regular_seating_count == 5


def test_the_specialist_is_given_the_requirement_not_a_furniture_count() -> None:
    """Five regular users is a room requirement. What satisfies it - a
    sectional, a sofa and two chairs, two sofas - is the specialist's to decide
    from what this retailer stocks (CLAUDE.md 10.1)."""
    assert "regular_seating_count" in InteriorDesignRequest.model_fields

    for forbidden in ("sofa_count", "sofa_quantity", "seat_count_per_product"):
        assert forbidden not in InteriorDesignRequest.model_fields


def test_nothing_maps_household_size_onto_a_quantity() -> None:
    """The rule that must never appear: family of five -> two sofas."""
    from pathlib import Path

    app = Path(__file__).parents[2] / "app"
    for module in app.rglob("*.py"):
        source = module.read_text()
        for forbidden in (
            "regular_seating_count //",
            "regular_seating_count /",
            "regular_seating_count *",
            "seats_per_sofa",
        ):
            assert forbidden not in source, f"{module.name}: {forbidden}"


# ── the next piece, not a room ──────────────────────────────────────────────


def test_a_complement_is_its_own_task() -> None:
    """A customer who likes a sofa has not asked to furnish a room."""
    assert DesignTask.COMPLEMENTARY_RECOMMENDATION
    assert {t.value for t in DesignTask} == {
        "general_advice",
        "room_plan",
        "complementary_recommendation",
    }


def test_a_complement_needs_the_piece_it_complements() -> None:
    """Without an anchor the question becomes "what furniture is nice", which
    is a different question."""
    from tests.unit.test_design_revision import _capabilities

    with pytest.raises(ValidationError):
        InteriorDesignRequest(
            task=DesignTask.COMPLEMENTARY_RECOMMENDATION,
            design_brief="what else?",
            catalog_capabilities=_capabilities(("seating", "sofa")),
        )


def test_a_complement_carries_a_short_ordered_shortlist() -> None:
    """One role is shown; the others are fallbacks for a thin category.

    The cap was two when the first role was the only one ever run. It is three
    now because a role the retailer *supports* can still return nothing - which
    is how a customer came to see an empty screen and a remark about lounge
    chairs they had never mentioned (CLAUDE.md 26).

    Small enough that the specialist still has to choose. It is not a licence
    to furnish a room nobody asked about: everything past the first viable role
    is discarded.
    """
    from app.services.interior_design import MAX_COMPLEMENTARY_NEEDS

    assert MAX_COMPLEMENTARY_NEEDS == 3


def test_the_handoff_says_which_scope_it_means() -> None:
    """Three extents of the same capability: the whole room, one piece beside
    another, or neither - a question about rooms in general (CLAUDE.md 36)."""
    assert {s.value for s in DesignScope} == {"whole_room", "complement", "advice"}
    assert CustomerAgentDecision(action=AgentAction.SEARCH).design_scope is (DesignScope.WHOLE_ROOM)


def test_a_complement_and_a_revision_are_not_asked_for_together() -> None:
    """One adds a piece beside the room; the other rewrites the plan."""
    from app.schemas.agent_decision import DesignRevisionIntent
    from app.schemas.bundle_reference import DesignNeedCategoryMatch

    with pytest.raises(ValidationError):
        CustomerAgentDecision(
            action=AgentAction.DESIGN_HANDOFF,
            design_scope=DesignScope.COMPLEMENT,
            design_revision=DesignRevisionIntent(
                removed_needs=(DesignNeedCategoryMatch(commerce_category="decor"),)
            ),
        )


def test_no_third_reasoning_agent_was_added() -> None:
    """Cross-sell is the same two agents doing what they already do."""
    from pathlib import Path

    app = Path(__file__).parents[2] / "app"
    for forbidden in ("CrossSellAgent", "RecommendationAgent", "MerchandisingAgent"):
        for module in app.rglob("*.py"):
            assert forbidden not in module.read_text(), forbidden


def test_no_merchandising_relationship_is_claimed() -> None:
    """Nothing records which products are bought together, so nothing may say
    so. The complement is design reasoning, not sales correlation."""
    import ast
    from pathlib import Path

    app = Path(__file__).parents[2] / "app"
    # Identifiers, not prose. Prompts must be able to name what they forbid,
    # and so must the docstring on `COMPLEMENTARY_RECOMMENDATION` explaining
    # why nothing records this - a guard that read words would fire on the
    # very sentences preventing the claim.
    for module in app.rglob("*.py"):
        tree = ast.parse(module.read_text())
        names = {
            (node.id if isinstance(node, ast.Name) else node.attr).lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
        }
        for forbidden in (
            "bought_together",
            "also_bought",
            "frequently_bought",
            "co_purchase",
            "affinity",
        ):
            assert not {n for n in names if forbidden in n}, f"{module.name}: {forbidden}"


def test_the_prompt_forbids_the_claim_rather_than_making_it() -> None:
    """The exclusion above is safe only because the prompt states the ban."""
    from pathlib import Path

    prompt = (Path(__file__).parents[2] / "app/prompts/customer_commerce/v1.py").read_text()

    assert "Never say another customer bought it" in prompt
    assert "frequently bought together" in prompt


# ── the response model gained nothing it could misuse ───────────────────────


@pytest.mark.parametrize(
    "forbidden",
    [
        "product_id",
        "price_amount",
        "unit_price",
        "total",
        "url",
        "store",
        "line_id",
        "need_id",
        "score",
    ],
)
def test_the_richer_grounding_carries_no_value(forbidden: str) -> None:
    """Category and subcategory name a *kind*. Everything that could become a
    claim about an item stayed out.

    `relative_price_reason` survives this list on purpose: it is a reason enum
    saying *why* a comparative price could not be resolved, and carries no
    figure. The tokens here are the ones that would be a value.
    """
    for name in ResponseGroundingView.model_fields:
        assert forbidden not in name, name


def test_the_grounding_says_what_kind_without_saying_which_one() -> None:
    view = ResponseGroundingView(
        kind=__import__(
            "app.schemas.response", fromlist=["ResponseOutcomeKind"]
        ).ResponseOutcomeKind.SEARCH_RESULTS,
        presented_count=5,
        commerce_category="seating",
        commerce_subcategory="sofa",
        exact_match_count=5,
    )

    assert view.commerce_category == "seating"
    assert view.presented_count == 5
