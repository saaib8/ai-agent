"""The turn contracts M11B-5 will produce, and the lines they draw.

Three distinctions carry this closure, and each is enforced by a contract
rather than by a convention the coordinator has to remember:

* a *question the model asked* is not a *question a service discovered*
* a *question* is not a *fact* ("that product is gone" answers nothing)
* a *handled outcome* is not an *internal defect*

No coordinator exists yet. These are contract tests.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CustomerAgentDecision,
)
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import CustomerTurnResult, TurnGrounding
from app.schemas.composition import CompositionDefect
from app.schemas.grounding import TurnFailure, TurnFailureCode
from app.schemas.resolution import (
    DeterministicClarification,
    ReferenceFailureReason,
    RelativePriceFailureReason,
)
from pydantic import ValidationError

APP = Path(__file__).parents[2] / "app"


def _decision() -> CustomerAgentDecision:
    return CustomerAgentDecision(action=AgentAction.ANSWER)


# ── CustomerTurnResult ──────────────────────────────────────────────────────


def test_a_turn_result_always_carries_the_next_state() -> None:
    """The reason it exists: an unreturned immutable state is a lost one."""
    result = CustomerTurnResult(
        state=AgentStateV1(), decision=_decision(), grounding=TurnGrounding()
    )

    assert isinstance(result.state, AgentStateV1)


@pytest.mark.parametrize("missing", ["state", "decision", "grounding"])
def test_every_turn_result_field_is_required(missing: str) -> None:
    fields = {
        "state": AgentStateV1(),
        "decision": _decision(),
        "grounding": TurnGrounding(),
    }
    del fields[missing]

    with pytest.raises(ValidationError):
        CustomerTurnResult(**fields)


def test_a_turn_result_is_frozen_and_closed() -> None:
    result = CustomerTurnResult(
        state=AgentStateV1(), decision=_decision(), grounding=TurnGrounding()
    )

    assert CustomerTurnResult.model_config["frozen"] is True
    with pytest.raises(ValidationError):
        setattr(result, "state", AgentStateV1())  # noqa: B010 - that is the point
    with pytest.raises(ValidationError):
        CustomerTurnResult.model_validate(
            {
                "state": AgentStateV1(),
                "decision": _decision(),
                "grounding": TurnGrounding(),
                "session_id": "abc",
            }
        )


@pytest.mark.parametrize(
    "forbidden",
    ["message", "response", "text", "prose", "session_id", "redis", "history"],
)
def test_a_turn_result_carries_no_prose_or_session(forbidden: str) -> None:
    """Wording is the response layer's; session lifecycle is later."""
    assert forbidden not in CustomerTurnResult.model_fields


def test_a_turn_result_holds_exactly_these_things() -> None:
    """`bundle_outcome` lives here rather than on the grounding because it
    carries `ProductCandidate`, and a grounded product deliberately holds no
    product id. This object already holds the whole state and never goes near a
    model."""
    assert set(CustomerTurnResult.model_fields) == {
        "state",
        "decision",
        "grounding",
        "bundle_outcome",
        "bundle_change",
    }


# ── TurnGrounding additions ─────────────────────────────────────────────────


def test_design_handoff_is_off_by_default() -> None:
    assert TurnGrounding().design_handoff_requested is False


def test_a_design_handoff_turn_is_representable() -> None:
    """Without constructing an InteriorDesignRequest, which needs catalog
    capabilities that do not exist yet."""
    grounding = TurnGrounding(design_handoff_requested=True)

    assert grounding.design_handoff_requested is True
    assert grounding.search is None
    assert grounding.product_detail is None


def test_the_two_clarifications_are_separate_fields() -> None:
    """A model's question and a service's discovery are different facts, so
    they have different fields - but a turn carries at most one of them.

    The separation is about *provenance*: one arrives with wording, the other
    with reason codes only. Which is asked is decided by priority before the
    grounding is built (see `test_turn_question_priority.py`).
    """
    model_question = TurnGrounding(
        clarification=BlockingClarification(
            reason=BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE,
            question="Which kind of table?",
        )
    )
    service_question = TurnGrounding(
        deterministic_clarification=DeterministicClarification(
            reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
            reference_reason=ReferenceFailureReason.SEVERAL_ATTRIBUTE_MATCHES,
        )
    )

    assert model_question.clarification is not None
    assert model_question.deterministic_clarification is None
    assert service_question.deterministic_clarification is not None
    assert service_question.clarification is None


def test_grounding_stays_structured() -> None:
    """No field a coordinator could fill with a sentence."""
    for name in TurnGrounding.model_fields:
        assert name not in ("message", "text", "prose", "answer", "reply")


# ── DeterministicClarification ──────────────────────────────────────────────


def test_a_deterministic_clarification_has_nowhere_to_put_wording() -> None:
    """Structural, not a convention: there is no question field to fill."""
    fields = set(DeterministicClarification.model_fields)

    assert "question" not in fields
    assert fields == {
        "reason",
        "reference_reason",
        "bundle_reason",
        "need_reason",
        "relative_price_reason",
    }


def test_it_is_frozen_and_closed() -> None:
    clarification = DeterministicClarification(
        reason=BlockingClarificationReason.MISSING_REFINEMENT_CURRENCY
    )

    assert DeterministicClarification.model_config["frozen"] is True
    with pytest.raises(ValidationError):
        setattr(  # noqa: B010 - the assignment IS what is being refused
            clarification, "reason", BlockingClarificationReason.COMPARISON_TARGETS
        )
    with pytest.raises(ValidationError):
        DeterministicClarification.model_validate(
            {
                "reason": BlockingClarificationReason.COMPARISON_TARGETS,
                "question": "which two?",
            }
        )


@pytest.mark.parametrize(
    ("reason", "detail", "case"),
    [
        (
            BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
            ReferenceFailureReason.SEVERAL_ATTRIBUTE_MATCHES,
            "'the beige one' matched two products",
        ),
        (
            BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
            ReferenceFailureReason.SEVERAL_SELECTED_PRODUCTS,
            "'the one I liked' with several selected",
        ),
        (
            BlockingClarificationReason.AMBIGUOUS_COMPARATIVE_REFERENCE,
            ReferenceFailureReason.TIED_EXTREMUM,
            "two products share the lowest price",
        ),
        (
            BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
            ReferenceFailureReason.ORDINAL_OUT_OF_RANGE,
            "'the fifth' of three",
        ),
    ],
)
def test_an_ambiguous_reference_keeps_its_specific_reason(
    reason: BlockingClarificationReason,
    detail: ReferenceFailureReason,
    case: str,
) -> None:
    clarification = DeterministicClarification(reason=reason, reference_reason=detail)

    assert clarification.reference_reason is detail, case


@pytest.mark.parametrize(
    ("reason", "case"),
    [
        (BlockingClarificationReason.MISSING_REFINEMENT_CURRENCY, "'under 3000' of what"),
        (BlockingClarificationReason.MISSING_DIMENSION_UNIT, "'no wider than 220'"),
        (BlockingClarificationReason.MISSING_DIMENSION_ROLE, "'a sofa under 200'"),
        (BlockingClarificationReason.MISSING_PRICE_CURRENCY, "a room budget with no currency"),
        (BlockingClarificationReason.COMPARISON_TARGETS, "more products than allowed"),
    ],
)
def test_each_composition_clarification_is_representable(
    reason: BlockingClarificationReason, case: str
) -> None:
    assert DeterministicClarification(reason=reason).reason is reason, case


def test_a_currency_conflict_is_customer_resolvable() -> None:
    """They can name another reference or another amount; we will not convert."""
    clarification = DeterministicClarification(
        reason=BlockingClarificationReason.MISSING_REFINEMENT_CURRENCY,
        relative_price_reason=RelativePriceFailureReason.CURRENCY_CONFLICT,
    )

    assert clarification.relative_price_reason is (
        RelativePriceFailureReason.CURRENCY_CONFLICT
    )


@pytest.mark.parametrize(
    ("relative_reason", "why"),
    [
        (
            RelativePriceFailureReason.MALFORMED_PERCENT,
            "a validation defect, not a question",
        ),
        (
            RelativePriceFailureReason.REFERENCE_UNRESOLVED,
            "already carried by reference_reason",
        ),
    ],
)
def test_non_customer_resolvable_relative_reasons_are_refused(
    relative_reason: RelativePriceFailureReason, why: str
) -> None:
    with pytest.raises(ValidationError):
        DeterministicClarification(
            reason=BlockingClarificationReason.MISSING_REFINEMENT_CURRENCY,
            relative_price_reason=relative_reason,
        )


@pytest.mark.parametrize(
    "unavailable",
    [
        ReferenceFailureReason.PRODUCT_UNAVAILABLE,
        ReferenceFailureReason.PRESENTED_SET_INCOMPLETE,
    ],
)
def test_an_unavailable_product_is_a_fact_not_a_question(
    unavailable: ReferenceFailureReason,
) -> None:
    """No answer the customer gives brings a deleted product back, so these
    belong to TurnFailure and the contract refuses them here."""
    with pytest.raises(ValidationError):
        DeterministicClarification(
            reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
            reference_reason=unavailable,
        )

    assert TurnFailure(code=TurnFailureCode.PRODUCT_UNAVAILABLE).code is (
        TurnFailureCode.PRODUCT_UNAVAILABLE
    )


def test_a_composition_defect_cannot_become_a_customer_question() -> None:
    """A relative price that reached the composer still relative is a wiring
    bug. Asking the customer to fix it would hide the defect behind a polite
    sentence, so there is no field for one."""
    source = (APP / "schemas/resolution.py").read_text()
    tree = ast.parse(source)
    model = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "DeterministicClarification"
    )
    annotations = {
        node.target.id
        for node in model.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }

    assert not any("defect" in name for name in annotations)
    assert "CompositionDefect" not in str(DeterministicClarification.model_fields)
    # The defect vocabulary still exists; it simply routes elsewhere.
    assert CompositionDefect.RELATIVE_PRICE_NOT_RESOLVED


def test_no_free_form_reason_string_is_possible() -> None:
    with pytest.raises(ValidationError):
        DeterministicClarification.model_validate({"reason": "something went wrong"})


def test_the_reason_families_are_the_approved_ones() -> None:
    """Every carried vocabulary is a typed enum; none is a free-form string.

    `reason` spans two families on purpose: the model-facing one, reused where
    a deterministic finding means what the model could have meant, and the
    application-only one holding what only query understanding can establish.
    """
    annotations = {
        name: str(field.annotation)
        for name, field in DeterministicClarification.model_fields.items()
    }

    assert "BlockingClarificationReason" in annotations["reason"]
    assert "SearchRequirementClarificationReason" in annotations["reason"]
    assert "str" not in annotations["reason"].replace("SearchRequirement", "")
    assert "ReferenceFailureReason" in annotations["reference_reason"]
    assert "RelativePriceFailureReason" in annotations["relative_price_reason"]


# ── the budget-without-currency case, at contract level ─────────────────────


def test_a_room_budget_needs_a_currency_the_proposal_may_not_have() -> None:
    """Why the mapper needs a clarification branch: the proposal's currency is
    optional and the persisted constraint's is not."""
    from app.schemas.agent_decision import PriceProposal
    from app.schemas.discovery import PriceConstraint

    proposal = PriceProposal(max_amount="12000")

    assert proposal.currency is None
    with pytest.raises(ValidationError):
        PriceConstraint.model_validate({"max_amount": Decimal("12000")})
    # The reason that case is reported with, rather than a guessed currency.
    assert DeterministicClarification(
        reason=BlockingClarificationReason.MISSING_PRICE_CURRENCY
    )


# ── the two reason families have two different authorities ──────────────────
#
# `BlockingClarificationReason` is model-facing: every member reaches the
# provider's schema and can be emitted by a decision. So a reason belongs there
# only if a model can legitimately know it from language and safe state.
#
# Whether this catalog can execute a requirement is not such a thing. Query
# understanding establishes it, from the taxonomy and the dimension registry,
# and only after it has run. Sharing one enum let a decision claim an
# unsupported requirement before any search existed - which is the hole this
# closes.

M7_ONLY_VALUES = (
    "unsupported_requirement",
    "unsupported_dimension_requirement",
    "unresolved_strict_requirement",
)


def test_the_model_facing_enum_holds_only_its_original_vocabulary() -> None:
    from app.schemas.agent_decision import BlockingClarificationReason

    assert {r.value for r in BlockingClarificationReason} == {
        "insufficient_product_type",
        "multiple_product_types",
        "missing_price_currency",
        "missing_refinement_currency",
        "missing_dimension_role",
        "missing_dimension_unit",
        "ambiguous_product_reference",
        "ambiguous_comparative_reference",
        "undefined_quality_criterion",
        "comparison_targets",
        # M12E-4D. Model-facing because a model can know it from language
        # alone: "keep this sofa but get rid of all the seating" contradicts
        # itself in the customer's own words. The application raises the same
        # reason when resolving revision constraints finds the contradiction.
        "contradictory_room_instructions",
    }


def test_the_application_only_enum_holds_exactly_the_three() -> None:
    from app.schemas.resolution import SearchRequirementClarificationReason

    assert {r.value for r in SearchRequirementClarificationReason} == set(M7_ONLY_VALUES)


def test_the_two_families_never_overlap() -> None:
    """Disjoint values, so a reason always names its own origin and the union
    can never resolve ambiguously."""
    from app.schemas.agent_decision import BlockingClarificationReason
    from app.schemas.resolution import SearchRequirementClarificationReason

    model_facing = {r.value for r in BlockingClarificationReason}
    application_only = {r.value for r in SearchRequirementClarificationReason}

    assert model_facing & application_only == set()


@pytest.mark.parametrize("value", M7_ONLY_VALUES)
def test_a_decision_cannot_claim_an_m7_outcome(value: str) -> None:
    """The key closure property: the model cannot say a requirement is
    unsupported before the search that would establish it has run."""
    from app.schemas.agent_decision import AgentAction, CustomerAgentDecision

    with pytest.raises(ValidationError):
        CustomerAgentDecision.model_validate(
            {
                "action": AgentAction.CLARIFY.value,
                "clarification": {"reason": value, "question": "Which material?"},
                "follow_up_policy": "none",
            }
        )


@pytest.mark.parametrize("value", M7_ONLY_VALUES)
def test_the_provider_schema_does_not_offer_the_three(value: str) -> None:
    """Checked on the emitted schema, plain and strict: what the provider is
    told the model may return."""
    import json

    from app.schemas.agent_decision import CustomerAgentDecision
    from openai.lib._pydantic import to_strict_json_schema

    assert value not in json.dumps(CustomerAgentDecision.model_json_schema())
    assert value not in json.dumps(to_strict_json_schema(CustomerAgentDecision))


def test_the_provider_structure_stays_intact_as_the_schema_grows() -> None:
    """The union the provider once rejected must still render as `anyOf`.

    Asserted on the selector itself rather than on a global `anyOf` tally: the
    contract legitimately gains optional fields over time, and a count would
    fail on every one of them while proving nothing about the union.
    """
    import json

    from app.schemas.agent_decision import CustomerAgentDecision
    from openai.lib._pydantic import to_strict_json_schema

    members = {
        "PresentedOrdinal",
        "FocusedProduct",
        "SoleSelectedProduct",
        "PresentedAttributeMatch",
        "PresentedExtremum",
    }
    for schema in (
        CustomerAgentDecision.model_json_schema(),
        to_strict_json_schema(CustomerAgentDecision),
    ):
        rendered = json.dumps(schema)
        assert rendered.count('"oneOf"') == 0
        assert rendered.count('"discriminator"') == 0
        selector = schema["$defs"]["ProductInteractionIntent"]["properties"]["reference"]
        assert {
            branch["$ref"].rsplit("/", 1)[-1] for branch in selector["anyOf"]
        } == members


def test_a_model_clarification_still_uses_its_own_vocabulary() -> None:
    """The narrowing must not have disturbed the reasons a model may give."""
    from app.schemas.agent_decision import (
        AgentAction,
        BlockingClarificationReason,
        CustomerAgentDecision,
    )

    for reason in BlockingClarificationReason:
        decision = CustomerAgentDecision.model_validate(
            {
                "action": AgentAction.CLARIFY.value,
                "clarification": {"reason": reason.value, "question": "Which one?"},
                "follow_up_policy": "none",
            }
        )
        assert decision.clarification is not None
        assert decision.clarification.reason is reason


@pytest.mark.parametrize("value", M7_ONLY_VALUES)
def test_a_deterministic_clarification_carries_each_application_only_reason(
    value: str,
) -> None:
    from app.schemas.resolution import SearchRequirementClarificationReason

    reason = SearchRequirementClarificationReason(value)
    clarification = DeterministicClarification(reason=reason)

    assert clarification.reason is reason
    # Serialisation keeps the string, so the response layer can tell them apart.
    assert clarification.model_dump(mode="json")["reason"] == value


def test_both_families_still_validate_on_the_same_field() -> None:
    from app.schemas.agent_decision import BlockingClarificationReason
    from app.schemas.resolution import SearchRequirementClarificationReason

    from_model_vocabulary = DeterministicClarification(
        reason=BlockingClarificationReason.MISSING_REFINEMENT_CURRENCY
    )
    from_search_vocabulary = DeterministicClarification(
        reason=SearchRequirementClarificationReason.UNSUPPORTED_REQUIREMENT
    )

    assert isinstance(from_model_vocabulary.reason, BlockingClarificationReason)
    assert isinstance(from_search_vocabulary.reason, SearchRequirementClarificationReason)
