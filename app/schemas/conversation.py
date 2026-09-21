"""Provider-neutral conversation history.

Deliberately the smallest thing that can carry a conversation: a role and some
words. No message ids, no timestamps, no provider objects, no tool-call
records, no summaries. Each of those would be a field the agent layer could
start depending on before the session layer that owns them exists.

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


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ConversationMessage(BaseModel):
    """One prior turn, as text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ConversationRole
    content: str = Field(min_length=1)


class ConversationContext(BaseModel):
    """The prior messages a caller chose to supply.

    Caller-selected and not necessarily complete: how many turns are kept, and
    which, is the session layer's decision. Nothing here persists anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[ConversationMessage, ...] = ()
