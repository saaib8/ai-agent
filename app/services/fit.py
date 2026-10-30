"""Will it fit: the measurements a designer judges with.

Code measures, the designer decides (docs/goal-led-agents-plan.md). Each piece
is compared with every wall and doorway the customer gave - and, when they gave
none, with the room's own length and width: a wall against the piece's longer
floor side, whatever column holds it (CLAUDE.md 15.1); a doorway against the
smaller of its shorter side and its height, the way it would be carried
through. How much floor the pieces take is counted too. Whether it works in
their room - walking space, what else is there - is Nora's judgement.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from app.schemas.design import FitVerdict, PieceFitCheck
from app.schemas.geometry import RoomGeometry, RoomMeasurementRole
from app.schemas.grounding import GroundedProduct
from app.schemas.product import ProductCandidate
from app.services.product_size import measurement
from app.taxonomy.dimensions import FloorSide, SourceAxis

MAX_WALLS: Final = 4
MAX_DOORWAYS: Final = 2

Piece = GroundedProduct | ProductCandidate


@dataclass(frozen=True, slots=True)
class Space:
    """A wall or a doorway, in centimetres."""

    kind: Literal["wall", "doorway"]
    centimetres: Decimal
    label: str | None = None


def knows_room_size(geometry: RoomGeometry | None) -> bool:
    """Whether the room's length and width are on record."""
    return (
        geometry is not None
        and bool(geometry.of(RoomMeasurementRole.ROOM_LENGTH))
        and bool(geometry.of(RoomMeasurementRole.ROOM_WIDTH))
    )


def room_spaces(geometry: RoomGeometry | None) -> tuple[Space, ...]:
    """Every wall and doorway they gave; with no wall given, the room's own
    length and width stand in as its two walls."""
    if geometry is None:
        return ()
    walls = [
        Space(kind="wall", centimetres=m.centimetres, label=m.label)
        for m in geometry.of(RoomMeasurementRole.USABLE_WALL)
    ]
    if not walls:
        walls = [
            Space(kind="wall", centimetres=m.centimetres, label=label)
            for role, label in (
                (RoomMeasurementRole.ROOM_LENGTH, "room length"),
                (RoomMeasurementRole.ROOM_WIDTH, "room width"),
            )
            for m in geometry.of(role)
        ]
    doors = [
        Space(kind="doorway", centimetres=m.centimetres, label=m.label)
        for m in geometry.of(RoomMeasurementRole.DOORWAY_WIDTH)
    ]
    # Walls and doorways are capped apart: a fourth wall never pushes the
    # doorway out of the check.
    return (*walls[:MAX_WALLS], *doors[:MAX_DOORWAYS])


def fit_checks(
    pieces: Sequence[Piece],
    spaces: Sequence[Space],
    cards: Sequence[int | None] = (),
) -> tuple[PieceFitCheck, ...]:
    """Each piece against each space: within it and by how much, too big and
    by how much, or not listed. `piece` is its place among the pieces given;
    `card`, its place on screen."""
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


def floor_share(pieces: Sequence[Piece], geometry: RoomGeometry | None) -> int | None:
    """The share of the room's floor the pieces take, as a whole percentage -
    None when the room's size or any piece's footprint is unknown."""
    if geometry is None or not knows_room_size(geometry):
        return None
    length = geometry.of(RoomMeasurementRole.ROOM_LENGTH)[0].centimetres
    width = geometry.of(RoomMeasurementRole.ROOM_WIDTH)[0].centimetres
    total = Decimal(0)
    for piece in pieces:
        longer = measurement(piece.dimensions, FloorSide.LONGER)
        shorter = measurement(piece.dimensions, FloorSide.SHORTER)
        if longer is None or shorter is None:
            return None
        total += longer * shorter
    return int(total * 100 / (length * width))


def _size(piece: Piece, space: Space) -> Decimal | None:
    sizes = piece.dimensions
    if space.kind == "wall":
        return measurement(sizes, FloorSide.LONGER)
    shorter = measurement(sizes, FloorSide.SHORTER)
    height = measurement(sizes, SourceAxis.HEIGHT)
    if shorter is None or height is None:
        return None
    return min(shorter, height)


def fit_words(check: PieceFitCheck) -> str:
    """One measurement as an agent reads it."""
    if check.verdict is FitVerdict.INSUFFICIENT_GEOMETRY or check.margin_cm is None:
        return "size not listed"
    margin = format(check.margin_cm.normalize(), "f")
    if check.verdict is FitVerdict.WITHIN_KNOWN_LIMIT:
        return f"fits with {margin} cm to spare"
    return f"{margin} cm too big"


def room_fit(
    cards: Sequence[GroundedProduct],
    geometry: RoomGeometry | None,
    chosen: Sequence[Piece] = (),
) -> dict[str, object]:
    """Each card measured against their walls and doorways, and how much floor
    it would take beside what they already chose - facts for the designer's
    judgement, never a verdict. Empty when nothing of the room is known."""
    spaces = room_spaces(geometry)
    if not spaces or not cards:
        return {}
    checks = fit_checks(cards, spaces, [c.presented_ordinal for c in cards])
    facts: dict[str, object] = {
        "room_fit": [
            {
                "card": card.presented_ordinal,
                "against": {
                    (c.label or c.space): fit_words(c) for c in checks if c.piece == position
                },
                **(
                    {"floor_taken_with_it_percent": share}
                    if (share := floor_share((*chosen, card), geometry)) is not None
                    else {}
                ),
            }
            for position, card in enumerate(cards, start=1)
        ]
    }
    if chosen and (taken := floor_share(chosen, geometry)) is not None:
        facts["floor_taken_now_percent"] = taken
    return facts
