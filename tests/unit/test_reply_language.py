"""Which language a session is answered in (docs/arabic-replies-plan.md).

Three layers: the pure rule, the coordinator that applies it to a turn, and the
HTTP exchange that carries it across requests. Off, every layer must leave the
turn exactly as it was before Arabic replies existed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.schemas.agent_state import AgentStateV1
from app.schemas.language import LanguageSource, ReplyLanguage
from app.services.reply_language import (
    TurnLanguage,
    after_the_decision,
    before_the_turn,
    writes_arabic_script,
)

from tests.conftest import build_settings
from tests.unit.agents.fakes import _state

AR, EN = ReplyLanguage.AR, ReplyLanguage.EN
ARABIC = "أبي كنبة رمادية"
ENGLISH = "show me grey sofas"


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


# ── the coordinator, switched on ────────────────────────────────────────────


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
