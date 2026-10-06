"""The words a customer reads, in the session's language (docs/arabic-replies-plan.md).

Phase 2: the reply writer and the decision model's own question. Everything a
model reasons with stays English; only the prose the customer reads changes.
Off, both prompts are exactly what they were.
"""

from __future__ import annotations

import pytest
from app.prompts.customer_commerce import response_v1
from app.prompts.customer_commerce import v1 as decision_prompt
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import CustomerResponse, CustomerTurnResult, TurnGrounding
from app.schemas.language import ReplyLanguage
from app.services.response_generator import CustomerResponseGenerator, _ends_on_a_question

from tests.unit.test_response_generator import AMBIGUOUS, FakeClient, _search, _turn

AR, EN = ReplyLanguage.AR, ReplyLanguage.EN
ARABIC_REPLY = CustomerResponse(message="هذه بعض الكنب التي قد تناسبك. هل تفضل لونا معينا؟")
UNSUPPORTED_FIGURE = CustomerResponse(message="These start from 1299, a great price.")


def _result(
    *, reply_language: ReplyLanguage | None, stored: ReplyLanguage | None = None
) -> CustomerTurnResult:
    return CustomerTurnResult(
        state=AgentStateV1(reply_language=stored),
        decision=CustomerAgentDecision(action=AgentAction.SEARCH),
        grounding=TurnGrounding(search=_search()),
        reply_language=reply_language,
    )


async def _instructions_used(
    result: CustomerTurnResult, *, arabic_replies: bool, reply: CustomerResponse = ARABIC_REPLY
) -> list[str]:
    client = FakeClient(reply)
    await CustomerResponseGenerator(client, arabic_replies=arabic_replies).generate(_turn(), result)
    return [call["instructions"] for call in client.calls]


# ── the writer's two prompts ────────────────────────────────────────────────


def test_the_english_prompt_is_exactly_todays() -> None:
    assert response_v1.build_instructions() == response_v1.INSTRUCTIONS
    assert response_v1.build_instructions(EN) == response_v1.INSTRUCTIONS
    assert response_v1.version_for(EN) == response_v1.VERSION


def test_the_arabic_prompt_differs_only_in_its_language_paragraph() -> None:
    arabic = response_v1.build_instructions(AR)

    assert "Reply in English." not in arabic
    assert arabic.replace(response_v1._ARABIC, response_v1._ENGLISH, 1) == (
        response_v1.INSTRUCTIONS
    )
    assert response_v1.version_for(AR) == response_v1.ARABIC_VERSION


def test_the_arabic_prompt_keeps_figures_as_digits() -> None:
    """The number check compares digits; a price written in words could not be
    checked, and one in Arabic-Indic digits would be refused."""
    flat = " ".join(response_v1._ARABIC.split())
    assert "Western digits" in flat
    assert "Never write a figure in words or in Arabic-Indic digits" in flat


@pytest.mark.parametrize("language", list(ReplyLanguage))
def test_the_correction_follows_the_language(language: ReplyLanguage) -> None:
    correction = response_v1.build_correction_instructions(language)

    assert correction == response_v1.build_instructions(language) + response_v1.CORRECTION


# ── which prompt a reply is written with ────────────────────────────────────


async def test_on_an_arabic_turn_is_written_in_arabic() -> None:
    used = await _instructions_used(_result(reply_language=AR), arabic_replies=True)

    assert used == [response_v1.build_instructions(AR)]


async def test_on_an_english_turn_is_written_exactly_as_before() -> None:
    used = await _instructions_used(_result(reply_language=EN), arabic_replies=True)

    assert used == [response_v1.INSTRUCTIONS]


@pytest.mark.parametrize("reply_language", [None, AR])
async def test_off_every_reply_is_english_whatever_was_stored(
    reply_language: ReplyLanguage | None,
) -> None:
    used = await _instructions_used(
        _result(reply_language=reply_language, stored=AR), arabic_replies=False
    )

    assert used == [response_v1.INSTRUCTIONS]


async def test_a_look_that_settled_no_language_answers_in_the_sessions() -> None:
    """The comparison pop-up is not a turn: it carries no settled language, so
    the stored one decides."""
    used = await _instructions_used(_result(reply_language=None, stored=AR), arabic_replies=True)

    assert used == [response_v1.build_instructions(AR)]


async def test_the_retry_is_in_the_same_language() -> None:
    used = await _instructions_used(
        _result(reply_language=AR), arabic_replies=True, reply=UNSUPPORTED_FIGURE
    )

    assert used == [
        response_v1.build_instructions(AR),
        response_v1.build_correction_instructions(AR),
    ]


async def test_the_design_hand_off_question_is_in_the_turns_language() -> None:
    """The one hand-off that still owes a question words it with a model."""
    client = FakeClient(ARABIC_REPLY)
    result = CustomerTurnResult(
        state=AgentStateV1(),
        decision=CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        grounding=TurnGrounding(
            design_handoff_requested=True, deterministic_clarification=AMBIGUOUS
        ),
        reply_language=AR,
    )

    await CustomerResponseGenerator(client, arabic_replies=True).generate(_turn(), result)

    assert [call["instructions"] for call in client.calls] == [response_v1.build_instructions(AR)]


def test_the_arabic_prompt_fixes_the_form_of_address_and_prices() -> None:
    flat = " ".join(response_v1._ARABIC.split())
    assert "masculine form unless they have said otherwise" in flat
    assert 'never "1250.00"' in flat


# ── a reply that already asks ───────────────────────────────────────────────


async def test_an_arabic_question_mark_counts_as_a_question() -> None:
    """Without it, an Arabic reply ending in "؟" had an English question
    appended to it."""
    client = FakeClient(ARABIC_REPLY)
    response = await CustomerResponseGenerator(client, arabic_replies=True).generate(
        _turn(), _result(reply_language=AR)
    )

    assert response.message == ARABIC_REPLY.message


def test_an_arabic_follow_up_counts_as_a_question() -> None:
    reply = CustomerResponse(
        message="هذه بعض الخيارات المناسبة.",
        follow_up_question="كم شخصا سيجلس عليها عادة؟",
    )

    assert _ends_on_a_question(reply, _result(reply_language=AR)) is reply


# ── the decision model's own question ───────────────────────────────────────


def test_the_decision_writes_only_its_question_in_the_sessions_language() -> None:
    on = " ".join(decision_prompt.build_instructions(reply_language=True).split())

    assert "Only clarification.question is written for the customer to read" in on
    assert "Every other field stays in English" in on
    assert "a search always sets search_request, and a design hand-off always sets" in on
    assert "design_question, to what they asked in plain English" in on
    assert "Reply in English." not in on


def test_off_the_decision_prompt_still_says_english() -> None:
    assert "Reply in English." in decision_prompt.build_instructions()
    assert decision_prompt.build_instructions() == decision_prompt.INSTRUCTIONS
