"""What one request did, for operators and the eval harness (CLAUDE.md 22).

A mutable holder set per request by the trace middleware. The endpoint runs in
a copied context, so only a shared object - never a rebound variable - carries
the facts back out: how many model calls the turn made, and which action it
took. Logged on every request; returned as debug headers only where the
settings allow it, never in prod.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class RequestTrace:
    model_calls: int = 0
    turn_action: str | None = None


_TRACE: ContextVar[RequestTrace | None] = ContextVar("request_trace", default=None)


def start_request_trace() -> RequestTrace:
    """A fresh trace for the request now starting."""
    trace = RequestTrace()
    _TRACE.set(trace)
    return trace


def count_model_call() -> None:
    trace = _TRACE.get()
    if trace is not None:
        trace.model_calls += 1


def record_turn_action(action: str) -> None:
    trace = _TRACE.get()
    if trace is not None:
        trace.turn_action = action
