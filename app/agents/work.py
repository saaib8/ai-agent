"""The memory the agents share within one turn.

Every agent reads and writes the same staged copy of the session state, and
every tool's outcome is recorded once, in order. Nothing here is persisted
until the reply is validated; a turn that fails leaves the stored session as
it was (CLAUDE.md 21.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue

from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1


class ToolStatus(StrEnum):
    OK = "ok"
    REFUSED = "refused"
    """Not this agent's to run, out of budget, or a rule said no."""
    INVALID = "invalid"
    """The arguments broke the tool's contract."""


class ToolRecord(BaseModel):
    """One tool call, as the turn remembers it."""

    model_config = ConfigDict(frozen=True)

    agent: AgentPersona
    tool: str
    status: ToolStatus
    code: str | None = None
    facts: dict[str, JsonValue] = {}
    """What the model was told: safe, verified, id-free."""
    payload: BaseModel | None = None
    """The typed outcome the reply and the screen are built from. Never shown
    to a model."""
    acted: bool = False
    """Whether the call changed the session or put something on screen - not
    only reported facts."""


@dataclass(slots=True)
class TurnWork:
    """The staged state and the tool record, for one turn only."""

    pre_turn: AgentStateV1
    staged: AgentStateV1
    records: list[ToolRecord] = field(default_factory=list)
    working: AgentPersona | None = None
    """The agent whose hop is under way - the one who says where the turn
    stands if it stops before that agent has run a tool."""
    views: list[dict[str, Any]] = field(default_factory=list)
    """Every session brief an agent was shown this turn: its figures may be
    cited (`app/agents/figures.py`)."""
    view_marks: list[int] = field(default_factory=list)
    """How many tool calls had run when each brief was shown."""
    said: tuple[str, ...] = ()
    """The customer's own words - this message, a tapped button's, and their
    earlier messages - whose figures a calculation may start from."""

    @classmethod
    def starting(cls, state: AgentStateV1, said: tuple[str, ...] = ()) -> TurnWork:
        return cls(pre_turn=state, staged=state, said=said)

    def show(self, view: dict[str, Any]) -> None:
        if not self.views or self.views[-1] is not view:
            self.views.append(view)
            self.view_marks.append(len(self.records))

    def record(self, record: ToolRecord, state: AgentStateV1 | None = None) -> None:
        self.records.append(record)
        if state is not None:
            self.staged = state

    def by(self, agent: AgentPersona) -> tuple[ToolRecord, ...]:
        return tuple(r for r in self.records if r.agent is agent)
