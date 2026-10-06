"""The eval runner's language checks (docs/arabic-replies-plan.md, phase 5).

The runner calls live servers and is never part of pytest, but the checks that
judge a reply are plain functions - and a check that passes everything would
make every Arabic case pass. These pin each one both ways.
"""

from __future__ import annotations

from typing import Any

from app.schemas.grounding import TurnFailureCode
from app.services.arabic_wording import ARABIC
from app.services.response_wording import FAILURE_WORDING
from evals.conversations.run import TurnResult, _asks, _check

ARABIC_REPLY = "هذه بعض الكنب الرمادية المناسبة لك. هل تودّ تضييق الخيارات؟"
ENGLISH_REPLY = "Here are a few grey sofas. Would you like to narrow these down?"


def _chat(message: str, *, reply_language: str | None, **body: Any) -> TurnResult:
    return TurnResult(
        status=200,
        elapsed_s=0.1,
        body={"response": {"message": message}, "reply_language": reply_language, **body},
    )


def test_either_question_mark_asks() -> None:
    assert _asks("كم شخصًا؟")
    assert _asks("How many?")
    assert not _asks("هذه بعض الخيارات.")


def test_an_arabic_reply_passes_the_arabic_check() -> None:
    assert _check({"arabic_reply": True}, _chat(ARABIC_REPLY, reply_language="ar")) == []


def test_english_prose_fails_the_arabic_check_whatever_the_flag_says() -> None:
    assert _check({"arabic_reply": True}, _chat(ENGLISH_REPLY, reply_language="ar"))


def test_arabic_prose_without_the_language_fails_the_arabic_check() -> None:
    assert _check({"arabic_reply": True}, _chat(ARABIC_REPLY, reply_language=None))


def test_the_english_check_is_the_mirror() -> None:
    assert _check({"english_reply": True}, _chat(ENGLISH_REPLY, reply_language="en")) == []
    assert _check({"english_reply": True}, _chat(ENGLISH_REPLY, reply_language=None)) == []
    assert _check({"english_reply": True}, _chat(ARABIC_REPLY, reply_language="ar"))


def test_the_pop_up_text_is_read_where_the_pop_up_puts_it() -> None:
    popup = TurnResult(status=200, elapsed_s=0.1, body={"message": ARABIC_REPLY}, kind="comparison")
    english = TurnResult(
        status=200, elapsed_s=0.1, body={"message": ENGLISH_REPLY}, kind="comparison"
    )

    assert _check({"arabic_text": True}, popup) == []
    assert _check({"arabic_text": True}, english)


def test_the_fallback_is_caught_in_either_language() -> None:
    english = FAILURE_WORDING[TurnFailureCode.REQUEST_NOT_UNDERSTOOD]

    assert _check({"not_fallback": True}, _chat(english, reply_language="en"))
    assert _check({"not_fallback": True}, _chat(ARABIC[english], reply_language="ar"))
    assert _check({"not_fallback": True}, _chat(ARABIC_REPLY, reply_language="ar")) == []


def test_an_arabic_question_with_chips_is_not_a_dead_end() -> None:
    turn = _chat(
        ARABIC_REPLY,
        reply_language="ar",
        presentation={"choices": [{"label": "Rugs", "value": "Rugs"}]},
    )

    assert _check({"next_step": True}, turn) == []


def test_the_offer_is_recognised_by_its_no_thanks_in_either_language() -> None:
    for no_thanks in ("No thanks", "لا، شكرًا"):
        turn = _chat(
            ARABIC_REPLY,
            reply_language="ar",
            presentation={
                "focus": {"name_english": "A sofa"},
                "choices": [{"label": "Rugs", "value": "Rugs"}, {"label": no_thanks, "value": "x"}],
            },
        )
        assert _check({"offer": True}, turn) == [], no_thanks


def test_every_label_beside_the_reply_must_be_arabic() -> None:
    arabic = _chat(
        ARABIC_REPLY,
        reply_language="ar",
        presentation={
            "choices": [
                {"label": "أرني المزيد", "value": "x"},
                {"label": "أقل من 10,000 ريال", "value": "y"},
            ]
        },
    )
    english = _chat(
        ARABIC_REPLY,
        reply_language="ar",
        presentation={
            "choices": [
                {"label": "أرني المزيد", "value": "x"},
                {"label": "Show me more", "value": "y"},
            ]
        },
    )

    assert _check({"arabic_chips": True}, arabic) == []
    assert _check({"arabic_chips": True}, english)
