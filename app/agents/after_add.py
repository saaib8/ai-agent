"""What follows an add to the cart: an ordered list of rules on the session.

An add is a fixed event (`added_to_cart`, or the client's "I've added X to my
cart"), so who handles it is decided here, from facts in the session, never
guessed by a model (CLAUDE.md 10.3, 17.3). The first rule that matches decides;
none matching is the usual path - Nora plans what goes with it, Salman shows it.
A new case is one more rule here, with its own test.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1, GoalKind, GoalStatus
from app.schemas.product import ProductCandidate
from app.services.goal_progress import GoalProgress, goal_progress
from app.taxonomy.seating import SeatingSemantics


class AfterAdd(StrEnum):
    CONTINUE_GOAL = "continue_goal"
    """A goal the add belongs to is not met yet: its next piece comes first."""
    PLAN_THEN_SHOW = "plan_then_show"
    """Nora plans what goes with it, then Salman shows it."""


@dataclass(frozen=True, slots=True)
class AddDecision:
    path: AfterAdd
    first: AgentPersona
    rule: str
    """Which rule decided - for the trace."""


@dataclass(frozen=True, slots=True)
class AddContext:
    state: AgentStateV1
    added: ProductCandidate | None
    progress: tuple[GoalProgress, ...]


Rule = Callable[[AddContext], AddDecision | None]


def _unmet_goal(context: AddContext) -> AddDecision | None:
    """Two beds, ten seats, four dining chairs: while what they set out to
    choose is not all in the cart, Sara helps with the next piece."""
    if any(progress.unmet for progress in context.progress):
        return AddDecision(AfterAdd.CONTINUE_GOAL, AgentPersona.SARA, "unmet_goal")
    return None


RULES: tuple[Rule, ...] = (_unmet_goal,)


def decide(context: AddContext) -> AddDecision:
    for rule in RULES:
        decision = rule(context)
        if decision is not None:
            return decision
    return AddDecision(AfterAdd.PLAN_THEN_SHOW, AgentPersona.NORA, "default")


def progress_of(
    state: AgentStateV1, by_id: Mapping[int, ProductCandidate]
) -> tuple[GoalProgress, ...]:
    return tuple(goal_progress(goal, state, by_id) for goal in state.goals)


def goal_for(
    state: AgentStateV1, added: ProductCandidate, seating: SeatingSemantics | None
) -> int | None:
    """The open goal an added product counts toward: the newest seating goal
    for a piece that counts as seats (reviewed, `seating_v1.yaml`), or the
    newest piece goal for its own kind. A room goal is the room engine's,
    never joined here."""
    kind = added.commerce.subcategory
    for goal in reversed(state.goals):
        if goal.status is not GoalStatus.OPEN or added.product_id in goal.members:
            continue
        counts = seating is not None and (seating.seats_one(kind) or seating.seats_several(kind))
        if goal.kind is GoalKind.SEATING and counts:
            return goal.goal
        if goal.kind is GoalKind.PIECE and goal.for_kind == added.commerce.subcategory:
            return goal.goal
    return None
