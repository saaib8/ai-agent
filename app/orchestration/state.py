"""What one exchange carries between graph nodes.

Deliberately a record of *execution*, not of reasoning. Each field is something
a phase produced and a later phase needs, so the graph can be read as a list of
what is known at each point.

What is absent is the design. There is no hidden scratchpad, no plan the model
wrote for itself, no provider object and no database row: the reasoning already
happened inside services that were built and tested without a graph, and a node
that started stashing intermediate thoughts here would be the beginning of a
third reasoning layer (CLAUDE.md 18).

`total=False` because the state is filled in as it goes. A node returns only
the keys it establishes, and LangGraph merges them - so a field being present
is itself the fact that its phase ran.
"""

from __future__ import annotations

from typing import TypedDict

from app.schemas.agent_turn import (
    CustomerResponse,
    CustomerTurnInput,
    CustomerTurnResult,
)
from app.schemas.chat import ChatPresentation, ChatRequest, ChatResponse
from app.schemas.retailer import RetailerContext
from app.services.chat_runtime import LoadedSession


class ChatGraphState(TypedDict, total=False):
    """One exchange, accumulating.

    Never handed to a model. The decision model gets `DecisionInput`, the
    response model gets `ResponseInput` and the design specialist gets
    `InteriorDesignRequest`; each is built by the service that calls it, from
    the narrow slice it is allowed to see (M13 44).
    """

    request: ChatRequest
    context: RetailerContext

    session: LoadedSession
    """The stored conversation and the revision it was read at.

    The revision is kept here rather than re-read at save time: persistence
    must compare against what *this* exchange loaded, not against whatever the
    session has become since.
    """

    turn: CustomerTurnInput
    result: CustomerTurnResult
    response: CustomerResponse
    presentation: ChatPresentation | None
    revision: int
    reply: ChatResponse
