"""Every tool the agents may call, assembled once into the registry."""

from __future__ import annotations

from app.agents.tools.calculate import calculate_tool
from app.agents.tools.common import HAND_BACK, RESPOND
from app.agents.tools.design import design_tools
from app.agents.tools.goals import update_goal_tool
from app.agents.tools.registry import ToolRegistry
from app.agents.tools.room_notes import note_room_tool
from app.agents.tools.sales import sales_tools
from app.agents.tools.schemas import ToolSchemas
from app.agents.tools.shopping import shopping_tools
from app.agents.tools.support import support_tools
from app.agents.tools.taste import remember_taste_tool


def build_registry(schemas: ToolSchemas) -> ToolRegistry:
    return ToolRegistry(
        [
            *shopping_tools(schemas),
            *design_tools(schemas),
            *sales_tools(schemas),
            *support_tools(schemas),
            remember_taste_tool(schemas),
            update_goal_tool(schemas),
            note_room_tool(schemas),
            calculate_tool(),
            RESPOND,
            HAND_BACK,
        ]
    )
