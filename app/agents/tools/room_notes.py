"""What the customer says about their room, kept by whichever agent hears it.

The room's size, its walls and what is on them - a door, a window, the TV
point, a wall to keep free - are the customer's facts. Any agent records them
when they are said, so nothing is asked twice and every fit, placement and
size judgement after it starts from the room as it is
(docs/goal-led-agents-plan.md).
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import Field, create_model

from app.agents.context import ToolContext
from app.agents.tools.base import ToolArgs, ToolOutcome, ZoryTool, list_of, one_of
from app.agents.tools.design import Budget, Measure
from app.agents.tools.schemas import ToolSchemas
from app.agents.work import ToolStatus, TurnWork
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import RoomProjectState
from app.schemas.agent_updates import AgentStateUpdate, ReplaceItems, RoomProjectUpdate
from app.schemas.geometry import (
    MAX_WALL_NOTES,
    RoomGeometry,
    RoomMeasurement,
    RoomMeasurementRole,
    WallFeature,
    WallNote,
)
from app.schemas.query import ConstraintStrength, SemanticPreference
from app.schemas.room_form import MAX_COLOURS_CHOSEN, MAX_STYLES_CHOSEN
from app.services.agent_state import apply_update
from app.services.fit import knows_room_size
from app.services.room_cart import room_words
from app.taxonomy.attributes import AttributeFamily

ROOM_KEEPERS = frozenset({AgentPersona.SARA, AgentPersona.NORA, AgentPersona.SALMAN})


class WallArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=40, description="In their words: 'the window wall'.")
    length: Measure | None = None
    has: list[WallFeature] = Field(default_factory=list, max_length=6)
    note: str | None = Field(default=None, max_length=120)


def room_note_args(schemas: ToolSchemas) -> type[ToolArgs]:
    rooms = schemas.room_pieces
    kinds = tuple(rooms.kinds) if rooms is not None else ()
    colour = one_of(tuple(sorted(schemas.attributes.colors)))
    style = one_of(tuple(sorted(schemas.attributes.styles)))
    fields: dict[str, Any] = {
        "room_type": (str | None, Field(default=None, min_length=1, max_length=60)),
        "room_kind": (one_of(kinds) | None, None) if kinds else (str | None, None),
        "length": (Measure | None, None),
        "width": (Measure | None, None),
        "doorway": (Measure | None, Field(default=None, description="The way in, its width.")),
        "walls": (list[WallArgs], Field(default_factory=list, max_length=MAX_WALL_NOTES)),
        "budget": (
            Budget | None,
            Field(
                default=None,
                description="The room's budget, as soon as they say it - kept before anything "
                "is built, so it is never asked again.",
            ),
        ),
        "colours": (
            list_of(colour),
            Field(
                default_factory=list,
                max_length=MAX_COLOURS_CHOSEN,
                description="The colours they said they want for this room ('a blue bedroom'), "
                "as soon as they say them - replacing the colours said before.",
            ),
        ),
        "styles": (
            list_of(style),
            Field(
                default_factory=list,
                max_length=MAX_STYLES_CHOSEN,
                description="The style they said they want for this room ('a modern living "
                "room') - replacing the style said before.",
            ),
        ),
        "purpose": (
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=120,
                description="What the room is mostly for, in their words: hosting guests, "
                "family time and TV, reading, children playing.",
            ),
        ),
        "already_own": (
            list[str],
            Field(
                default_factory=list,
                max_length=6,
                description="Product types they said, unprompted, they already have for this "
                "room ('I already have a desk and chair': office-table, office-chair). Never "
                "asked or offered.",
            ),
        ),
        "declined_size": (
            bool,
            Field(
                default=False,
                description="They would rather not give the room's size - it is not asked again.",
            ),
        ),
    }
    return cast(type[ToolArgs], create_model("NoteRoomArgs", __base__=ToolArgs, **fields))


def note_room_tool(schemas: ToolSchemas) -> ZoryTool[Any]:
    async def note(args: Any, context: ToolContext, work: TurnWork) -> ToolOutcome:
        current = work.staged.room_project
        in_progress = current is not None and bool(
            current.in_cart or current.bundle_items or current.pieces
        )
        if current is not None and in_progress and _names_another_room(current, args):
            # A room being designed, or in their cart, is never given up for
            # another by a remark about the second one.
            return ToolOutcome(
                status=ToolStatus.REFUSED,
                code="another_room_in_progress",
                facts={"room_on_record": room_words(current)},
            )
        another = current is not None and _names_another_room(current, args)
        unknown = [k for k in args.already_own if context.services.taxonomy.category_of(k) is None]
        if unknown:
            return ToolOutcome(
                status=ToolStatus.INVALID, code="unapproved_type", facts={"types": unknown}
            )
        room_type = args.room_type
        if current is not None and current.room_kind and args.room_kind == current.room_kind:
            # The same room in other words ("the lounge") keeps its walls.
            room_type = None
        measured = [
            RoomMeasurement(role=role, centimetres=m.cm())
            for role, m in (
                (RoomMeasurementRole.ROOM_LENGTH, args.length),
                (RoomMeasurementRole.ROOM_WIDTH, args.width),
                (RoomMeasurementRole.DOORWAY_WIDTH, args.doorway),
            )
            if m is not None
        ]
        measured.extend(
            RoomMeasurement(
                role=RoomMeasurementRole.USABLE_WALL, centimetres=w.length.cm(), label=w.name
            )
            for w in args.walls
            if w.length is not None
        )
        walls = tuple(
            WallNote(
                wall=w.name,
                length_cm=w.length.cm() if w.length is not None else None,
                features=tuple(dict.fromkeys(w.has)),
                note=w.note,
            )
            for w in args.walls
        )
        state = apply_update(
            work.staged,
            AgentStateUpdate(
                room_project=RoomProjectUpdate(
                    room_type=room_type,
                    room_kind=args.room_kind,
                    geometry=RoomGeometry(measurements=tuple(measured)) if measured else None,
                    # Another room's size is its own: the last room's never
                    # carries into a note about a different one.
                    clear_geometry=not measured and another,
                    walls=walls,
                    budget=args.budget.constraint() if args.budget else None,
                    size_declined=True if args.declined_size else (False if measured else None),
                    purpose=args.purpose,
                    already_own=tuple(args.already_own) or None,
                    design_preferences=_said_look(
                        current, tuple(args.colours), tuple(args.styles), another
                    ),
                )
            ),
        )
        room = state.room_project
        assert room is not None
        said = room.design_preferences
        return ToolOutcome(
            facts={
                "room": room.room_type or room.room_kind,
                "size_known": knows_room_size(room.geometry),
                **_look_facts(said),
                **({"size_declined": True} if room.size_declined else {}),
                **({"purpose": room.purpose} if room.purpose else {}),
                **({"already_own": list(room.already_own)} if room.already_own else {}),
                **(
                    {"budget": room.budget.max_amount, "currency": room.budget.currency}
                    if room.budget is not None and room.budget.max_amount is not None
                    else {}
                ),
                "walls_noted": [w.wall for w in room.layout],
            },
            state=state,
        )

    return ZoryTool(
        name="note_room",
        description=(
            "Record what they say about their room: its kind, budget, colours and style, "
            "length and width, the doorway, and each wall with what is on it (door, window, "
            "TV point, radiator, built-in, keep free). Say it once and every fit, placement "
            "and size after it uses it, and the room form leaves it off. Builds nothing."
        ),
        args=room_note_args(schemas),
        owners=ROOM_KEEPERS,
        handler=note,
    )


def _said_look(
    current: RoomProjectState | None,
    colours: tuple[str, ...],
    styles: tuple[str, ...],
    another: bool,
) -> ReplaceItems[SemanticPreference] | None:
    """The room's colours and style as they said them. What they say now
    replaces what they said before in the same family and keeps the other;
    another room starts from nothing, so a bedroom's blue never answers the
    living room's form."""
    if not (colours or styles or another):
        return None
    kept = tuple(
        p
        for p in (() if another or current is None else current.design_preferences)
        if not (p.family is AttributeFamily.COLOR and colours)
        and not (p.family is AttributeFamily.STYLE and styles)
    )
    return ReplaceItems(
        items=kept
        + tuple(_preference(AttributeFamily.COLOR, value) for value in colours)
        + tuple(_preference(AttributeFamily.STYLE, value) for value in styles)
    )


def _preference(family: AttributeFamily, value: str) -> SemanticPreference:
    return SemanticPreference(
        family=family,
        raw_value=value,
        canonical_value=value,
        strength=ConstraintStrength.PREFERRED,
    )


def _look_facts(said: tuple[SemanticPreference, ...]) -> dict[str, object]:
    colours = [p.canonical_value or p.raw_value for p in said if p.family is AttributeFamily.COLOR]
    styles = [p.canonical_value or p.raw_value for p in said if p.family is AttributeFamily.STYLE]
    return {
        **({"colours": colours} if colours else {}),
        **({"styles": styles} if styles else {}),
    }


def _names_another_room(current: RoomProjectState, args: Any) -> bool:
    """Whether the note is about a room other than the one on record."""
    if args.room_kind is not None and current.room_kind is not None:
        return bool(args.room_kind != current.room_kind)
    if args.room_type is not None and current.room_type is not None:
        return bool(args.room_type.casefold().strip() != current.room_type.casefold().strip())
    return False
