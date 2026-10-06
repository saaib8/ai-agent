"""The application's own sentences in Arabic (docs/arabic-replies-plan.md, phase 3).

The catalog is keyed by the English sentence, so the English tables stay the one
source of what is said. These tests make it total in both directions, keep the
Arabic to the same rules as the English (no figure, a question only where the
English asks one), and prove the writer reads it on every path a fixed sentence
reaches a customer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import ModuleType
from typing import Any

import pytest
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import CustomerResponse, CustomerTurnResult, TurnGrounding
from app.schemas.grounding import TurnFailure, TurnFailureCode
from app.schemas.language import ReplyLanguage
from app.schemas.next_step import ANY_NEXT_STEP, QUESTIONS, NextStep, NextStepKind
from app.services import response_wording
from app.services.arabic_wording import ARABIC, in_language
from app.services.response_generator import CustomerResponseGenerator

from tests.unit.test_response_generator import FakeClient, _search, _turn

AR, EN = ReplyLanguage.AR, ReplyLanguage.EN


def _sentences_in(value: object) -> set[str]:
    """Every string inside a constant, however its table is shaped."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        return set().union(*(_sentences_in(v) for v in value.values()))
    if isinstance(value, tuple | list | set | frozenset):
        return set().union(*(_sentences_in(v) for v in value))
    return set()


def _wording_constants() -> dict[str, object]:
    """`response_wording`'s own module-level values - imports and functions
    aside - whatever their names are cased."""
    return {
        name: value
        for name, value in vars(response_wording).items()
        if not name.startswith("__")
        and not callable(value)
        and not isinstance(value, ModuleType)
        and name != "annotations"
    }


def _english_sentences() -> set[str]:
    """Every fixed sentence a customer can read, found rather than listed, so a
    table added later is covered without anyone remembering to add it here."""
    found: set[str] = {ANY_NEXT_STEP, *QUESTIONS.values()}
    for value in _wording_constants().values():
        found |= _sentences_in(value)
    return found


def test_every_wording_constant_holds_sentences() -> None:
    """A constant the scan finds no sentence in is a shape it does not read -
    and a table it would skip in silence."""
    unread = sorted(name for name, v in _wording_constants().items() if not _sentences_in(v))
    assert not unread, f"the scan cannot read {unread}"


# ── the catalog ─────────────────────────────────────────────────────────────


def test_the_scan_finds_the_tables() -> None:
    """Guards the guard: a scan that found nothing would pass everything."""
    sentences = _english_sentences()
    assert len(sentences) >= 50
    assert response_wording.FALLBACK_WORDING[next(iter(response_wording.FALLBACK_WORDING))] in (
        sentences
    )


def test_every_english_sentence_has_its_arabic() -> None:
    missing = sorted(_english_sentences() - set(ARABIC))
    assert not missing, f"untranslated: {missing}"


def test_no_arabic_entry_has_lost_its_english() -> None:
    """An English sentence edited without its Arabic leaves an orphan here."""
    orphans = sorted(set(ARABIC) - _english_sentences())
    assert not orphans, f"orphaned: {orphans}"


@pytest.mark.parametrize("english", sorted(ARABIC), ids=lambda s: s[:40])
def test_the_arabic_is_arabic_and_figure_free(english: str) -> None:
    arabic = ARABIC[english]

    # `\d` is every script's digits: Western, Arabic-Indic and Persian alike.
    assert not re.search(r"\d", arabic), "no figure, in any digits"
    assert not re.search(r"[A-Za-z]", arabic), "no untranslated English"
    assert ("?" in english) == ("؟" in arabic), "a question exactly where the English asks one"
    assert "?" not in arabic


def test_english_is_returned_untouched() -> None:
    for sentence in _english_sentences():
        assert in_language(sentence, EN) is sentence


def test_an_untranslated_sentence_is_still_said() -> None:
    """Unreachable while the totality test passes; if it ever is reached, the
    customer gets the English rather than nothing."""
    assert in_language("Something new.", AR) == "Something new."


# ── the writer reads it ─────────────────────────────────────────────────────


def _result(
    grounding: TurnGrounding,
    *,
    reply_language: ReplyLanguage | None,
    action: AgentAction = AgentAction.SEARCH,
    next_step: NextStep | None = None,
) -> CustomerTurnResult:
    return CustomerTurnResult(
        state=AgentStateV1(),
        decision=CustomerAgentDecision(action=action),
        grounding=grounding,
        reply_language=reply_language,
        next_step=next_step,
    )


async def _reply(
    result: CustomerTurnResult, *replies: Any, arabic_replies: bool = True
) -> CustomerResponse:
    client = FakeClient(*(replies or (CustomerResponse(message="unused"),)))
    return await CustomerResponseGenerator(client, arabic_replies=arabic_replies).generate(
        _turn(), result
    )


FAILED = TurnGrounding(failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE))
FAILURE = response_wording.FAILURE_WORDING[TurnFailureCode.SEARCH_UNAVAILABLE]


async def test_a_handled_failure_is_said_in_arabic() -> None:
    response = await _reply(_result(FAILED, reply_language=AR))

    assert response.message.startswith(ARABIC[FAILURE])


async def test_off_a_handled_failure_is_english_whatever_was_stored() -> None:
    result = _result(FAILED, reply_language=AR).model_copy(
        update={"state": AgentStateV1(reply_language=AR)}
    )

    response = await _reply(result, arabic_replies=False)

    assert response.message.startswith(FAILURE)


async def test_a_reply_that_could_not_be_written_falls_back_in_arabic() -> None:
    from app.core.exceptions import LLMRequestError

    response = await _reply(
        _result(TurnGrounding(search=_search()), reply_language=AR), LLMRequestError()
    )

    fallback = response_wording.FALLBACK_WORDING[
        next(k for k in response_wording.FALLBACK_WORDING if k.value == "search_results")
    ]
    assert response.message.startswith(ARABIC[fallback])


async def test_the_next_step_added_to_an_arabic_reply_is_arabic() -> None:
    """The mixed reply QA found after a room swap: Arabic prose, then the
    English next-step question."""
    result = _result(
        TurnGrounding(search=_search()),
        reply_language=AR,
        next_step=NextStep(kind=NextStepKind.KEEP_BROWSING),
    )

    response = await _reply(result, CustomerResponse(message="هذه بعض الخيارات المناسبة لك."))

    assert response.message == (
        "هذه بعض الخيارات المناسبة لك. " + ARABIC[QUESTIONS[NextStepKind.KEEP_BROWSING]]
    )


async def test_the_next_step_added_to_an_english_reply_is_unchanged() -> None:
    result = _result(
        TurnGrounding(search=_search()),
        reply_language=EN,
        next_step=NextStep(kind=NextStepKind.KEEP_BROWSING),
    )

    response = await _reply(result, CustomerResponse(message="Here are a few options."))

    assert response.message == ("Here are a few options. " + QUESTIONS[NextStepKind.KEEP_BROWSING])
