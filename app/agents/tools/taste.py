"""Remembering what a customer said about their taste.

Said taste is the customer's own: the colours and styles they like and those
they would rather avoid, in approved values only. It orders every later search
and room and filters none (CLAUDE.md 12.4), and every agent reads it back in
the session brief.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import Field, create_model

from app.agents.context import ToolContext
from app.agents.tools.base import ToolArgs, ToolOutcome, ZoryTool, list_of, one_of
from app.agents.tools.schemas import ToolSchemas
from app.agents.work import TurnWork
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import MAX_ROOM_WORDS
from app.services.agent_state import remember_taste

TASTE_KEEPERS = frozenset({AgentPersona.SARA, AgentPersona.NORA, AgentPersona.SALMAN})


def taste_args(schemas: ToolSchemas) -> type[ToolArgs]:
    colours = tuple(sorted(schemas.attributes.colors))
    styles = tuple(sorted(schemas.attributes.styles))
    colour, style = one_of(colours), one_of(styles)
    fields: dict[str, Any] = {
        "colours": (list_of(colour), Field(default_factory=list, description="Colours they like.")),
        "styles": (list_of(style), Field(default_factory=list, description="Styles they like.")),
        "avoid_colours": (
            list_of(colour),
            Field(default_factory=list, description="Colours they would rather not have."),
        ),
        "avoid_styles": (
            list_of(style),
            Field(default_factory=list, description="Styles they would rather not have."),
        ),
        "forget": (
            list_of(one_of((*colours, *styles))),
            Field(default_factory=list, description="Values they no longer like or mind."),
        ),
        "shopping_for_room": (
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=MAX_ROOM_WORDS,
                description="The room, in a few words.",
            ),
        ),
    }
    return cast(type[ToolArgs], create_model("RememberTasteArgs", __base__=ToolArgs, **fields))


def remember_taste_tool(schemas: ToolSchemas) -> ZoryTool[Any]:
    async def remember(args: Any, context: ToolContext, work: TurnWork) -> ToolOutcome:
        state = remember_taste(
            work.staged,
            colours=args.colours,
            styles=args.styles,
            avoid_colours=args.avoid_colours,
            avoid_styles=args.avoid_styles,
            forget=args.forget,
            room=args.shopping_for_room,
        )
        said = state.customer_preferences
        return ToolOutcome(
            facts={
                "likes": [p.canonical_value or p.raw_value for p in said.semantic_preferences],
                "avoids": [*said.avoid_colours, *said.avoid_styles],
            },
            state=state,
        )

    return ZoryTool(
        name="remember_taste",
        description=(
            "Remember the colours and styles the customer said they like or would rather "
            "avoid, or the room they are shopping for. Their likes lean every new product "
            "search; what they avoid is pushed down in every search, this one's next page "
            "included, and what goes with a pick. Nothing is hidden. A room does not read "
            "it: pass their colours and styles to design_room yourself."
        ),
        args=taste_args(schemas),
        owners=TASTE_KEEPERS,
        handler=remember,
    )
