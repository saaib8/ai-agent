"""Checking an agent's reply before the customer reads it.

An agent writes the words; code checks every figure in them. A number is
allowed only when a tool returned it or the customer said it - so a price, a
size or a total an agent made up never reaches the customer. Agents write
figures as references code fills in (`app/agents/figures.py`), so this is the
safety net: one broken reply is sent back once with the rule it broke; a second
loses only the sentences that carry such a figure, and ends on the fixed reply
only when nothing is left.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from app.agents.figures import keep_sentences
from app.agents.tools.common import RespondArgs
from app.agents.work import TurnWork
from app.schemas.agent_turn import CustomerResponse
from app.schemas.conversation import ConversationContext, ConversationRole
from app.schemas.text_choice import asks_a_question
from app.services.numeric_guard import (
    NumericPolicyViolation,
    build_allowance,
    check_numeric_policy,
    numbers_in,
)
from app.services.written_numbers import written_figures

FALLBACK_REPLY = {
    "en": "Sorry, I didn't quite catch that - could you say it another way?",
    "ar": "عذرًا، لم أفهم ذلك تمامًا - هل يمكنك قولها بطريقة أخرى؟",
}


def figures_in(value: Any, *, written: bool = False) -> Iterable[Decimal]:
    """Every figure in a tool's facts, however deep. `written` also reads the
    figures a text writes in words - for the store's own documents only, so a
    product named "Two Seater Sofa" does not license a "2" no fact gave."""
    if isinstance(value, bool):
        return
    if isinstance(value, int | float | Decimal):
        yield Decimal(str(value))
    elif isinstance(value, str):
        # Every figure the text holds - a product name's "2.25m", a policy's
        # "4 to 7 days" - not only a string that is a number.
        for token in numbers_in(value):
            try:
                yield Decimal(token)
            except InvalidOperation:
                continue
        if written:
            # "fourteen days", "10k" in a store's policy: said in digits.
            yield from written_figures(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from figures_in(item, written=written)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from figures_in(item, written=written)


_DOCUMENTS = frozenset({"store_policy"})
"""The tools whose facts are the store's own documents."""


def unsupported_figure(
    reply: RespondArgs,
    work: TurnWork,
    message: str,
    conversation: ConversationContext,
    views: Sequence[dict[str, Any]],
    *,
    also_said: Sequence[str] = (),
) -> NumericPolicyViolation | None:
    """The first figure in the reply no tool, view or customer gave, or None.

    `views` are every view an agent was shown this turn; `also_said` the
    words a tapped button stands for, which are the customer's too.
    """
    allowance = _allowance(work, message, conversation, views, also_said)
    question = reply.follow_up_question
    checked = check_numeric_policy(
        message=reply.message, follow_up_question=question, allowance=allowance
    )
    if checked is not None:
        return checked
    for choice in reply.choices:
        violation = check_numeric_policy(
            message=choice.label, follow_up_question=choice.value, allowance=allowance
        )
        if violation is not None:
            return violation
    return None


def repaired(
    reply: RespondArgs,
    work: TurnWork,
    message: str,
    conversation: ConversationContext,
    views: Sequence[dict[str, Any]],
    *,
    also_said: Sequence[str] = (),
) -> RespondArgs | None:
    """The reply without the sentences whose figures nobody gave - a question
    with one goes, with the choices that answered it; a choice with one goes
    alone. None when nothing worth sending is left. The rest of what the agent
    wrote stands: one stray figure never costs the customer the whole reply."""
    allowance = _allowance(work, message, conversation, views, also_said)

    def clean(text: str) -> bool:
        return numbers_in(text) <= allowance

    kept = keep_sentences(reply.message, clean)
    if not kept:
        return None
    question = reply.follow_up_question
    if question is not None and not clean(question):
        question = None
    choices = tuple(c for c in reply.choices if clean(c.label) and clean(c.value))
    try:
        return RespondArgs(
            message=kept,
            follow_up_question=question,
            choices=choices,
            language=reply.language,
            show=reply.show,
        )
    except ValidationError:
        # The question that the choices answered went: they go with it.
        return RespondArgs(
            message=kept, follow_up_question=question, language=reply.language, show=reply.show
        )


def _allowance(
    work: TurnWork,
    message: str,
    conversation: ConversationContext,
    views: Sequence[dict[str, Any]],
    also_said: Sequence[str],
) -> frozenset[str]:
    figures = [
        *(f for view in views for f in figures_in(view)),
        *(f for r in work.records for f in figures_in(r.facts, written=r.tool in _DOCUMENTS)),
    ]
    return build_allowance(
        message,
        said_earlier=[
            *also_said,
            *(m.content for m in conversation.messages if m.role is ConversationRole.USER),
        ],
        presented_count=max((len(v.get("cards_on_screen", ())) for v in views), default=0),
        figures=figures,
    )


def fallback(language: str | None) -> RespondArgs:
    return RespondArgs(message=FALLBACK_REPLY.get(language or "en", FALLBACK_REPLY["en"]))


def customer_response(reply: RespondArgs) -> CustomerResponse:
    """The reply as the client receives it. Chips that answer no question are
    dropped rather than shown as a dead end, and a reply with a follow-up asks
    it once: the client shows the follow-up apart, so a question the message
    also asks - the same one in other words - is left out of the message. Only
    when the follow-up is itself a question: worded as a request ("Choose a
    colour"), the message's question is the one the choices answer, so it stays."""
    message = reply.message
    if asks_a_question(reply.follow_up_question):
        message = keep_sentences(message, lambda s: not asks_a_question(s)) or message
    try:
        return CustomerResponse(
            message=message,
            follow_up_question=reply.follow_up_question,
            choices=reply.choices,
        )
    except ValidationError:
        return CustomerResponse(message=message, follow_up_question=reply.follow_up_question)

