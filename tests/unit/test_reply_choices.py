"""Questions and answers travel together from the backend to the chat UI."""

import pytest
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CustomerAgentDecision,
    FollowUpGoal,
    FollowUpPolicy,
)
from app.schemas.agent_turn import CustomerResponse, TurnGrounding
from app.schemas.language import ReplyLanguage
from app.schemas.next_step import NextStep, NextStepKind
from app.schemas.reply_choice import ReplyChoice
from app.schemas.response import ResponseViolationKind
from app.schemas.text_choice import TextReplyChoice
from app.services.chat_runtime import ChatRuntime
from app.services.media_wording import photo_reply
from app.services.next_step import next_step
from app.services.numeric_guard import build_allowance
from app.services.response_generator import CustomerResponseGenerator
from app.services.response_validation import validate_response
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from tests.unit.test_chat_api import SESSION, STORE, Harness
from tests.unit.test_response_generator import AMBIGUOUS, FakeClient, _result, _turn


@pytest.mark.parametrize(
    ("language", "question", "answers"),
    [
        (
            "en",
            "Would you like me to prioritize the burnt-orange colour or the curved headboard?",
            ["Burnt-orange colour", "Curved headboard"],
        ),
        (
            "ar",
            "هل تفضّل إعطاء الأولوية للون البرتقالي أم لشكل اللوح المنحني؟",
            ["اللون البرتقالي", "شكل اللوح المنحني"],
        ),
        ("en", "Should I focus on the price or the style?", ["Price", "Style"]),
        ("ar", "هل نعطي الأولوية للسعر أم للطراز؟", ["السعر", "الطراز"]),
    ],
)
async def test_clarification_answers_reach_the_http_response_without_reinterpretation(
    language: str, question: str, answers: list[str]
) -> None:
    choices = tuple(TextReplyChoice(label=answer, value=answer) for answer in answers)
    decision = CustomerAgentDecision(
        action=AgentAction.CLARIFY,
        follow_up_policy=FollowUpPolicy.NONE,
        clarification=BlockingClarification(
            reason=BlockingClarificationReason.UNDEFINED_QUALITY_CRITERION,
            question=question,
            choices=choices,
        ),
    )
    client = FakeClient(CustomerResponse(message="Unused."))
    harness = Harness(
        decision,
        responses=CustomerResponseGenerator(client, arabic_replies=True),  # type: ignore[arg-type]
        arabic_replies=True,
    )
    async with AsyncClient(
        transport=ASGITransport(app=harness.app()), base_url="http://test"
    ) as http:
        response = await http.post(
            "/v1/chat",
            json={
                "session_id": SESSION,
                "store_id": STORE,
                "locale": language,
                "message": "Help me narrow these down"
                if language == "en"
                else "ساعدني في تضييق الخيارات",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["response"]["message"] == question
    assert [choice["label"] for choice in body["presentation"]["choices"]] == answers
    assert [choice["value"] for choice in body["presentation"]["choices"]] == answers
    assert all(choice["product_action"] is None for choice in body["presentation"]["choices"])
    assert client.calls == [], "clarification and answers need no second model call"
    assert harness.sessions.saved[(STORE, SESSION)].conversation.messages[-1].content == question


async def test_writer_question_and_answers_are_kept_together() -> None:
    choices = (
        TextReplyChoice(label="Colour", value="Prioritize the colour"),
        TextReplyChoice(label="Shape", value="Prioritize the shape"),
    )
    response = CustomerResponse(
        message="We can narrow these further.",
        follow_up_question="Should I prioritize the colour or the shape?",
        choices=choices,
    )
    result = _result(TurnGrounding(follow_up_policy=FollowUpPolicy.OPTIONAL))
    generated = await CustomerResponseGenerator(FakeClient(response)).generate(_turn(), result)
    presentation = ChatRuntime.presentation(result, generated)
    assert generated.choices == choices
    assert presentation is not None
    assert [c.value for c in presentation.choices] == [c.value for c in choices]


async def test_a_reply_asking_its_own_question_keeps_it() -> None:
    """The summary can call for the reply's own question - what setting a
    requirement aside would find - and the reply keeps it, with its answers;
    the next step's question is not appended beside it."""
    question = CustomerResponse(
        message="Which colour?",
        choices=[
            {"label": "Blue", "value": "Blue"},
        ],
    )
    step = NextStep(
        kind=NextStepKind.START, chips=(ReplyChoice(label="Find a piece", value="Find a piece"),)
    )
    result = _result(TurnGrounding()).model_copy(update={"next_step": step})
    response = await CustomerResponseGenerator(FakeClient(question)).generate(_turn(), result)
    assert response.message == "Which colour?"
    assert not response.message.endswith(step.question)
    assert [c.label for c in response.choices] == ["Blue"]


async def test_composed_design_acknowledgement_preserves_question_answers() -> None:
    choices = (
        TextReplyChoice(label="First", value="The first one"),
        TextReplyChoice(label="Second", value="The second one"),
    )
    question = CustomerResponse(message="Which product did you mean?", choices=choices)
    result = _result(
        TurnGrounding(design_handoff_requested=True, deterministic_clarification=AMBIGUOUS),
        action=AgentAction.DESIGN_HANDOFF,
    )
    response = await CustomerResponseGenerator(FakeClient(question)).generate(_turn(), result)
    assert response.message.endswith(question.message)
    assert response.choices == choices


def test_a_reply_asking_its_own_question_shows_its_own_answers() -> None:
    owned = (ReplyChoice(label="Find a piece", value="Find a piece"),)
    result = _result(TurnGrounding()).model_copy(
        update={
            "next_step": NextStep(kind=NextStepKind.START, chips=owned),
        }
    )
    response = CustomerResponse(
        message="Which colour?", choices=(TextReplyChoice(label="Blue", value="Blue"),)
    )
    presentation = ChatRuntime.presentation(result, response)
    assert presentation is not None
    assert [c.label for c in presentation.choices] == ["Blue"]


def test_plain_question_without_choices_does_not_invent_answers() -> None:
    response = CustomerResponse(message="Would you prefer the colour or the shape?")
    assert ChatRuntime.presentation(_result(TurnGrounding()), response) is None


async def test_specific_preference_question_is_not_replaced_by_generic_next_step() -> None:
    result = _result(TurnGrounding(follow_up_policy=FollowUpPolicy.OPTIONAL))
    result = result.model_copy(
        update={
            "decision": result.decision.model_copy(
                update={
                    "follow_up_goal": FollowUpGoal.PRODUCT_PREFERENCE,
                }
            )
        }
    )
    # The step is kept, so a reply that asked nothing still ends on chips;
    # a reply that asks its own question with answers shows those instead.
    result = result.model_copy(update={"next_step": next_step(result, None)})
    assert result.next_step is not None
    response = CustomerResponse(
        message="Would you prioritize colour or shape?",
        choices=[
            {"label": "Colour", "value": "Colour"},
            {"label": "Shape", "value": "Shape"},
        ],
    )
    generated = await CustomerResponseGenerator(FakeClient(response)).generate(_turn(), result)
    presentation = ChatRuntime.presentation(result, generated)
    assert presentation is not None
    assert generated.message == "Would you prioritize colour or shape?"
    assert [choice.label for choice in presentation.choices] == ["Colour", "Shape"]


def test_existing_products_keep_browsing_context_on_an_answer_turn() -> None:
    result = _result(TurnGrounding())
    interaction = result.state.product_interaction.model_copy(
        update={
            "presented_product_ids": (10, 11),
        }
    )
    result = result.model_copy(
        update={
            "state": result.state.model_copy(
                update={
                    "product_interaction": interaction,
                }
            )
        }
    )
    step = next_step(result, None)
    assert step is not None
    assert step.kind is NextStepKind.KEEP_BROWSING


@pytest.mark.parametrize("language", list(ReplyLanguage))
@pytest.mark.parametrize("count", [0, 1, 3])
def test_photo_answers_are_owned_by_backend_and_match_available_results(
    language: ReplyLanguage, count: int
) -> None:
    response = photo_reply("sofa", count, language)
    assert len(response.choices) == (1 if count == 1 else 2)
    comparing = "قارن" if language is ReplyLanguage.AR else "Compare"
    assert any(comparing in c.label for c in response.choices) is (count >= 2)


@pytest.mark.parametrize(
    "bad",
    [
        {"label": " ", "value": "Valid"},
        {"label": "Valid", "value": " "},
        {"label": "Valid", "value": "Valid", "product_action": {"kind": "compare"}},
        {"label": "Valid", "value": "Valid", "bundle_action": {"kind": "swap"}},
    ],
)
def test_model_choices_cannot_be_blank_or_execute_actions(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TextReplyChoice.model_validate(bad)


def test_duplicate_and_excessive_answers_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CustomerResponse(
            message="Which one?",
            choices=[
                {"label": "Blue", "value": "Blue"},
                {"label": "blue", "value": "blue"},
            ],
        )
    with pytest.raises(ValidationError):
        BlockingClarification(
            reason=BlockingClarificationReason.UNDEFINED_QUALITY_CRITERION,
            question="Which one?",
            choices=[TextReplyChoice(label=str(i), value=str(i)) for i in range(7)],
        )


def test_choices_without_a_question_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CustomerResponse(message="Here you go.", choices=[{"label": "Yes", "value": "Yes"}])


def test_numeric_guard_also_checks_model_choice_text() -> None:
    response = CustomerResponse(
        message="Which budget?",
        choices=[
            {"label": "Under 99999", "value": "Under 99999"},
        ],
    )
    violation = validate_response(
        response,
        valid_grounding_refs=frozenset(),
        follow_up_allowed=True,
        allowance=build_allowance("show me sofas"),
    )
    assert violation is not None
    assert violation.kind is ResponseViolationKind.UNSUPPORTED_NUMBER
    assert violation.field == "choices"
