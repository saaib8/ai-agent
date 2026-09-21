"""The decision contract: what each action may carry, and what it may not.

Composite turns are bounded here rather than in a prompt. A decision can hold
one execution action and at most one interaction, so the shape itself refuses
to describe a plan - there is no list of steps to grow into a tool loop.
"""

from __future__ import annotations

import pytest
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CommercialReason,
    CustomerAgentDecision,
    CustomerStateProposal,
    DerivedCommerceProposal,
    ExtremumDirection,
    FocusedProduct,
    FollowUpPolicy,
    NewSearchProposal,
    PreferenceProposal,
    PreferenceProposalOp,
    PresentedAttributeMatch,
    PresentedExtremum,
    PresentedOrdinal,
    PriceProposal,
    ProductInteractionIntent,
    ProductInteractionOp,
    SoleSelectedProduct,
)
from app.schemas.agent_state import PurchaseStage
from app.schemas.conversation import (
    ConversationContext,
    ConversationMessage,
    ConversationRole,
)
from app.schemas.query import ConstraintStrength, SemanticPreference
from app.schemas.refinement import (
    PriceRefinement,
    RefinementOp,
    SearchRefinementDelta,
    SemanticIntentOp,
    SemanticIntentRefinement,
)
from app.taxonomy.attributes import AttributeFamily
from pydantic import BaseModel, ValidationError

CLARIFICATION = BlockingClarification(
    reason=BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE,
    question="What kind of table did you have in mind?",
)
SET_INTENT = SemanticIntentRefinement(op=SemanticIntentOp.SET, value="cosy")
CHEAPER = SearchRefinementDelta(
    price=PriceRefinement(op=RefinementOp.SET, max_amount="3000", currency="SAR")
)


# ── conversation ────────────────────────────────────────────────────────────


def test_history_holds_prior_user_and_assistant_messages() -> None:
    conversation = ConversationContext(
        messages=(
            ConversationMessage(role=ConversationRole.USER, content="show me sofas"),
            ConversationMessage(role=ConversationRole.ASSISTANT, content="here are 3"),
        )
    )

    assert [m.role for m in conversation.messages] == [
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    ]


def test_a_customer_may_repeat_themselves_word_for_word() -> None:
    """Without message identity a genuine repeat is indistinguishable from a
    caller bug, and refusing it would break an ordinary conversation."""
    repeated = ConversationMessage(role=ConversationRole.USER, content="show me sofas")

    conversation = ConversationContext(messages=(repeated, repeated))

    assert len(conversation.messages) == 2


def test_history_carries_no_transport_metadata() -> None:
    assert set(ConversationMessage.model_fields) == {"role", "content"}


def test_an_empty_message_is_refused() -> None:
    with pytest.raises(ValidationError):
        ConversationMessage(role=ConversationRole.USER, content="")


# ── one action, one payload ─────────────────────────────────────────────────


def test_an_answer_carries_no_execution_payload() -> None:
    decision = CustomerAgentDecision(action=AgentAction.ANSWER)

    assert decision.refinement is None
    assert decision.comparison_references == ()


@pytest.mark.parametrize(
    "action", [AgentAction.ANSWER, AgentAction.SEARCH, AgentAction.COMPARE]
)
def test_only_a_refinement_may_carry_a_delta(action: AgentAction) -> None:
    with pytest.raises(ValidationError, match="only a refinement"):
        CustomerAgentDecision(action=action, refinement=CHEAPER)


def test_a_refinement_needs_something_to_change() -> None:
    with pytest.raises(ValidationError, match="delta or a taxonomy change"):
        CustomerAgentDecision(action=AgentAction.REFINE_SEARCH)


def test_an_empty_delta_is_not_a_refinement() -> None:
    with pytest.raises(ValidationError, match="changes nothing"):
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH, refinement=SearchRefinementDelta()
        )


def test_a_taxonomy_change_alone_is_a_valid_refinement() -> None:
    """The model says the product type changed; M7 says what it changed to."""
    decision = CustomerAgentDecision(
        action=AgentAction.REFINE_SEARCH, taxonomy_change_requested=True
    )

    assert decision.refinement is None


def test_the_decision_cannot_name_a_taxonomy_value() -> None:
    names = set(CustomerAgentDecision.model_fields)

    assert "commerce_category" not in names
    assert "commerce_subcategory" not in names
    assert "commerce_category" not in set(SearchRefinementDelta.model_fields)


def test_only_a_search_may_carry_a_new_search_proposal() -> None:
    with pytest.raises(ValidationError, match="only a search"):
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            new_search=NewSearchProposal(semantic_intent=SET_INTENT),
        )


def test_a_product_detail_needs_a_reference() -> None:
    with pytest.raises(ValidationError, match="needs a reference"):
        CustomerAgentDecision(action=AgentAction.PRODUCT_DETAIL)


def test_a_comparison_needs_at_least_two_references() -> None:
    with pytest.raises(ValidationError, match="at least 2"):
        CustomerAgentDecision(
            action=AgentAction.COMPARE,
            comparison_references=(PresentedOrdinal(position=1),),
        )


def test_a_comparison_must_not_repeat_a_reference() -> None:
    with pytest.raises(ValidationError, match="repeat a reference"):
        CustomerAgentDecision(
            action=AgentAction.COMPARE,
            comparison_references=(
                PresentedOrdinal(position=1),
                PresentedOrdinal(position=1),
            ),
        )


def test_comparison_references_belong_only_to_a_comparison() -> None:
    with pytest.raises(ValidationError, match="only a comparison"):
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            comparison_references=(
                PresentedOrdinal(position=1),
                PresentedOrdinal(position=2),
            ),
        )


# ── blocking clarification ──────────────────────────────────────────────────


def test_a_clarification_action_needs_a_question() -> None:
    with pytest.raises(ValidationError, match="needs a question"):
        CustomerAgentDecision(
            action=AgentAction.CLARIFY, follow_up_policy=FollowUpPolicy.NONE
        )


def test_only_a_clarification_action_may_carry_a_question() -> None:
    with pytest.raises(ValidationError, match="only a clarification"):
        CustomerAgentDecision(action=AgentAction.ANSWER, clarification=CLARIFICATION)


def test_a_clarification_turn_has_no_optional_follow_up() -> None:
    """Nothing executed, so there are no results to ask about."""
    with pytest.raises(ValidationError, match="no optional follow-up"):
        CustomerAgentDecision(
            action=AgentAction.CLARIFY,
            clarification=CLARIFICATION,
            follow_up_policy=FollowUpPolicy.OPTIONAL,
        )


def test_a_clarification_turn_changes_nothing() -> None:
    with pytest.raises(ValidationError, match="changes nothing"):
        CustomerAgentDecision(
            action=AgentAction.CLARIFY,
            clarification=CLARIFICATION,
            follow_up_policy=FollowUpPolicy.NONE,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=2)
            ),
        )


def test_blocking_clarification_is_separate_from_the_follow_up_directive() -> None:
    """One blocks execution and carries words; the other only permits a later
    question whose wording depends on what the search returned."""
    assert "question" in BlockingClarification.model_fields
    assert set(FollowUpPolicy) == {FollowUpPolicy.NONE, FollowUpPolicy.OPTIONAL}
    assert "question" not in [f.value for f in FollowUpPolicy]


# ── composite turns stay bounded ────────────────────────────────────────────


def test_a_refinement_may_carry_one_interaction() -> None:
    """"I like the second one, but show me cheaper options." """
    decision = CustomerAgentDecision(
        action=AgentAction.REFINE_SEARCH,
        refinement=CHEAPER,
        interaction=ProductInteractionIntent(
            op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=2)
        ),
    )

    assert decision.interaction is not None
    assert decision.interaction.op is ProductInteractionOp.SELECT


def test_focus_cannot_accompany_a_search_that_replaces_results() -> None:
    """Committing results clears focus, so the pair is contradictory."""
    with pytest.raises(ValidationError, match="focus cannot accompany"):
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            refinement=CHEAPER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.FOCUS, reference=PresentedOrdinal(position=2)
            ),
        )


def test_there_is_no_action_sequence_field() -> None:
    """A decision describes one turn, never a plan."""
    for name in CustomerAgentDecision.model_fields:
        assert name not in {"actions", "steps", "plan", "sequence", "tool_calls"}
    assert CustomerAgentDecision.model_fields["action"].annotation is AgentAction


def test_at_most_one_interaction_can_be_expressed() -> None:
    annotation = str(CustomerAgentDecision.model_fields["interaction"].annotation)

    assert "tuple" not in annotation and "list" not in annotation


# ── commercial reason is separate from the action ───────────────────────────


def test_a_commercial_reason_rides_alongside_an_ordinary_action() -> None:
    decision = CustomerAgentDecision(
        action=AgentAction.REFINE_SEARCH,
        refinement=CHEAPER,
        commercial_reason=CommercialReason.UPSELL,
    )

    assert decision.action is AgentAction.REFINE_SEARCH


def test_no_execution_action_exists_for_a_sales_concept() -> None:
    """Upsell and objection are reasons for a search, not separate machinery."""
    actions = {a.value for a in AgentAction}

    assert not actions & {"upsell", "cheaper", "premium", "objection", "cross_sell"}


# ── state proposals ─────────────────────────────────────────────────────────


def test_customer_state_carries_only_stated_facts() -> None:
    """Everything here is something the customer said about themselves or their
    room - including the measurements, which are theirs and never inferred."""
    assert set(CustomerStateProposal.model_fields) == {
        "customer_preferences",
        "room_type",
        "clear_room_type",
        "room_geometry",
        "clear_room_geometry",
        "room_budget",
        "clear_room_budget",
        "design_preferences",
    }


@pytest.mark.parametrize("forbidden", ["purchase_stage", "semantic_intent"])
def test_customer_state_rejects_inferred_or_search_fields(forbidden: str) -> None:
    assert forbidden not in CustomerStateProposal.model_fields
    with pytest.raises(ValidationError):
        CustomerStateProposal(**{forbidden: "anything"})


def test_derived_commerce_owns_the_purchase_stage() -> None:
    proposal = DerivedCommerceProposal(purchase_stage=PurchaseStage.CONSIDERING)

    assert proposal.purchase_stage is PurchaseStage.CONSIDERING
    assert set(DerivedCommerceProposal.model_fields) == {
        "purchase_stage",
        "clear_purchase_stage",
    }


def test_a_stage_cannot_be_set_and_cleared_at_once() -> None:
    with pytest.raises(ValidationError, match="set and cleared"):
        DerivedCommerceProposal(
            purchase_stage=PurchaseStage.EXPLORING, clear_purchase_stage=True
        )


def test_a_room_fact_cannot_be_set_and_cleared_at_once() -> None:
    with pytest.raises(ValidationError, match="set and cleared"):
        CustomerStateProposal(room_type="living room", clear_room_type=True)


def test_a_reusable_preference_is_expressible() -> None:
    """"I usually prefer Modern" - about the customer, not about this search."""
    proposal = CustomerStateProposal(
        customer_preferences=PreferenceProposal(
            op=PreferenceProposalOp.ADD,
            preferences=(
                SemanticPreference(
                    family=AttributeFamily.STYLE,
                    raw_value="Modern",
                    canonical_value="Modern",
                    strength=ConstraintStrength.PREFERRED,
                ),
            ),
        )
    )

    assert proposal.customer_preferences is not None


def test_a_task_preference_has_no_home_on_customer_state() -> None:
    """"Show me modern sofas" is a search preference. The contract offers no
    way to file it as a reusable customer default by accident."""
    assert "search_preferences" not in CustomerStateProposal.model_fields
    assert "attribute_preferences" not in CustomerStateProposal.model_fields


def test_a_budget_proposal_needs_a_bound() -> None:
    with pytest.raises(ValidationError, match="at least one bound"):
        PriceProposal(currency="SAR")


def test_a_budget_proposal_keeps_money_out_of_a_float() -> None:
    assert str(PriceProposal.model_fields["max_amount"].annotation) == "str | None"


# ── semantic intent has exactly one owner ───────────────────────────────────


def test_semantic_intent_lives_only_on_search_paths() -> None:
    assert "semantic_intent" in NewSearchProposal.model_fields
    assert "semantic_intent" in SearchRefinementDelta.model_fields
    assert "semantic_intent" not in CustomerStateProposal.model_fields
    assert "semantic_intent" not in DerivedCommerceProposal.model_fields
    assert "semantic_intent" not in CustomerAgentDecision.model_fields


def test_a_new_search_may_author_durable_fuzzy_wording() -> None:
    proposal = NewSearchProposal(semantic_intent=SET_INTENT)

    assert proposal.semantic_intent is not None
    assert proposal.semantic_intent.value == "cosy"


# ── selectors ───────────────────────────────────────────────────────────────


def test_an_ordinal_is_one_based_and_positive() -> None:
    assert PresentedOrdinal(position=1).position == 1
    with pytest.raises(ValidationError):
        PresentedOrdinal(position=0)


@pytest.mark.parametrize(
    "selector",
    [
        PresentedOrdinal(position=2),
        FocusedProduct(),
        SoleSelectedProduct(),
        PresentedAttributeMatch(family=AttributeFamily.COLOR, value="Beige"),
        PresentedExtremum(direction=ExtremumDirection.LOWEST),
    ],
)
def test_no_selector_can_carry_a_resolved_product_id(selector: BaseModel) -> None:
    shape = type(selector)

    assert "product_id" not in shape.model_fields
    with pytest.raises(ValidationError):
        shape.model_validate({**selector.model_dump(), "product_id": 165645})


def test_a_selector_is_chosen_by_kind() -> None:
    """A closed set: an unknown kind cannot be smuggled through."""
    with pytest.raises(ValidationError):
        ProductInteractionIntent(
            op=ProductInteractionOp.SELECT, reference={"kind": "by_product_id"}
        )


@pytest.mark.parametrize(
    "op", [ProductInteractionOp.SELECT, ProductInteractionOp.DESELECT]
)
def test_interactions_address_products_by_selector(
    op: ProductInteractionOp,
) -> None:
    intent = ProductInteractionIntent(op=op, reference=PresentedOrdinal(position=3))

    assert isinstance(intent.reference, PresentedOrdinal)


def test_detail_is_an_action_not_an_interaction() -> None:
    """It needs fresh hydration and drives the reply, so it is a primary
    action with a reference (locked M11A), not a silent state edit."""
    assert {o.value for o in ProductInteractionOp} == {"select", "deselect", "focus"}
    assert AgentAction.PRODUCT_DETAIL in AgentAction
