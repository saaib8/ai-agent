"""Two orchestration invariants that only appear when locked rules compose.

Neither is visible in any single rule. Each falls out of combining several that
are individually correct, which is why they are pinned here with executable
counterexamples rather than described.

* **The focus contradiction.** Interactions resolve and apply against pre-turn
  state; `PRODUCT_DETAIL` resolves against pre-turn state too and implies focus;
  and focus must name a presented or selected product. A single decision can
  satisfy all three and still produce a state that cannot exist.
* **One question per turn.** A turn can discover several unresolved details.
  Asking about all of them is an interrogation, so the grounding carries one,
  chosen by priority rather than by which branch ran first.
"""

from __future__ import annotations

import pytest
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CustomerAgentDecision,
    FollowUpPolicy,
    NewSearchProposal,
    ProductInteractionIntent,
    ProductInteractionOp,
)
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    ProductInteractionState,
)
from app.schemas.agent_turn import TurnGrounding
from app.schemas.agent_updates import (
    AgentStateUpdate,
    ProductInteractionUpdate,
    RemoveItems,
)
from app.schemas.discovery import ProductSearchRequest
from app.schemas.grounding import TurnFailure, TurnFailureCode
from app.schemas.product_reference import (
    FocusedProduct,
    PresentedOrdinal,
    ProductReferenceSelector,
    SoleSelectedProduct,
)
from app.schemas.refinement import SemanticIntentOp, SemanticIntentRefinement
from app.schemas.resolution import DeterministicClarification, ReferenceFailureReason
from app.services.agent_state import apply_update
from pydantic import ValidationError

REQUEST = ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa")

DETAIL_PRODUCT = 42
"""Selected but never presented - which is what makes the counterexample bite."""


def _pre_turn(focus: int | None = None) -> AgentStateV1:
    """presented=(10, 11), selected=(42,) - a selection reached by another path."""
    return AgentStateV1(
        active_search=ActiveSearchState(request=REQUEST, revision=1),
        product_interaction=ProductInteractionState(
            presented_product_ids=(10, 11),
            presented_search_revision=1,
            selected_product_ids=(DETAIL_PRODUCT,),
            focused_product_id=focus,
        ),
    )


# ── the focus contradiction, demonstrated then forbidden ────────────────────


def test_the_state_contradiction_is_real() -> None:
    """The counterexample, executed against the real reducer.

    Both selectors resolve to 42 against the pre-turn state. Deselecting it
    removes the only thing making it known, and the detail's implied focus then
    names a product that is neither presented nor selected.
    """
    state = _pre_turn()

    deselected = apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                selected_product_ids=RemoveItems(items=(DETAIL_PRODUCT,))
            )
        ),
    )
    assert deselected.product_interaction.selected_product_ids == ()
    assert DETAIL_PRODUCT not in deselected.product_interaction.presented_product_ids

    with pytest.raises(ValidationError, match="presented or selected"):
        apply_update(
            deselected,
            AgentStateUpdate(
                product_interaction=ProductInteractionUpdate(
                    focused_product_id=DETAIL_PRODUCT
                )
            ),
        )


def test_the_decision_that_would_cause_it_is_now_rejected() -> None:
    """Forbidden at the decision, so the coordinator never faces the choice."""
    with pytest.raises(ValidationError, match="cannot also deselect"):
        CustomerAgentDecision(
            action=AgentAction.PRODUCT_DETAIL,
            reference=SoleSelectedProduct(),
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.DESELECT, reference=SoleSelectedProduct()
            ),
        )


@pytest.mark.parametrize(
    "reference",
    [SoleSelectedProduct(), PresentedOrdinal(position=1), FocusedProduct()],
    ids=lambda r: r.kind,
)
def test_no_deselect_accompanies_a_product_detail_whatever_it_points_at(
    reference: ProductReferenceSelector,
) -> None:
    """The rule is on the pair, not on whether the two selectors look alike:
    two different-looking selectors can resolve to the same product."""
    with pytest.raises(ValidationError, match="cannot also deselect"):
        CustomerAgentDecision(
            action=AgentAction.PRODUCT_DETAIL,
            reference=PresentedOrdinal(position=1),
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.DESELECT, reference=reference
            ),
        )


# ── the combinations that stay valid ────────────────────────────────────────


def test_select_still_accompanies_a_product_detail() -> None:
    decision = CustomerAgentDecision(
        action=AgentAction.PRODUCT_DETAIL,
        reference=PresentedOrdinal(position=1),
        interaction=ProductInteractionIntent(
            op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=2)
        ),
    )

    assert decision.interaction is not None
    assert decision.interaction.op is ProductInteractionOp.SELECT


def test_a_select_beside_a_detail_produces_a_valid_state() -> None:
    """Selecting only ever adds, so nothing the focus needs can disappear."""
    state = apply_update(
        _pre_turn(),
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                selected_product_ids=RemoveItems(items=())
            )
        ),
    )
    final = apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                focused_product_id=DETAIL_PRODUCT
            )
        ),
    )

    assert final.product_interaction.focused_product_id == DETAIL_PRODUCT


def test_focus_still_accompanies_a_product_detail() -> None:
    decision = CustomerAgentDecision(
        action=AgentAction.PRODUCT_DETAIL,
        reference=PresentedOrdinal(position=1),
        interaction=ProductInteractionIntent(
            op=ProductInteractionOp.FOCUS, reference=PresentedOrdinal(position=2)
        ),
    )

    assert decision.interaction is not None
    assert decision.interaction.op is ProductInteractionOp.FOCUS


def test_the_detail_product_is_the_final_focus() -> None:
    """An optional FOCUS applies first; the detail's implied focus is later and
    therefore wins. Pinned so the ordering is not left to be rediscovered."""
    after_interaction = apply_update(
        _pre_turn(),
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(focused_product_id=10)
        ),
    )
    final = apply_update(
        after_interaction,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                focused_product_id=DETAIL_PRODUCT
            )
        ),
    )

    assert after_interaction.product_interaction.focused_product_id == 10
    assert final.product_interaction.focused_product_id == DETAIL_PRODUCT


def test_deselect_still_accompanies_other_actions() -> None:
    """Only the detail pair is forbidden; deselecting remains ordinary."""
    for action in (AgentAction.ANSWER, AgentAction.COMPARE, AgentAction.SEARCH):
        payload: dict[str, object] = {
            "action": action,
            "interaction": ProductInteractionIntent(
                op=ProductInteractionOp.DESELECT, reference=SoleSelectedProduct()
            ),
        }
        if action is AgentAction.COMPARE:
            payload["comparison_references"] = (
                PresentedOrdinal(position=1),
                PresentedOrdinal(position=2),
            )
        assert CustomerAgentDecision(**payload).action is action


# ── similar search takes no proposal ────────────────────────────────────────

INTENT = NewSearchProposal(
    semantic_intent=SemanticIntentRefinement(op=SemanticIntentOp.SET, value="cosy")
)


def test_a_plain_search_is_valid() -> None:
    assert CustomerAgentDecision(action=AgentAction.SEARCH).reference is None


def test_a_search_with_a_proposal_is_valid() -> None:
    decision = CustomerAgentDecision(action=AgentAction.SEARCH, new_search=INTENT)

    assert decision.new_search is not None
    assert decision.reference is None


def test_a_search_for_alternatives_is_valid() -> None:
    decision = CustomerAgentDecision(
        action=AgentAction.SEARCH, reference=PresentedOrdinal(position=2)
    )

    assert decision.reference is not None
    assert decision.new_search is None


def test_a_search_for_alternatives_with_a_proposal_is_rejected() -> None:
    """"Similar to the second one, but cosy" - V1 has no planner that could
    combine a structural seed with a separate durable wording, so the shape is
    refused rather than one half being silently dropped."""
    with pytest.raises(ValidationError, match="carries no new-search proposal"):
        CustomerAgentDecision(
            action=AgentAction.SEARCH,
            reference=PresentedOrdinal(position=2),
            new_search=INTENT,
        )


def test_no_new_action_and_no_motive_routing_were_introduced() -> None:
    from app.schemas.agent_decision import CommercialReason

    assert len(AgentAction) == 8
    for reason in CommercialReason:
        decision = CustomerAgentDecision(
            action=AgentAction.SEARCH, commercial_reason=reason
        )
        assert decision.reference is None


# ── one question per turn ───────────────────────────────────────────────────

MODEL_QUESTION = BlockingClarification(
    reason=BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE,
    question="Which kind of table?",
)
SERVICE_QUESTION = DeterministicClarification(
    reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
    reference_reason=ReferenceFailureReason.SEVERAL_ATTRIBUTE_MATCHES,
)
BUDGET_QUESTION = DeterministicClarification(
    reason=BlockingClarificationReason.MISSING_PRICE_CURRENCY
)


def test_a_model_clarification_stands_alone() -> None:
    grounding = TurnGrounding(clarification=MODEL_QUESTION)

    assert grounding.deterministic_clarification is None
    assert grounding.follow_up_policy is FollowUpPolicy.NONE


def test_a_deterministic_clarification_stands_alone() -> None:
    grounding = TurnGrounding(deterministic_clarification=SERVICE_QUESTION)

    assert grounding.clarification is None
    assert grounding.follow_up_policy is FollowUpPolicy.NONE


def test_the_two_kinds_cannot_coexist() -> None:
    """The invariant that makes the priority policy enforceable rather than
    advisory: there is no grounding that carries both."""
    with pytest.raises(ValidationError, match="at most one question"):
        TurnGrounding(
            clarification=MODEL_QUESTION,
            deterministic_clarification=SERVICE_QUESTION,
        )


def test_two_deterministic_findings_cannot_both_be_asked() -> None:
    """An ambiguous selection and a currency-less budget are both real. The
    field holds one, so the coordinator picks by priority."""
    assert BUDGET_QUESTION != SERVICE_QUESTION
    grounding = TurnGrounding(deterministic_clarification=BUDGET_QUESTION)

    assert grounding.deterministic_clarification is BUDGET_QUESTION


@pytest.mark.parametrize(
    "kwargs",
    [
        {"clarification": MODEL_QUESTION},
        {"deterministic_clarification": SERVICE_QUESTION},
    ],
    ids=["model", "deterministic"],
)
def test_a_question_turn_offers_no_follow_up(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="no follow-up"):
        TurnGrounding(follow_up_policy=FollowUpPolicy.OPTIONAL, **kwargs)


@pytest.mark.parametrize("code", list(TurnFailureCode))
def test_a_handled_failure_offers_no_follow_up(code: TurnFailureCode) -> None:
    """The operation did not complete, so there are no results to ask about."""
    with pytest.raises(ValidationError, match="failed turn offers no follow-up"):
        TurnGrounding(
            failure=TurnFailure(code=code), follow_up_policy=FollowUpPolicy.OPTIONAL
        )

    assert TurnGrounding(failure=TurnFailure(code=code)).follow_up_policy is (
        FollowUpPolicy.NONE
    )


def test_a_failure_is_not_turned_into_a_question() -> None:
    """Three different outcomes stay three: a failure carries no clarification
    of either kind, and nothing here manufactures one."""
    grounding = TurnGrounding(
        failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE)
    )

    assert grounding.clarification is None
    assert grounding.deterministic_clarification is None


def test_an_ordinary_turn_may_still_offer_a_follow_up() -> None:
    grounding = TurnGrounding(follow_up_policy=FollowUpPolicy.OPTIONAL)

    assert grounding.follow_up_policy is FollowUpPolicy.OPTIONAL
    assert grounding.clarification is None
    assert grounding.deterministic_clarification is None
