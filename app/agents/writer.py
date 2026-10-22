"""Replies to what happens outside a chat turn, written by an agent.

A photo searched or a room rendered is answered in the agent's own words from
the facts of what happened - checked like any reply - and in fixed words only
when no model can give a usable one (CLAUDE.md 21.1).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from app.agents.context import ToolContext
from app.agents.reply import customer_response
from app.agents.services import AgentServices
from app.agents.team import AgentTeam
from app.agents.trace import TurnTrace
from app.core.exceptions import ZoryError
from app.core.logging import get_logger
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import CustomerResponse
from app.schemas.conversation import ConversationContext
from app.schemas.language import ReplyLanguage
from app.schemas.retailer import RetailerContext

logger = get_logger(__name__)


class ReplyWriter:
    def __init__(self, team: AgentTeam, services: AgentServices, *, deadline_s: float) -> None:
        self._team = team
        self._services = services
        self._deadline_s = deadline_s

    async def write(
        self,
        *,
        happened: str,
        facts: Mapping[str, Any],
        state: AgentStateV1,
        conversation: ConversationContext,
        retailer: RetailerContext,
        language: ReplyLanguage,
        fixed: CustomerResponse,
        agent: AgentPersona = AgentPersona.SARA,
    ) -> CustomerResponse:
        """The agent's reply, or `fixed` when it could not give one in time."""
        context = ToolContext(retailer=retailer, services=self._services, language=language)
        trace = TurnTrace(store_id=retailer.store_id, path="tap")
        try:
            reply = await asyncio.wait_for(
                self._team.word(agent, happened, facts, state, conversation, context, trace),
                timeout=self._deadline_s,
            )
        except (TimeoutError, ZoryError) as exc:
            # Any failure to word it - an outage, a refused request - keeps the
            # fixed words: what happened has already happened and is paid for.
            logger.warning("agent_writer_unavailable", error_type=type(exc).__name__)
            reply = None
        trace.finish(owner=agent.value if reply else None, fallback=None if reply else "fixed")
        return customer_response(reply) if reply is not None else fixed
