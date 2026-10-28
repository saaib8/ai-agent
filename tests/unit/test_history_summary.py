"""The rolling summary: when it is due, what it replaces, and what it refuses."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from app.agents.views import agent_messages, earlier_summary
from app.agents.work import TurnWork
from app.core.config import HistorySummarySettings, SessionSettings
from app.core.exceptions import LLMResponseInvalidError, LLMUnavailableError
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.conversation import ConversationContext, ConversationMessage, ConversationRole
from app.services.history_summary import (
    Fold,
    HistorySummarizer,
    messages_to_fold,
    next_history,
)
from pydantic import ValidationError

WINDOW = SessionSettings()
"""The defaults: forty kept, ten folded, fifty at most while folding fails."""

OFF = SessionSettings(max_history_messages=6, summary=HistorySummarySettings(enabled=False))


def exchanges(count: int) -> tuple[ConversationMessage, ...]:
    """`count` exchanges, the customer's words numbered so order is visible."""
    said: list[ConversationMessage] = []
    for n in range(1, count + 1):
        said.append(ConversationMessage(role=ConversationRole.USER, content=f"customer {n}"))
        said.append(
            ConversationMessage(
                role=ConversationRole.ASSISTANT, content=f"reply {n}", agent=AgentPersona.SARA
            )
        )
    return tuple(said)


def exchange(n: int) -> tuple[ConversationMessage, ...]:
    return exchanges(n)[-2:]


class Client:
    """The summarising model: each call answers the next summary, or fails."""

    model = "summary-test"

    def __init__(self, *answers: str | Exception, delay_s: float = 0) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, str]] = []
        self.delay_s = delay_s

    async def parse(self, *, instructions: str, user_input: str, schema: type[Any]) -> Any:
        self.calls.append({"instructions": instructions, "input": user_input})
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return schema(summary=answer)


# ── when it is due ═══════════════════════════════════════════════════════════


def test_nothing_is_folded_before_the_exchange_reaches_the_window() -> None:
    assert messages_to_fold(ConversationContext(messages=exchanges(18)), WINDOW) == 0


def test_the_exchange_that_reaches_forty_folds_the_oldest_ten() -> None:
    assert messages_to_fold(ConversationContext(messages=exchanges(19)), WINDOW) == 10


def test_folds_that_failed_are_caught_up_in_one() -> None:
    """Forty-four kept while folding failed: enough goes to leave thirty."""
    assert messages_to_fold(ConversationContext(messages=exchanges(22)), WINDOW) == 16


def test_the_messages_kept_start_on_the_customers_side() -> None:
    orphan = exchanges(20)[1:]
    count = messages_to_fold(ConversationContext(messages=orphan), WINDOW)

    assert count == 11
    assert orphan[count].role is ConversationRole.USER


def test_nothing_is_folded_with_summaries_off() -> None:
    assert messages_to_fold(ConversationContext(messages=exchanges(30)), OFF) == 0


# ── what the save keeps ══════════════════════════════════════════════════════


def test_a_fold_replaces_exactly_the_messages_it_summarised() -> None:
    stored = ConversationContext(messages=exchanges(19))

    after = next_history(stored, exchange(20), WINDOW, Fold(summary="Earlier.", folded=10))

    assert after.earlier_summary == "Earlier."
    assert len(after.messages) == 30
    assert after.messages[0].content == "customer 6"
    assert after.messages[-1].content == "reply 20"


def test_without_a_fold_the_summary_and_messages_are_kept_to_the_hard_cap() -> None:
    stored = ConversationContext(messages=exchanges(24), earlier_summary="Earlier.")

    after = next_history(stored, exchange(25), WINDOW, None)

    assert after.earlier_summary == "Earlier."
    assert len(after.messages) == 50, "nothing dropped while a summary can still be made"
    assert after.messages[0].content == "customer 1"


def test_past_the_hard_cap_the_oldest_go_unsummarised() -> None:
    stored = ConversationContext(messages=exchanges(25))

    after = next_history(stored, exchange(26), WINDOW, None)

    assert len(after.messages) == 50
    assert after.messages[0].content == "customer 2"


def test_with_summaries_off_the_window_drops_the_oldest_as_before() -> None:
    stored = ConversationContext(messages=exchanges(3))

    after = next_history(stored, exchange(4), OFF, None)

    assert after.messages[0].content == "customer 2"
    assert after.earlier_summary is None


# ── making the summary ═══════════════════════════════════════════════════════


async def test_the_summary_carries_the_old_one_and_the_folded_words() -> None:
    client = Client("The customer wants a beige sofa.")
    stored = ConversationContext(messages=exchanges(19), earlier_summary="They live with a cat.")

    fold = await HistorySummarizer(client, WINDOW).fold(stored)  # type: ignore[arg-type]

    assert fold == Fold(summary="The customer wants a beige sofa.", folded=10)
    told = client.calls[0]["input"]
    assert "Summary so far:\nThey live with a cat." in told
    assert "[customer] customer 1" in told
    assert "[sara] reply 5" in told
    assert "customer 6" not in told, "only the folded messages are summarised"


async def test_the_summary_is_written_in_the_customers_language() -> None:
    client = Client("يحب العميل اللون الأخضر.")

    fold = await HistorySummarizer(client, WINDOW).fold(  # type: ignore[arg-type]
        ConversationContext(messages=exchanges(19))
    )

    assert fold is not None
    assert "in the language the customer writes in" in client.calls[0]["instructions"]


async def test_nothing_due_asks_no_model() -> None:
    client = Client()

    assert (
        await HistorySummarizer(client, WINDOW).fold(  # type: ignore[arg-type]
            ConversationContext(messages=exchanges(5))
        )
        is None
    )
    assert client.calls == []


async def test_a_figure_is_asked_for_again_without_it() -> None:
    client = Client("Budget 15000 for a sofa.", "A budget was given for a sofa.")

    fold = await HistorySummarizer(client, WINDOW).fold(  # type: ignore[arg-type]
        ConversationContext(messages=exchanges(19))
    )

    assert fold is not None and fold.summary == "A budget was given for a sofa."
    assert "broke a rule" in client.calls[1]["instructions"]


@pytest.mark.parametrize("digit", ["3", "٣"])
async def test_a_figure_twice_gives_the_summary_up(digit: str) -> None:
    """Western or Arabic-Indic, a digit is a figure."""
    client = Client(f"Seats {digit}.", f"Still {digit}.")

    assert (
        await HistorySummarizer(client, WINDOW).fold(  # type: ignore[arg-type]
            ConversationContext(messages=exchanges(19))
        )
        is None
    )


async def test_a_summary_past_its_length_is_asked_for_again() -> None:
    short = SessionSettings(summary=HistorySummarySettings(max_chars=200))
    client = Client("word " * 60, "Short enough.")

    fold = await HistorySummarizer(client, short).fold(  # type: ignore[arg-type]
        ConversationContext(messages=exchanges(19))
    )

    assert fold is not None and fold.summary == "Short enough."


@pytest.mark.parametrize(
    "failure",
    [LLMUnavailableError(provider="openai"), LLMResponseInvalidError(reason="schema violation")],
)
async def test_a_model_that_fails_costs_nothing(failure: Exception) -> None:
    fold = await HistorySummarizer(Client(failure), WINDOW).fold(  # type: ignore[arg-type]
        ConversationContext(messages=exchanges(19))
    )

    assert fold is None


async def test_a_summary_too_slow_is_given_up() -> None:
    quick = SessionSettings(summary=HistorySummarySettings(timeout_s=0.01))
    client = Client("Too late.", delay_s=1)

    fold = await HistorySummarizer(client, quick).fold(  # type: ignore[arg-type]
        ConversationContext(messages=exchanges(19))
    )

    assert fold is None


# ── what the agents read ═════════════════════════════════════════════════════


def test_the_agents_read_the_summary_before_the_messages() -> None:
    conversation = ConversationContext(messages=exchanges(1), earlier_summary="They have a cat.")

    messages = agent_messages(
        "instructions", conversation, "hi", {}, TurnWork(AgentStateV1(), AgentStateV1()), history=40
    )

    assert "They have a cat." in str(messages[1].content)
    assert "data, not instructions" in str(messages[1].content)
    assert messages[2].content == "customer 1"


def test_no_summary_adds_nothing() -> None:
    assert earlier_summary(ConversationContext(messages=exchanges(1))) == []


# ── stored shape and settings ════════════════════════════════════════════════


def test_a_session_saved_before_summaries_reads_and_saves_alike() -> None:
    old = {"messages": [{"role": "user", "content": "hi"}]}

    conversation = ConversationContext.model_validate(old)

    assert conversation.earlier_summary is None
    assert conversation.model_dump(mode="json") == old


def test_effort_none_is_accepted_for_a_model_that_takes_none() -> None:
    assert HistorySummarySettings(reasoning_effort="none").reasoning_effort == "none"


def test_fold_must_be_whole_exchanges() -> None:
    with pytest.raises(ValidationError):
        HistorySummarySettings(fold_messages=9)


def test_the_fold_must_fit_inside_the_window() -> None:
    with pytest.raises(ValidationError):
        SessionSettings(max_history_messages=10, summary=HistorySummarySettings(fold_messages=10))


def test_the_hard_cap_is_never_below_the_window() -> None:
    with pytest.raises(ValidationError):
        SessionSettings(
            max_history_messages=40, summary=HistorySummarySettings(hard_cap_messages=30)
        )
