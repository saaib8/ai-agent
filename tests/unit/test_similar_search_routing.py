"""How a decision asks for alternatives, and how it must not.

`SEARCH` carrying a reference is the whole trigger. It is a shape the contract
already permitted; what this closure adds is that it *means* something, and
that nothing else does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.prompts.customer_commerce.v1 import INSTRUCTIONS
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CommercialReason,
    CustomerAgentDecision,
    FollowUpPolicy,
    NewSearchProposal,
)
from app.schemas.product_reference import (
    ExtremumDirection,
    FocusedProduct,
    PresentedAttributeMatch,
    PresentedExtremum,
    PresentedOrdinal,
    ProductReferenceSelector,
    SoleSelectedProduct,
)
from app.schemas.refinement import (
    PriceRefinement,
    PriceRefinementOp,
    SearchRefinementDelta,
    SemanticIntentOp,
    SemanticIntentRefinement,
)
from app.taxonomy.attributes import AttributeFamily
from pydantic import ValidationError

FLAT_INSTRUCTIONS = " ".join(INSTRUCTIONS.split())
APP = Path(__file__).parents[2] / "app"


def _is_similar_search(decision: CustomerAgentDecision) -> bool:
    """The routing predicate itself, written once here so the tests below
    describe the rule rather than re-deriving it."""
    return decision.action is AgentAction.SEARCH and decision.reference is not None


# ── the trigger ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reference", "phrasing"),
    [
        (PresentedOrdinal(position=2), "something similar to the second one"),
        (
            PresentedAttributeMatch(family=AttributeFamily.COLOR, value="Beige"),
            "something like the beige one",
        ),
        (SoleSelectedProduct(), "something similar to the one I selected"),
        (FocusedProduct(), "anything else like this"),
        (
            PresentedExtremum(direction=ExtremumDirection.LOWEST),
            "something like the cheapest one",
        ),
    ],
)
def test_a_search_with_a_reference_is_an_alternatives_search(
    reference: ProductReferenceSelector, phrasing: str
) -> None:
    decision = CustomerAgentDecision(action=AgentAction.SEARCH, reference=reference)

    assert _is_similar_search(decision), phrasing


def test_a_plain_search_carries_no_reference() -> None:
    """"Show me sofas" - the ordinary new-task path, with no product to seed
    from."""
    decision = CustomerAgentDecision(action=AgentAction.SEARCH)

    assert decision.reference is None
    assert not _is_similar_search(decision)


def test_a_search_with_a_proposal_is_still_an_ordinary_search() -> None:
    decision = CustomerAgentDecision(
        action=AgentAction.SEARCH,
        new_search=NewSearchProposal(
            semantic_intent=SemanticIntentRefinement(
                op=SemanticIntentOp.SET, value="cosy"
            )
        ),
    )

    assert not _is_similar_search(decision)


# ── what must not route ─────────────────────────────────────────────────────


@pytest.mark.parametrize("reason", list(CommercialReason))
def test_commercial_reason_is_never_routing_authority(reason: CommercialReason) -> None:
    """A motive describes why the turn is worth doing. An upsell and a request
    for alternatives are both searches, so routing on the motive would make one
    decision execute two ways."""
    without = CustomerAgentDecision(action=AgentAction.SEARCH, commercial_reason=reason)
    with_reference = CustomerAgentDecision(
        action=AgentAction.SEARCH,
        commercial_reason=reason,
        reference=PresentedOrdinal(position=1),
    )

    assert not _is_similar_search(without)
    assert _is_similar_search(with_reference)


def test_the_alternative_motive_alone_routes_nothing() -> None:
    decision = CustomerAgentDecision(
        action=AgentAction.SEARCH, commercial_reason=CommercialReason.ALTERNATIVE
    )

    assert decision.commercial_reason is CommercialReason.ALTERNATIVE
    assert not _is_similar_search(decision)


def test_semantic_intent_does_not_route() -> None:
    """Durable fuzzy wording is executed by the search; it is not a switch."""
    decision = CustomerAgentDecision(
        action=AgentAction.SEARCH,
        new_search=NewSearchProposal(
            semantic_intent=SemanticIntentRefinement(
                op=SemanticIntentOp.SET, value="similar to that one"
            )
        ),
    )

    assert not _is_similar_search(decision)


def test_no_similar_action_was_added() -> None:
    """Alternatives still route by a search's reference, not by an action.

    The action set has grown since - M12E-4B added `bundle_refine`, which is a
    room edit rather than a search - but nothing was added for "something like
    this one", which is what this guard is about.
    """
    assert {a.value for a in AgentAction} == {
        "answer",
        "clarify",
        "search",
        "refine_search",
        "product_detail",
        "compare",
        "bundle_refine",
        "design_handoff",
        # M17: showing the customer their own choices. Deterministic - it
        # presents what the session records and searches for nothing.
        "show_selection",
    }
    assert "similar" not in {a.value for a in AgentAction}


def test_a_reference_is_still_refused_where_it_would_act_on_the_wrong_thing() -> None:
    """Refused where the action has a reference field of its own.

    A comparison and a bundle edit each carry their own, so a stray product
    reference on either means the model confused the two - and acting on the
    wrong one would edit the wrong piece of the customer's room.
    """
    for action, payload in (
        (AgentAction.REFINE_SEARCH, {"refinement": SearchRefinementDelta(
            price=PriceRefinement(op=PriceRefinementOp.CLEAR))}),
    ):
        with pytest.raises(ValidationError):
            CustomerAgentDecision(action=action, reference=FocusedProduct(), **payload)


def test_an_inert_reference_does_not_fail_the_turn() -> None:
    """The live defect this closes: "will the second sofa fit my living room?"
    with no room measurements on record is a clarification *about* card two, so
    the model attached card two - and the turn 502'd.

    The provider's strict schema puts the field on every decision, so refusing
    it turned an artefact of that into a failed answer while changing nothing:
    neither branch resolves a reference. Recorded for the trace, acted on
    nowhere.
    """
    for action in (AgentAction.ANSWER, AgentAction.CLARIFY):
        decision = CustomerAgentDecision(
            action=action,
            reference=FocusedProduct(),
            **(
                {
                    "clarification": BlockingClarification(
                        reason=BlockingClarificationReason.MISSING_ROOM_REQUIREMENTS,
                        question="How big is the room?",
                    ),
                    "follow_up_policy": FollowUpPolicy.NONE,
                }
                if action is AgentAction.CLARIFY
                else {}
            ),
        )
        assert decision.reference is not None


def test_a_similar_search_still_names_no_product() -> None:
    decision = CustomerAgentDecision(
        action=AgentAction.SEARCH, reference=PresentedOrdinal(position=2)
    )

    assert "product_id" not in decision.model_dump_json()


# ── documented, not implicit ────────────────────────────────────────────────


def test_the_contract_documents_what_a_search_reference_means() -> None:
    source = (APP / "schemas/agent_decision.py").read_text()
    flat = " ".join(source.split())

    assert "structurally similar alternatives" in flat
    assert "commercial_reason" in flat


def test_the_prompt_teaches_the_alternatives_shape() -> None:
    for anchor in (
        "something similar to the second one",
        "you attach the reference to it",
        "an ordinary new search carries none",
        "a motive is not a route",
    ):
        assert anchor in FLAT_INSTRUCTIONS, anchor


def test_the_prompt_does_not_promise_composite_alternatives() -> None:
    """V1 has no dependent composite planner, and the prompt says so rather
    than leaving the model to attempt one."""
    for anchor in (
        "takes no proposal alongside it",
        "is not one request here",
        "rather than inventing a shape that holds both",
    ):
        assert anchor in FLAT_INSTRUCTIONS, anchor


def test_the_prompt_tells_the_model_not_to_deselect_during_a_detail() -> None:
    """The state contradiction, stated as conversational policy so the model
    avoids the shape rather than having the decision rejected."""
    assert "Never deselect anything in the same turn" in FLAT_INSTRUCTIONS


def test_the_prompt_still_names_no_product_identity() -> None:
    assert "product_id" not in INSTRUCTIONS


def test_going_with_something_is_not_being_like_it() -> None:
    """The live defect: "show me coffee tables that would work with it" was
    routed as a search *for alternatives to the sofa*, so the customer asked
    for tables and got a fresh page of sofas.

    A reference means more of this kind of thing. It never means something that
    would suit it.
    """
    assert "LIKE THIS ONE IS NOT GOES WITH THIS ONE" in FLAT_INSTRUCTIONS
    assert "It never means \"something that would suit it\"" in FLAT_INSTRUCTIONS
    assert "When they name a different kind of thing, the reference would search" in (
        FLAT_INSTRUCTIONS
    )


def test_similar_after_a_design_answer_is_not_the_anchor() -> None:
    """The live defect: "which sofa goes with this carpet?" was answered about
    sofas, then "show me some similar option" returned more rugs - the
    reference was attached to the selected carpet, so a similar-search ran for
    the very kind of thing they already had (M23 2)."""
    assert "SIMILAR TO WHAT YOU WERE JUST TALKING ABOUT" in FLAT_INSTRUCTIONS
    assert "they want the sofas you described" in FLAT_INSTRUCTIONS
    assert "attach no reference" in FLAT_INSTRUCTIONS
    assert "not what is selected" in FLAT_INSTRUCTIONS
