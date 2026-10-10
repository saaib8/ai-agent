"""Will it fit: the measurements a designer judges with
(docs/designer-led-shopping-plan.md, phase 7).

Code measures, the designer decides. Each piece the question is about is
compared with every wall and doorway the customer gave: a wall against the
piece's longer floor side, whatever column holds it (CLAUDE.md 15.1); a doorway
against the smaller of its shorter side and its height, the way it would be
carried through. Whether it works in their room - walking space, what else is
there - is the design specialist's call, with the room's size.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from app.schemas.agent_state import AgentStateV1, RoomCheck
from app.schemas.design import FitVerdict, PieceFitCheck
from app.schemas.geometry import RoomGeometry, RoomMeasurementRole
from app.schemas.product import ProductCandidate
from app.services.product_size import measurement
from app.taxonomy.dimensions import FloorSide, SourceAxis

MAX_SPACES: Final = 4


@dataclass(frozen=True, slots=True)
class Space:
    """A wall or a doorway they gave, in centimetres."""

    kind: Literal["wall", "doorway"]
    centimetres: Decimal
    label: str | None = None


def awaiting_room_check(state: AgentStateV1) -> RoomCheck | None:
    """The room's size asked for a pick, while a reply can still answer it:
    not yet judged, the pick still theirs, and the list it was asked beside
    still on screen - once another list replaces it, nothing answers it, as
    with a taste question (CLAUDE.md 10.9, 10.11)."""
    check = state.room_check
    interaction = state.product_interaction
    if (
        check is None
        or check.answered
        or check.product_id not in interaction.selected_product_ids
        or check.list_revision != interaction.presented_search_revision
    ):
        return None
    return check


def knows_room_size(geometry: RoomGeometry | None) -> bool:
    """Whether the room's length and width are on record - what any judgement
    of fit needs before anything else."""
    return (
        geometry is not None
        and bool(geometry.of(RoomMeasurementRole.ROOM_LENGTH))
        and bool(geometry.of(RoomMeasurementRole.ROOM_WIDTH))
    )


def spaces_of(state: AgentStateV1) -> tuple[Space, ...]:
    """Every wall and doorway the customer has given: the room's, and the
    space a search was for."""
    spaces: list[Space] = []
    room = state.room_project
    if room is not None and room.geometry is not None:
        spaces.extend(
            Space(kind="wall", centimetres=m.centimetres, label=m.label)
            for m in room.geometry.of(RoomMeasurementRole.USABLE_WALL)
        )
        spaces.extend(
            Space(kind="doorway", centimetres=m.centimetres, label=m.label)
            for m in room.geometry.of(RoomMeasurementRole.DOORWAY_WIDTH)
        )
    lean = state.active_search.lean if state.active_search is not None else None
    if (
        lean is not None
        and lean.space_cm is not None
        and all(s.centimetres != lean.space_cm for s in spaces if s.kind == "wall")
    ):
        spaces.append(Space(kind="wall", centimetres=lean.space_cm))
    return tuple(spaces[-MAX_SPACES:])


def fit_checks(
    pieces: Sequence[ProductCandidate],
    spaces: Sequence[Space],
    cards: Sequence[int | None] = (),
) -> tuple[PieceFitCheck, ...]:
    """Each piece against each space: within it and by how much, too big and
    by how much, or not listed. `piece` is its place among the anchors the
    designer is given; `card`, its place on screen."""
    checks: list[PieceFitCheck] = []
    for position, piece in enumerate(pieces, start=1):
        card = cards[position - 1] if position <= len(cards) else None
        for space in spaces:
            size = _size(piece, space)
            within = size is not None and size <= space.centimetres
            checks.append(
                PieceFitCheck(
                    piece=position,
                    card=card,
                    space=space.kind,
                    label=space.label,
                    space_cm=space.centimetres,
                    piece_cm=size,
                    verdict=(
                        FitVerdict.INSUFFICIENT_GEOMETRY
                        if size is None
                        else FitVerdict.WITHIN_KNOWN_LIMIT
                        if within
                        else FitVerdict.EXCEEDS_KNOWN_LIMIT
                    ),
                    margin_cm=abs(space.centimetres - size) if size is not None else None,
                )
            )
    return tuple(checks)


def _size(piece: ProductCandidate, space: Space) -> Decimal | None:
    sizes = piece.dimensions
    if space.kind == "wall":
        return measurement(sizes, FloorSide.LONGER)
    shorter = measurement(sizes, FloorSide.SHORTER)
    height = measurement(sizes, SourceAxis.HEIGHT)
    if shorter is None or height is None:
        return None
    return min(shorter, height)
