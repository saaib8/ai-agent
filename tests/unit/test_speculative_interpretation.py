"""Reading the message for a search while the turn is decided (plan 11, 3b).

Query understanding reads one message and nothing else, so it can start at the
same time as the decision. Its answer is used only when the decision hands it
exactly that message; a restated request, a failure, or a turn that is not a
search falls back to today's path, and an unused reading never fails the turn.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.schemas.agent_decision import AgentAction, CommercialReason, CustomerAgentDecision
from app.schemas.agent_state import AgentStateV1
from app.schemas.query import ResolvedSearch

from tests.unit import test_product_brief as brief_tests


class CountingUnderstanding:
    """Query understanding that counts its calls, can fail once, and can be slow."""

    def __init__(
        self, outcome: ResolvedSearch, *, fail_first: Exception | None = None, delay: float = 0
    ) -> None:
        self.outcome = outcome
        self.fail_first = fail_first
        self.delay = delay
        self.messages: list[str] = []

    async def interpret(self, message: str, **_: Any) -> Any:
        self.messages.append(message)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_first is not None:
            failure, self.fail_first = self.fail_first, None
            raise failure
        return self.outcome


def _coordinator(decision: CustomerAgentDecision, understanding: CountingUnderstanding) -> Any:
    coordinator, pipeline = brief_tests._coordinator(decision)
    coordinator._query_understanding = understanding
    coordinator._speculative_interpretation = True
    return coordinator, pipeline


def _search(**fields: Any) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.SEARCH, commercial_reason=CommercialReason.CUSTOMER_REQUEST, **fields
    )


async def test_the_message_read_alongside_the_decision_is_used_once() -> None:
    understanding = CountingUnderstanding(brief_tests._need())
    coordinator, _ = _coordinator(_search(), understanding)

    result = await coordinator.run(brief_tests._typed(AgentStateV1(), "I need a sofa"))

    assert understanding.messages == ["I need a sofa"]
    assert result.product_brief is not None


async def test_the_same_words_differently_spaced_still_count() -> None:
    understanding = CountingUnderstanding(brief_tests._need())
    coordinator, _ = _coordinator(_search(search_request="I need  a sofa "), understanding)

    await coordinator.run(brief_tests._typed(AgentStateV1(), "I need a sofa"))

    assert understanding.messages == ["I need a sofa"]


async def test_a_restated_request_is_read_again_as_restated() -> None:
    """The decision hands on "grey sofas under 3000 SAR", not the typed
    "grey, under 3000": that is what is searched. The early reading is
    discarded - cancelled before it starts, when the decision is quick."""
    understanding = CountingUnderstanding(brief_tests._need())
    restated = _search(search_request="grey sofas under 3000 SAR")
    coordinator, _ = _coordinator(restated, understanding)

    await coordinator.run(brief_tests._typed(AgentStateV1(), "grey, under 3000"))

    assert understanding.messages[-1] == "grey sofas under 3000 SAR"
    assert set(understanding.messages) <= {"grey, under 3000", "grey sofas under 3000 SAR"}


async def test_a_reading_that_failed_is_handled_once_never_read_again() -> None:
    """Query understanding already spent its own corrective attempt; reading
    again would double it - and double the wait in an outage (CLAUDE.md 21.1).
    The failure is handled exactly as a fresh reading's would be."""
    from app.core.exceptions import IntegrationUnavailableError
    from app.schemas.grounding import TurnFailureCode

    understanding = CountingUnderstanding(
        brief_tests._need(), fail_first=IntegrationUnavailableError()
    )
    coordinator, _ = _coordinator(_search(), understanding)

    result = await coordinator.run(brief_tests._typed(AgentStateV1(), "I need a sofa"))

    assert understanding.messages == ["I need a sofa"]
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.SEARCH_UNAVAILABLE


async def test_an_unused_reading_is_discarded_without_failing_the_turn() -> None:
    """An answer turn never searches: the reading in flight is cancelled, and
    even one that failed is collected, never raised."""
    from app.core.exceptions import IntegrationUnavailableError

    understanding = CountingUnderstanding(
        brief_tests._need(), fail_first=IntegrationUnavailableError(), delay=0.05
    )
    coordinator, _ = _coordinator(CustomerAgentDecision(action=AgentAction.ANSWER), understanding)

    result = await coordinator.run(brief_tests._typed(AgentStateV1(), "thanks!"))

    assert result.decision.action is AgentAction.ANSWER
    assert coordinator._speculation is None


async def test_no_reading_while_a_card_waits_for_its_answer() -> None:
    """The answer to a card is restated by the decision, never searched as typed."""
    understanding = CountingUnderstanding(brief_tests._need())
    coordinator, _ = _coordinator(CustomerAgentDecision(action=AgentAction.ANSWER), understanding)
    asked = await brief_tests._coordinator(_search())[0].run(
        brief_tests._typed(AgentStateV1(), "I need a sofa")
    )
    assert asked.state.product_brief.pending is not None

    await coordinator.run(brief_tests._typed(asked.state, "grey please"))

    assert understanding.messages == []


async def test_switched_off_it_reads_only_after_the_decision() -> None:
    understanding = CountingUnderstanding(brief_tests._need())
    coordinator, _ = _coordinator(CustomerAgentDecision(action=AgentAction.ANSWER), understanding)
    coordinator._speculative_interpretation = False

    await coordinator.run(brief_tests._typed(AgentStateV1(), "thanks!"))

    assert understanding.messages == []


class SlowDecisions:
    """A decision that takes a while - so the reading really runs alongside."""

    def __init__(self, decision: CustomerAgentDecision, understanding: CountingUnderstanding):
        self.decision = decision
        self.understanding = understanding
        self.readings_started_before_deciding: int = 0

    async def decide(self, decision_input: Any, **_: Any) -> CustomerAgentDecision:
        await asyncio.sleep(0.05)
        self.readings_started_before_deciding = len(self.understanding.messages)
        return self.decision


async def test_the_reading_runs_while_the_decision_is_made() -> None:
    understanding = CountingUnderstanding(brief_tests._need())
    coordinator, _ = _coordinator(_search(), understanding)
    decisions = SlowDecisions(_search(), understanding)
    coordinator._decisions = decisions

    await coordinator.run(brief_tests._typed(AgentStateV1(), "I need a sofa"))

    assert decisions.readings_started_before_deciding == 1
    assert understanding.messages == ["I need a sofa"]


async def test_a_change_of_type_reuses_the_reading() -> None:
    """ "Make them sectionals" is read from the message itself: always a hit."""
    from app.schemas.agent_decision import CommercialReason
    from app.schemas.refinement import SearchRefinementDelta

    understanding = CountingUnderstanding(brief_tests._need("sectional-sofa"))
    searched, _ = brief_tests._coordinator(_search(skip_questions=True))
    shown = await searched.run(brief_tests._typed(AgentStateV1(), "just show me sofas"))
    refine = CustomerAgentDecision(
        action=AgentAction.REFINE_SEARCH,
        commercial_reason=CommercialReason.CUSTOMER_REQUEST,
        taxonomy_change_requested=True,
        refinement=SearchRefinementDelta(),
    )
    coordinator, _ = _coordinator(refine, understanding)

    await coordinator.run(brief_tests._typed(shown.state, "make them sectionals"))

    assert understanding.messages == ["make them sectionals"]


async def test_no_reading_while_a_room_is_being_asked_about() -> None:
    from app.schemas.agent_state import RoomProjectState

    understanding = CountingUnderstanding(brief_tests._need())
    coordinator, _ = _coordinator(CustomerAgentDecision(action=AgentAction.ANSWER), understanding)
    asking = AgentStateV1(room_project=RoomProjectState(room_type="living_room"))

    await coordinator.run(brief_tests._typed(asking, "around 15000"))

    assert understanding.messages == []


async def test_a_defect_in_an_unused_reading_never_fails_the_turn() -> None:
    understanding = CountingUnderstanding(brief_tests._need(), fail_first=KeyError("boom"))
    coordinator, _ = _coordinator(CustomerAgentDecision(action=AgentAction.ANSWER), understanding)
    coordinator._decisions = SlowDecisions(
        CustomerAgentDecision(action=AgentAction.ANSWER), understanding
    )

    result = await coordinator.run(brief_tests._typed(AgentStateV1(), "thanks!"))

    assert result.decision.action is AgentAction.ANSWER
