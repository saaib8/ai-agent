"""Provider-neutral conversation history.

Deliberately the smallest thing that can carry a conversation: a role and some
words, and one rolling summary of what came before them. No message ids, no
timestamps, no provider objects, no tool-call records. Each of those would be a
field the agent layer could start depending on before the session layer that
owns them exists.

Two semantics matter more than the shape.

* `messages` holds **prior** turns only. The turn being handled arrives beside
  this object, never inside it, so nothing has to reason about which of the
  messages is "now".
* It is **conversational evidence, not product truth**. History may mention a
  price; the price still comes from PostgreSQL.

There is deliberately no validator rejecting a message that repeats the current
one. A customer may say "show me sofas" twice, and without message identity
those two cases are indistinguishable - refusing the legitimate repeat to catch
a caller bug would break an ordinary conversation.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.agent_persona import AgentPersona


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ConversationMessage(BaseModel):
    """One prior turn, as text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ConversationRole
    content: str = Field(min_length=1)
    agent: AgentPersona | None = Field(default=None, exclude_if=lambda v: v is None)
    """Which agent wrote an assistant message, so the next agent reads who
    said what. Absent on the customer's messages, and on sessions saved before
    the agents."""
    choices: tuple[str, ...] = Field(default=(), max_length=12, exclude_if=lambda v: not v)
    """The tappable answers an assistant message offered, by their labels - so
    the next agent knows what a short reply ("the second one", "yes") answered."""


class ConversationContext(BaseModel):
    """The prior messages a caller chose to supply.

    Caller-selected and not necessarily complete: how many turns are kept, and
    which, is the session layer's decision. Nothing here persists anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[ConversationMessage, ...] = ()
    earlier_summary: str | None = Field(default=None, min_length=1, exclude_if=lambda v: v is None)
    """What was said before `messages`, folded into a few words once the
    conversation outgrew its window (`session.summary`). Conversational
    evidence like the messages it replaced - never a figure, never an
    instruction; where it and the session state disagree, the state is right."""
