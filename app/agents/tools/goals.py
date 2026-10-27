"""What the customer is working toward, kept by the agents.

A goal is the agents' to open, revise and close - seat ten people, furnish a
bedroom, find a rug for the sofa. Code only counts its progress from the cart
(the session brief), so what happens next is decided from what is unfinished,
never from a reflex (docs/goal-led-agents-plan.md).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.agents.context import ToolContext
from app.agents.tools.base import ToolArgs, ToolOutcome, ZoryTool
from app.agents.tools.schemas import ToolSchemas
from app.agents.work import ToolStatus, TurnWork
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import MAX_ROOM_WORDS, GoalKind, GoalStatus
from app.schemas.design import MAX_REGULAR_SEATING_COUNT
from app.services.agent_state import set_goal

GOAL_KEEPERS = frozenset({AgentPersona.SARA, AgentPersona.NORA, AgentPersona.SALMAN})


class GoalArgs(ToolArgs):
    action: Literal["open", "revise", "done", "drop"]
    goal: int | None = Field(
        default=None, ge=1, description="The goal's number - for revise, done or drop."
    )
    kind: GoalKind | None = Field(default=None, description="For a new goal.")
    summary: str | None = Field(
        default=None, min_length=1, max_length=160, description="What it is, in a few words."
    )
    seats: int | None = Field(
        default=None, ge=2, le=MAX_REGULAR_SEATING_COUNT, description="Seating: how many."
    )
    for_kind: str | None = Field(
        default=None, max_length=64, description="A piece: its kind (an approved type)."
    )
    room: str | None = Field(default=None, min_length=1, max_length=MAX_ROOM_WORDS)


_STATUS = {"done": GoalStatus.DONE, "drop": GoalStatus.DROPPED}


def update_goal_tool(schemas: ToolSchemas) -> ZoryTool[GoalArgs]:
    async def update(args: GoalArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        taxonomy = context.services.taxonomy
        if args.for_kind is not None and not taxonomy.is_subcategory(args.for_kind):
            return ToolOutcome(status=ToolStatus.REFUSED, code="unknown_kind")
        if (args.action == "open") != (args.goal is None):
            return ToolOutcome(status=ToolStatus.REFUSED, code="open_takes_no_number")
        state, goal = set_goal(
            work.staged,
            goal=args.goal,
            kind=args.kind,
            summary=args.summary,
            seats=args.seats,
            for_kind=args.for_kind,
            room=args.room,
            status=_STATUS.get(args.action),
        )
        return ToolOutcome(
            facts={"goal": goal.goal, "kind": str(goal.kind), "status": str(goal.status)},
            state=state,
        )

    return ZoryTool(
        name="update_goal",
        description=(
            "Keep what the customer is working toward: open a goal (seat 10 people, furnish "
            "a bedroom, a rug for the sofa), revise it, or mark it done or dropped. Every "
            "agent sees open goals and their progress; add_to_cart's for_goal adds a "
            "product to one."
        ),
        args=GoalArgs,
        owners=GOAL_KEEPERS,
        beside_reply=True,
        handler=update,
    )
