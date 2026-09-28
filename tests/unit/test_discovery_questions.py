"""A stated need gets a question or two, one per turn, and then products.

A sharp salesperson asks a little before bringing things out, and knows when to
stop. The decision model judges what is worth asking; the application holds the
ceiling, so however the conversation drifts a customer is never asked a third
question before seeing anything.
"""

from __future__ import annotations

from typing import Any

from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CustomerAgentDecision,
    FollowUpGoal,
    FollowUpPolicy,
)
from app.schemas.agent_state import AgentStateV1, DerivedCommerceState
from app.services.agent_view import project_state

from tests.unit.test_turn_coordinator import _coordinator, _resolved, _state, _turn

ASK_SEATS = CustomerAgentDecision(
    action=AgentAction.CLARIFY,
    clarification=BlockingClarification(
        reason=BlockingClarificationReason.DETAIL_BEFORE_SEARCH,
        question="How many people will sit on it?",
    ),
    follow_up_policy=FollowUpPolicy.NONE,
)
ASK_TYPE = CustomerAgentDecision(
    action=AgentAction.CLARIFY,
    clarification=BlockingClarification(
        reason=BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE,
        question="Which kind of table?",
    ),
    follow_up_policy=FollowUpPolicy.NONE,
)
SEARCH = CustomerAgentDecision(action=AgentAction.SEARCH)
ANSWER = CustomerAgentDecision(action=AgentAction.ANSWER, follow_up_policy=FollowUpPolicy.NONE)


class ScriptedDecisions:
    """One decision per call, in order - a first answer and its correction."""

    def __init__(self, *decisions: CustomerAgentDecision) -> None:
        self._decisions = list(decisions)
        self.inputs: list[Any] = []
        self.problems: list[tuple[str, ...]] = []

    async def decide(self, decision_input: Any, *, problems: Any = ()) -> CustomerAgentDecision:
        self.inputs.append(decision_input)
        self.problems.append(tuple(problems))
        return self._decisions[len(self.inputs) - 1]


def _asked(count: int) -> AgentStateV1:
    state = _state(request=None, presented=(), selected=())
    return state.model_copy(
        update={"derived_commerce": DerivedCommerceState(discovery_questions_asked=count)}
    )


async def _run(state: AgentStateV1, *decisions: CustomerAgentDecision) -> tuple[Any, Any]:
    scripted = ScriptedDecisions(*decisions)
    coordinator, _ = _coordinator(decisions[0], decisions=scripted, interpretation=_resolved())
    result = await coordinator.run(_turn(state, "I need a sofa under 5000"))
    return result, scripted


# ── what the decision model is shown ────────────────────────────────────────


def test_the_state_view_says_how_many_questions_are_left() -> None:
    assert project_state(_asked(0), discovery_question_limit=2).discovery_questions_left == 2
    assert project_state(_asked(1), discovery_question_limit=2).discovery_questions_left == 1
    assert project_state(_asked(2), discovery_question_limit=2).discovery_questions_left == 0
    assert project_state(_asked(5), discovery_question_limit=2).discovery_questions_left == 0


# ── counting ────────────────────────────────────────────────────────────────


async def test_a_question_about_a_stated_need_is_counted() -> None:
    result, _ = await _run(_asked(0), ASK_SEATS)

    assert result.state.derived_commerce.discovery_questions_asked == 1
    assert result.grounding.clarification is not None


async def test_the_second_question_is_still_allowed() -> None:
    result, scripted = await _run(_asked(1), ASK_SEATS)

    assert result.state.derived_commerce.discovery_questions_asked == 2
    assert len(scripted.inputs) == 1, "within the limit, no correction"


async def test_showing_products_starts_the_count_again() -> None:
    result, _ = await _run(_asked(2), SEARCH)

    assert result.grounding.search is not None
    assert result.state.derived_commerce.discovery_questions_asked == 0


async def test_an_answer_or_another_kind_of_question_leaves_the_count_alone() -> None:
    answered, _ = await _run(_asked(1), ANSWER)
    clarified, _ = await _run(_asked(1), ASK_TYPE)

    assert answered.state.derived_commerce.discovery_questions_asked == 1
    assert clarified.state.derived_commerce.discovery_questions_asked == 1


# ── the ceiling ─────────────────────────────────────────────────────────────


async def test_a_third_question_is_refused_and_the_turn_shows_products() -> None:
    """At the limit, the question is sent back once with the rule it broke,
    and the corrected decision searches."""
    result, scripted = await _run(_asked(2), ASK_SEATS, SEARCH)

    assert len(scripted.inputs) == 2
    assert scripted.problems[1], "the correction says what was wrong"
    assert "discovery_questions_left is 0" in scripted.problems[1][0]
    assert result.grounding.clarification is None
    assert result.grounding.search is not None
    assert result.state.derived_commerce.discovery_questions_asked == 0


async def test_a_limit_of_zero_means_show_first() -> None:
    scripted = ScriptedDecisions(ASK_SEATS, SEARCH)
    coordinator, _ = _coordinator(
        ASK_SEATS, decisions=scripted, interpretation=_resolved(), discovery_question_limit=0
    )

    result = await coordinator.run(_turn(_asked(0), "I need a sofa"))

    assert result.grounding.search is not None
    assert len(scripted.inputs) == 2


# ── seats before the look, for a sofa ───────────────────────────────────────

def _discovery(**fields: Any) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.CLARIFY,
        clarification=BlockingClarification(
            reason=BlockingClarificationReason.DETAIL_BEFORE_SEARCH,
            question="What look are you after?",
            **fields,
        ),
        follow_up_policy=FollowUpPolicy.NONE,
    )


ASK_SOFA_STYLE = _discovery(subject=FollowUpGoal.STYLE, multi_seat_seating=True)
ASK_SOFA_SEATS = _discovery(
    subject=FollowUpGoal.SEATING_REQUIREMENT, multi_seat_seating=True
)


async def test_a_sofa_asked_about_its_look_before_its_seats_is_corrected() -> None:
    result, scripted = await _run(_asked(0), ASK_SOFA_STYLE, ASK_SOFA_SEATS)

    assert len(scripted.inputs) == 2
    assert "how many will usually sit" in scripted.problems[1][0]
    assert result.grounding.clarification is not None
    assert result.grounding.clarification.subject is FollowUpGoal.SEATING_REQUIREMENT


async def test_the_look_may_come_first_once_the_seats_are_known() -> None:
    known = _discovery(subject=FollowUpGoal.STYLE, multi_seat_seating=True, seats_known=True)
    result, scripted = await _run(_asked(0), known)

    assert len(scripted.inputs) == 1
    assert result.grounding.clarification is not None


async def test_the_look_may_come_first_for_anything_that_is_not_a_sofa() -> None:
    result, scripted = await _run(_asked(0), _discovery(subject=FollowUpGoal.STYLE))

    assert len(scripted.inputs) == 1
    assert result.grounding.clarification is not None
