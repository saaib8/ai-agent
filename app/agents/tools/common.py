"""The two calls that end an agent's hop, shared by every agent."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.agents.tools.base import ToolArgs, ToolKind, ZoryTool
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_turn import MAX_RESPONSE_CHARS
from app.schemas.text_choice import TextReplyChoice, require_a_question

EVERY_AGENT = frozenset(AgentPersona)


class RespondArgs(ToolArgs):
    """The reply the customer reads, written by the agent that did the work."""

    message: str = Field(min_length=1, max_length=MAX_RESPONSE_CHARS)
    follow_up_question: str | None = Field(default=None, max_length=300)
    """At most one question, and only when one helps them decide."""
    choices: tuple[TextReplyChoice, ...] = Field(default=(), max_length=6)
    """Short answers to the reply's question the customer can tap. Only with a
    question - the follow-up, or one the message asks."""

    language: Literal["en", "ar"] | None = Field(
        default=None,
        description=(
            "The language your reply is written in - the customer's own. Their cards and "
            "buttons follow it."
        ),
    )
    show: (
        Literal["list", "product", "comparison", "room", "seating", "cart", "liked", "unchanged"]
        | None
    ) = Field(
        default=None,
        description=(
            "What the customer's screen shows beside your reply, from what your tools "
            "produced this turn. Omit for the last thing a tool showed - except a room "
            "design_room built, which is shown only with 'room', once you have checked it. "
            "'unchanged' keeps the screen as it was - when no result this turn is right to "
            "show."
        ),
    )

    @model_validator(mode="after")
    def _choices_answer_a_question(self) -> RespondArgs:
        # Told at once, as broken arguments, so the agent corrects its reply -
        # never chips dropped later without a word.
        require_a_question(self.choices, self.message, self.follow_up_question)
        return self


class HandBackReason(StrEnum):
    """Why an agent returns the turn: a typed reason, never a free-form note
    to another agent (CLAUDE.md 3.5)."""

    DONE = "done"
    NEEDS_SHOPPING = "needs_shopping"
    NEEDS_DESIGN = "needs_design"
    NEEDS_SALES = "needs_sales"
    NEEDS_SUPPORT = "needs_support"
    OUT_OF_SCOPE = "out_of_scope"


class HandBackArgs(ToolArgs):
    reason: HandBackReason
    to: AgentPersona | None = Field(
        default=None,
        description=(
            "The colleague who should take it next, when you know - the turn goes "
            "straight to them, with what you did. Leave it empty for the supervisor "
            "to decide."
        ),
    )


RESPOND = ZoryTool(
    name="respond",
    description=(
        "Send your reply to the customer and end your part of this turn. Call it "
        "once, after the tools you needed. State only facts the tools returned."
    ),
    args=RespondArgs,
    owners=EVERY_AGENT,
    kind=ToolKind.RESPOND,
)

HAND_BACK = ZoryTool(
    name="hand_back",
    description=(
        "Pass the turn on without replying, when part of what the customer asked "
        "belongs to another agent. Say which kind of help is still needed, and "
        "name the colleague in `to` when you know who it is."
    ),
    args=HandBackArgs,
    owners=EVERY_AGENT,
    kind=ToolKind.HAND_BACK,
)
