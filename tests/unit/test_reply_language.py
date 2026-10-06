"""Which language a session is answered in (docs/arabic-replies-plan.md).

Three layers: the pure rule, the coordinator that applies it to a turn, and the
HTTP exchange that carries it across requests. Off, every layer must leave the
turn exactly as it was before Arabic replies existed.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from app.core.exceptions import LLMResponseInvalidError
from app.prompts.customer_commerce.v1 import (
    _LANGUAGE_SECTION,
    INSTRUCTIONS,
    build_instructions,
)
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision, with_reply_language
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import CustomerTurnInput, DecisionInput
from app.schemas.language import LanguageSource, ReplyLanguage
from app.schemas.search_action import MoreOptionsAction
from app.services.customer_decision import CustomerAgentDecisionService, _constrained_schema
from app.services.reply_language import (
    TurnLanguage,
    after_the_decision,
    before_the_turn,
    writes_arabic_script,
)
from app.taxonomy.attributes import load_catalog_attributes
from httpx import ASGITransport, AsyncClient
from openai.lib._pydantic import to_strict_json_schema

from tests.conftest import build_settings
from tests.unit.test_chat_api import SESSION, STORE, Harness, a_search, body
from tests.unit.test_turn_coordinator import CONTEXT, _coordinator, _state

AR, EN = ReplyLanguage.AR, ReplyLanguage.EN
ARABIC = "أبي كنبة رمادية"
ENGLISH = "show me grey sofas"


def _answer(**fields: Any) -> CustomerAgentDecision:
    return CustomerAgentDecision(action=AgentAction.ANSWER, **fields)


# ── reading the script ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        ARABIC,
        "ابغى طاولة قهوة",
        "كنبة 3 مقاعد بـ 3000",
        "كنبة L-shape رمادية",  # an English product word inside Arabic
        "٣ كنب",
    ],
)
def test_arabic_letters_are_read_as_arabic(message: str) -> None:
    assert writes_arabic_script(message)


@pytest.mark.parametrize(
    "message",
    [
        ENGLISH,
        "abi kanaba",  # Arabizi: Latin letters, left to the decision model
        "3000",
        "٣٠٠٠",  # Arabic-Indic digits are not letters
        "👍",
        "",
        "the كنبة please",  # mostly Latin
        "a ب",  # one stray Arabic letter says nothing
    ],
)
def test_other_text_is_not_read_as_arabic(message: str) -> None:
    assert not writes_arabic_script(message)


# ── before the turn ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stored", "message", "locale", "expected"),
    [
        # An undecided session: the script decides, then the locale, then English.
        (None, ARABIC, None, TurnLanguage(AR, AR, LanguageSource.SCRIPT)),
        (None, ARABIC, EN, TurnLanguage(AR, AR, LanguageSource.SCRIPT)),
        (None, ENGLISH, AR, TurnLanguage(None, AR, LanguageSource.LOCALE)),
        (None, ENGLISH, EN, TurnLanguage(None, EN, LanguageSource.LOCALE)),
        (None, ENGLISH, None, TurnLanguage(None, EN, LanguageSource.DEFAULT)),
        (None, "3000", None, TurnLanguage(None, EN, LanguageSource.DEFAULT)),
        # Arabic is sticky: English words, figures and a locale never move it.
        (AR, ENGLISH, None, TurnLanguage(AR, AR, LanguageSource.SESSION)),
        (AR, "3000", EN, TurnLanguage(AR, AR, LanguageSource.SESSION)),
        (AR, ARABIC, None, TurnLanguage(AR, AR, LanguageSource.SESSION)),
        # English chosen explicitly is never undone by Arabic letters.
        (EN, ARABIC, None, TurnLanguage(EN, EN, LanguageSource.SESSION)),
        (EN, ENGLISH, AR, TurnLanguage(EN, EN, LanguageSource.SESSION)),
    ],
)
def test_the_language_a_turn_starts_in(
    stored: ReplyLanguage | None,
    message: str,
    locale: ReplyLanguage | None,
    expected: TurnLanguage,
) -> None:
    assert before_the_turn(stored, message, locale) == expected


# ── after the decision ──────────────────────────────────────────────────────


@pytest.mark.parametrize("started_stored", [None, AR, EN])
@pytest.mark.parametrize("switch_to", [AR, EN])
def test_an_explicit_request_wins_either_way(
    started_stored: ReplyLanguage | None, switch_to: ReplyLanguage
) -> None:
    started = TurnLanguage(started_stored, started_stored or EN, LanguageSource.SESSION)

    settled = after_the_decision(started, switch_to=switch_to, writes_arabizi=True)

    assert settled == TurnLanguage(switch_to, switch_to, LanguageSource.SWITCH)


def test_arabizi_moves_an_undecided_session_to_arabic() -> None:
    started = TurnLanguage(None, EN, LanguageSource.DEFAULT)

    settled = after_the_decision(started, switch_to=None, writes_arabizi=True)

    assert settled == TurnLanguage(AR, AR, LanguageSource.ARABIZI)


def test_arabizi_overrides_a_locale_that_was_never_stored() -> None:
    started = TurnLanguage(None, EN, LanguageSource.LOCALE)

    assert after_the_decision(started, switch_to=None, writes_arabizi=True).stored is AR


def test_arabizi_never_undoes_an_explicit_english_choice() -> None:
    started = TurnLanguage(EN, EN, LanguageSource.SESSION)

    assert after_the_decision(started, switch_to=None, writes_arabizi=True) is started


def test_nothing_said_leaves_the_turn_as_it_started() -> None:
    started = TurnLanguage(AR, AR, LanguageSource.SESSION)

    assert after_the_decision(started, switch_to=None, writes_arabizi=False) is started


# ── the coordinator, switched off ───────────────────────────────────────────


async def test_off_nothing_is_read_stored_or_passed_on() -> None:
    """Today's behaviour exactly, whatever the customer writes or sends."""
    coordinator, parts = _coordinator(
        _answer(switch_reply_language=AR, writes_arabizi=True), arabic_replies=False
    )
    turn = CustomerTurnInput(message=ARABIC, state=_state(), context=CONTEXT, locale=AR)

    result = await coordinator.run(turn)

    assert result.reply_language is None
    assert result.state.reply_language is None
    assert parts["decisions"].inputs[0].reply_language is None


async def test_off_a_stored_language_is_left_alone() -> None:
    """A session written while the switch was on keeps its value untouched,
    so turning the switch back on later resumes it."""
    coordinator, _ = _coordinator(_answer(), arabic_replies=False)
    state = _state().model_copy(update={"reply_language": AR})

    result = await coordinator.run(CustomerTurnInput(message=ENGLISH, state=state, context=CONTEXT))

    assert result.reply_language is None
    assert result.state.reply_language is AR


# ── the coordinator, switched on ────────────────────────────────────────────


async def _run_on(
    decision: CustomerAgentDecision,
    message: str,
    *,
    stored: ReplyLanguage | None = None,
    locale: ReplyLanguage | None = None,
    decision_error: Exception | None = None,
) -> tuple[Any, Any]:
    coordinator, parts = _coordinator(decision, decision_error=decision_error, arabic_replies=True)
    state = _state().model_copy(update={"reply_language": stored})
    turn = CustomerTurnInput(message=message, state=state, context=CONTEXT, locale=locale)
    return await coordinator.run(turn), parts


async def test_an_arabic_message_starts_an_arabic_session() -> None:
    result, parts = await _run_on(_answer(), ARABIC)

    assert result.reply_language is AR
    assert result.state.reply_language is AR
    assert parts["decisions"].inputs[0].reply_language is AR


async def test_an_english_message_keeps_an_arabic_session() -> None:
    result, parts = await _run_on(_answer(), ENGLISH, stored=AR)

    assert result.reply_language is AR
    assert result.state.reply_language is AR
    # The model is told so - the input the LANGUAGE section exists to explain.
    assert parts["decisions"].inputs[0].reply_language is AR


async def test_an_undecided_english_session_stores_nothing() -> None:
    result, parts = await _run_on(_answer(), ENGLISH)

    assert result.reply_language is EN
    assert result.state.reply_language is None
    assert parts["decisions"].inputs[0].reply_language is None


async def test_asking_for_english_switches_and_is_remembered() -> None:
    result, _ = await _run_on(_answer(switch_reply_language=EN), "English please", stored=AR)

    assert result.reply_language is EN
    assert result.state.reply_language is EN


async def test_asking_for_arabic_switches_an_english_session() -> None:
    result, _ = await _run_on(_answer(switch_reply_language=AR), "تكلم عربي", stored=EN)

    assert result.reply_language is AR
    assert result.state.reply_language is AR


async def test_arabizi_read_by_the_decision_starts_an_arabic_session() -> None:
    result, _ = await _run_on(_answer(writes_arabizi=True), "abi kanaba")

    assert result.reply_language is AR
    assert result.state.reply_language is AR


async def test_the_locale_answers_the_turn_but_is_never_stored() -> None:
    result, parts = await _run_on(_answer(), ENGLISH, locale=AR)

    assert result.reply_language is AR
    assert result.state.reply_language is None
    assert parts["decisions"].inputs[0].reply_language is AR


async def test_a_turn_we_could_not_understand_keeps_the_language() -> None:
    """The fallback answers in the language the turn started in - settled
    before the decision that failed."""
    result, _ = await _run_on(
        _answer(), ARABIC, decision_error=LLMResponseInvalidError(reason="bad")
    )

    assert result.reply_language is AR
    assert result.state.reply_language is AR


async def test_a_screen_action_keeps_the_session_language() -> None:
    """A tap carries no words to read; the session's language stands."""
    coordinator, _ = _coordinator(_answer(), arabic_replies=True)
    state = _state().model_copy(update={"reply_language": AR})
    turn = CustomerTurnInput(
        message="more", state=state, context=CONTEXT, search_action=MoreOptionsAction()
    )

    result = await coordinator.run(turn)

    assert result.reply_language is AR
    assert result.state.reply_language is AR


async def test_the_language_never_changes_what_the_turn_does() -> None:
    """Presentation only: the same decision on the same state does the same
    thing in either language, apart from the language itself."""
    english, _ = await _run_on(_answer(), ENGLISH)
    arabic, _ = await _run_on(_answer(), ARABIC)

    def without_language(state: AgentStateV1) -> dict[str, Any]:
        return state.model_dump(exclude={"reply_language"})

    assert without_language(english.state) == without_language(arabic.state)
    assert english.grounding == arabic.grounding


# ── settings ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("environment", "expected"),
    [("local", True), ("stage", True), ("test", False), ("prod", False)],
)
def test_arabic_replies_default_by_environment(environment: str, expected: bool) -> None:
    settings = build_settings(environment=environment)

    assert settings.customer_agent.arabic_replies is expected


@pytest.mark.parametrize("environment", ["local", "stage", "test", "prod"])
@pytest.mark.parametrize("explicit", [True, False])
def test_an_explicit_setting_always_wins(environment: str, explicit: bool) -> None:
    settings = build_settings(environment=environment, customer_agent={"arabic_replies": explicit})

    assert settings.customer_agent.arabic_replies is explicit


# ── across requests ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def chat_client() -> AsyncIterator[Any]:
    clients: list[AsyncClient] = []

    async def build(harness: Harness) -> AsyncClient:
        client = AsyncClient(transport=ASGITransport(app=harness.app()), base_url="http://test")
        clients.append(client)
        return client

    try:
        yield build
    finally:
        for client in clients:
            await client.aclose()


async def test_an_arabic_session_stays_arabic_across_requests(chat_client: Any) -> None:
    harness = Harness(a_search(), a_search(), arabic_replies=True)
    client = await chat_client(harness)

    first = (await client.post("/v1/chat", json=body(ARABIC))).json()
    second = (await client.post("/v1/chat", json=body(ENGLISH, expected_session_revision=1))).json()

    assert first["reply_language"] == "ar"
    assert second["reply_language"] == "ar"
    assert harness.sessions.saved[(STORE, SESSION)].state.reply_language is AR


async def test_the_locale_is_per_request(chat_client: Any) -> None:
    harness = Harness(a_search(), a_search(), arabic_replies=True)
    client = await chat_client(harness)

    first = (await client.post("/v1/chat", json=body(ENGLISH, locale="ar"))).json()
    second = (await client.post("/v1/chat", json=body(ENGLISH, expected_session_revision=1))).json()

    assert first["reply_language"] == "ar"
    assert second["reply_language"] == "en"
    assert harness.sessions.saved[(STORE, SESSION)].state.reply_language is None


async def test_off_the_response_carries_no_language(chat_client: Any) -> None:
    harness = Harness(a_search())
    client = await chat_client(harness)

    payload = (await client.post("/v1/chat", json=body(ARABIC, locale="ar"))).json()

    assert payload["reply_language"] is None
    assert harness.sessions.saved[(STORE, SESSION)].state.reply_language is None


async def test_an_unknown_locale_is_refused(chat_client: Any) -> None:
    client = await chat_client(Harness(a_search(), arabic_replies=True))

    reply = await client.post("/v1/chat", json=body(ENGLISH, locale="fr"))

    assert reply.status_code == 422


# ── every rebuilt state carries the language ────────────────────────────────


def _rebuilds() -> list[tuple[str, int, set[str]]]:
    found = []
    for module in sorted((Path(__file__).parents[2] / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(module.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AgentStateV1"
                and node.keywords  # `AgentStateV1()` is a fresh state, not a rebuild
            ):
                names = {keyword.arg for keyword in node.keywords if keyword.arg}
                found.append((module.name, node.lineno, names))
    return found


def test_every_state_rebuild_names_every_field() -> None:
    """A state rebuilt field by field silently drops any field it forgets -
    which is how a stored language was lost on an ordinary turn. Every rebuild
    must name every field (schema_version aside, which is fixed)."""
    expected = set(AgentStateV1.model_fields) - {"schema_version"}
    rebuilds = _rebuilds()

    assert rebuilds, "the scan found no rebuilds - it is no longer checking anything"
    for module, line, names in rebuilds:
        assert names == expected, f"{module}:{line} misses {sorted(expected - names)}"


# ── what the decision model is asked ────────────────────────────────────────


class _RecordingClient:
    def __init__(self, decision: CustomerAgentDecision) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return "decision-model-under-test"

    async def parse(self, *, instructions: str, user_input: str, schema: type[Any]) -> Any:
        self.calls.append({"instructions": instructions, "schema": schema})
        return self.decision


def _schema_fields(schema: type[Any]) -> set[str]:
    return set(to_strict_json_schema(schema)["properties"])


@pytest.mark.parametrize("vocabulary", [False, True], ids=["plain", "constrained"])
async def test_off_the_model_is_asked_exactly_what_it_was_before(vocabulary: bool) -> None:
    """Same instructions, same response schema: the language fields are on the
    contract but invisible to the model."""
    attributes = load_catalog_attributes() if vocabulary else None
    client = _RecordingClient(_answer())
    service = CustomerAgentDecisionService(client, attributes)  # type: ignore[arg-type]

    await service.decide(DecisionInput(message=ENGLISH))

    call = client.calls[0]
    assert call["instructions"] == build_instructions(attributes)
    assert "LANGUAGE\n" not in call["instructions"]
    fields = _schema_fields(call["schema"])
    assert "switch_reply_language" not in fields
    assert "writes_arabizi" not in fields
    expected = _constrained_schema(attributes) if attributes else CustomerAgentDecision
    assert call["schema"] is expected, "the very schema main sends"


@pytest.mark.parametrize("vocabulary", [False, True], ids=["plain", "constrained"])
async def test_on_the_model_is_shown_and_told_about_the_language_fields(
    vocabulary: bool,
) -> None:
    attributes = load_catalog_attributes() if vocabulary else None
    client = _RecordingClient(_answer())
    service = CustomerAgentDecisionService(
        client,  # type: ignore[arg-type]
        attributes,
        reply_language=True,
    )

    await service.decide(DecisionInput(message=ENGLISH))

    call = client.calls[0]
    assert call["instructions"] == build_instructions(attributes, reply_language=True)
    assert "LANGUAGE\n" in call["instructions"]
    assert {"switch_reply_language", "writes_arabizi"} <= _schema_fields(call["schema"])


async def test_on_what_the_model_wrote_reaches_the_plain_contract() -> None:
    shown = with_reply_language(CustomerAgentDecision)
    client = _RecordingClient(
        shown(action=AgentAction.ANSWER, switch_reply_language=EN, writes_arabizi=True)
    )
    service = CustomerAgentDecisionService(client, reply_language=True)  # type: ignore[arg-type]

    decision = await service.decide(DecisionInput(message="English please"))

    assert type(decision) is CustomerAgentDecision
    assert decision.switch_reply_language is EN
    assert decision.writes_arabizi is True


def test_the_language_section_is_only_added_when_on() -> None:
    assert build_instructions() == INSTRUCTIONS
    on = build_instructions(reply_language=True)
    assert on != INSTRUCTIONS
    # The section, and in its place no blanket "Reply in English." - the
    # section says which field is in which language.
    assert on.replace(_LANGUAGE_SECTION, "", 1) == INSTRUCTIONS.replace(
        "Reply in English.\n\n", "", 1
    )


def test_the_language_section_teaches_the_counter_examples() -> None:
    """The live failure this guards: an English follow-up in an Arabic session
    read as a request for English."""
    flat = " ".join(_LANGUAGE_SECTION.split())
    assert "Writing in a language is not asking for it" in flat
    assert '"show me cheaper ones"' in flat
    assert "never sets switch_reply_language" in flat


# ── what a saved session holds ──────────────────────────────────────────────


def test_a_session_that_settled_no_language_is_saved_without_the_field() -> None:
    """So a session written with the switch off is exactly what code without
    the field writes and reads - a rollback can still continue it."""
    import json

    saved = json.loads(_state().model_dump_json())

    assert "reply_language" not in saved
    assert AgentStateV1.model_validate(saved).reply_language is None


def test_a_settled_language_is_saved_and_read_back() -> None:
    import json

    state = _state().model_copy(update={"reply_language": AR})

    saved = json.loads(state.model_dump_json())

    assert saved["reply_language"] == "ar"
    assert AgentStateV1.model_validate(saved).reply_language is AR
