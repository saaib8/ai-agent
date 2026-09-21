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
    CommercialReason,
    CustomerAgentDecision,
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
from app.schemas.refinement import SemanticIntentOp, SemanticIntentRefinement
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
    }
    assert "similar" not in {a.value for a in AgentAction}


def test_a_reference_still_cannot_accompany_the_other_actions() -> None:
    """The permission was documented, not widened."""
    for action in (AgentAction.ANSWER, AgentAction.CLARIFY, AgentAction.REFINE_SEARCH):
        with pytest.raises(ValidationError):
            CustomerAgentDecision(action=action, reference=FocusedProduct())


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
