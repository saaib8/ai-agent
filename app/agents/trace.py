"""What one agent turn did, for the logs: never the customer's words, never
an argument's value, never a secret (CLAUDE.md 22)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from app.core.logging import get_logger
from app.prompts.agents.v2 import VERSION as PROMPT_VERSION

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TraceEvent:
    role: str
    kind: Literal["model", "tool", "handoff"]
    name: str
    ms: int
    status: str | None = None
    code: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(slots=True)
class TurnTrace:
    store_id: int
    path: Literal["typed", "tap"]
    started: float = field(default_factory=time.perf_counter)
    events: list[TraceEvent] = field(default_factory=list)
    listener: Callable[[TraceEvent], None] | None = None
    """Told of each event as it happens - how live progress reaches a client."""

    def add(self, event: TraceEvent) -> None:
        self.events.append(event)
        if self.listener is not None:
            self.listener(event)

    def finish(self, *, owner: str | None, fallback: str | None = None) -> None:
        logger.info(
            "agent_turn_completed",
            store_id=self.store_id,
            path=self.path,
            prompt_version=PROMPT_VERSION,
            owner=owner,
            fallback=fallback,
            ms=int((time.perf_counter() - self.started) * 1000),
            model_calls=sum(1 for e in self.events if e.kind == "model"),
            tool_calls=sum(1 for e in self.events if e.kind == "tool"),
            events=[{k: v for k, v in vars_of(e).items() if v is not None} for e in self.events],
        )


def vars_of(event: TraceEvent) -> dict[str, object]:
    return {name: getattr(event, name) for name in event.__slots__}


def elapsed_ms(since: float) -> int:
    return int((time.perf_counter() - since) * 1000)
