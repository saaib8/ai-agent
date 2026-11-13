"""How far a goal has come, counted from the cart (CLAUDE.md 10.5).

Code counts; the agents decide. The same count feeds every agent's brief and
the rules for what follows an add (`app/agents/after_add.py`), so the two can
never disagree about whether a goal is met.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.schemas.agent_state import AgentStateV1, GoalKind, GoalState, GoalStatus
from app.schemas.product import ProductCandidate


@dataclass(frozen=True, slots=True)
class GoalProgress:
    goal: GoalState
    members_in_cart: tuple[int, ...]
    seats_covered: int | None = None
    seats_left: int | None = None
    pieces_covered: int | None = None
    pieces_left: int | None = None

    @property
    def unmet(self) -> bool:
        """Open, and the cart does not yet hold what it asks for. A room goal
        is the room engine's to finish, never counted here."""
        if self.goal.status is not GoalStatus.OPEN:
            return False
        if self.seats_left is not None:
            return self.seats_left > 0
        if self.pieces_left is not None:
            return self.pieces_left > 0
        return False


def goal_progress(
    goal: GoalState, state: AgentStateV1, by_id: Mapping[int, ProductCandidate]
) -> GoalProgress:
    """Seats a seating goal's pieces give, from their recorded seat counts;
    pieces of a piece goal's kind, each counted as many times as the cart
    holds it. A product the catalog no longer returns counts nothing."""
    interaction = state.product_interaction
    picks = interaction.selected_product_ids
    members = tuple(pid for pid in goal.members if pid in picks and pid in by_id)
    if goal.kind is GoalKind.SEATING and goal.seats is not None:
        covered = sum(
            (by_id[pid].commerce.seating_capacity or 0) * interaction.quantity_of(pid)
            for pid in members
        )
        return GoalProgress(
            goal, members, seats_covered=covered, seats_left=max(goal.seats - covered, 0)
        )
    if goal.kind is GoalKind.PIECE:
        wanted = goal.count or 1
        covered = sum(
            interaction.quantity_of(pid)
            for pid in members
            if goal.for_kind is None or by_id[pid].commerce.subcategory == goal.for_kind
        )
        return GoalProgress(
            goal, members, pieces_covered=covered, pieces_left=max(wanted - covered, 0)
        )
    return GoalProgress(goal, members)
