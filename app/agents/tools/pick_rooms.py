"""Which room a cart product is for, when the customer says so.

A sofa may be for the living room or the bedroom; its kind cannot say which.
So a room is recorded only from a fact (CLAUDE.md 36): the customer named it,
answered which room it is for, or it was added within a room's work - the last
two are recorded by code. Any agent that hears it records it here.
"""

from __future__ import annotations

from pydantic import Field

from app.agents.context import ToolContext
from app.agents.tools.base import ToolOutcome, ZoryTool
from app.agents.tools.shopping import ProductRef, resolve_product
from app.agents.work import ToolStatus, TurnWork
from app.schemas.agent_persona import AgentPersona
from app.services.agent_state import room_key, tag_pick_rooms

ROOM_TAGGERS = frozenset({AgentPersona.SARA, AgentPersona.NORA, AgentPersona.SALMAN})


class PickRoomArgs(ProductRef):
    room: str = Field(
        min_length=1,
        max_length=40,
        description=(
            "The room it is for, in their words - 'bedroom', 'living room', 'majlis'. "
            "Only what they said, never a guess from the kind."
        ),
    )


def pick_room_tool() -> ZoryTool[PickRoomArgs]:
    async def tag(args: PickRoomArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        product = await resolve_product(context.services, args.which, work, context)
        if isinstance(product, ToolOutcome):
            return product
        if product not in work.staged.product_interaction.selected_product_ids:
            return ToolOutcome(status=ToolStatus.REFUSED, code="not_in_cart")
        if not _said_by_them(args.room, work.said):
            # A room is theirs to say: one the agent concluded is a guess, and
            # a guess is exactly what would pull a piece into the wrong room.
            return ToolOutcome(
                status=ToolStatus.REFUSED,
                code="room_not_said",
                facts={"ask": "ask them which room it is for; record only the room they name"},
            )
        return ToolOutcome(
            facts={"room": room_key(args.room)},
            state=tag_pick_rooms(work.staged, (product,), args.room),
        )

    return ZoryTool(
        name="set_pick_room",
        description=(
            "Record which room a product in their cart is for, when they say it or answer "
            "which room it is for. Each cart line in the context shows its room once known."
        ),
        args=PickRoomArgs,
        owners=ROOM_TAGGERS,
        handler=tag,
        beside_reply=True,
    )


def _said_by_them(room: str, said: tuple[str, ...]) -> bool:
    """The room is in the customer's own words - typed or tapped - not only the
    agent's conclusion."""
    return room_key(room) in room_key(" ".join(said))
