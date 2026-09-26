"""Which pieces a room is made of, as reviewed domain data.

A room the customer asks to have designed is built from pieces they chose, not
from a composition a model invents: the chips they pick from come from here,
intersected with what the store actually stocks (CLAUDE.md 9.1, 10.3). Each
piece has a tier - essential, recommended or optional - which decides whether
it starts selected and what a short budget gives up first.

One piece may be built from several types. The living room's seating is met
with however many sofas, sets, sectionals or single seats the customer's head
count needs (CLAUDE.md 27.1), and the chip names it simply "Sofa".

The loader mirrors the other registries: read the versioned file, validate its
shape, and reject any type the commerce taxonomy does not approve, so this file
cannot drift from the vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

from app.core.exceptions import TaxonomyConfigurationError
from app.taxonomy.registry import CommerceTaxonomy
from app.taxonomy.seating import SEATING_CATEGORY, SeatingSemantics

DEFAULT_ROOM_PIECES_PATH: Final[Path] = Path(__file__).parent / "room_pieces_v1.yaml"

MAX_PIECE_QUANTITY: Final[int] = 4


class PieceTier(StrEnum):
    """How much a room needs a piece. Ordered by what a short budget keeps."""

    ESSENTIAL = "essential"
    RECOMMENDED = "recommended"
    OPTIONAL = "optional"

    @property
    def starts_selected(self) -> bool:
        return self is not PieceTier.OPTIONAL


@dataclass(frozen=True, slots=True)
class RoomPiece:
    """One chip: a piece of one approved type, or the room's seating."""

    key: str
    label: str
    tier: PieceTier
    commerce_category: str
    commerce_subcategory: str | None
    quantity: int = 1
    seating_types: tuple[str, ...] = ()
    """For seating built to a head count: every type it may use. The first is
    the single piece used when the customer gives no count."""

    @property
    def is_seating(self) -> bool:
        return bool(self.seating_types)

    @property
    def types(self) -> tuple[str, ...]:
        """Every subcategory this piece may be filled with."""
        if self.seating_types:
            return self.seating_types
        return (self.commerce_subcategory,) if self.commerce_subcategory else ()


@dataclass(frozen=True, slots=True)
class RoomTemplate:
    """The pieces one kind of room may hold, in the order they are offered."""

    kind: str
    pieces: tuple[RoomPiece, ...]
    asks_seats: bool

    def piece(self, key: str) -> RoomPiece | None:
        return next((p for p in self.pieces if p.key == key), None)

    @property
    def seating(self) -> RoomPiece | None:
        return next((p for p in self.pieces if p.is_seating), None)


class RoomPieces:
    """Every room template, keyed by room kind."""

    def __init__(self, version: str, rooms: Mapping[str, RoomTemplate]) -> None:
        self._version = version
        self._rooms = dict(rooms)

    @property
    def version(self) -> str:
        return self._version

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(self._rooms)

    def template(self, kind: str | None) -> RoomTemplate | None:
        return None if kind is None else self._rooms.get(kind)

    def __repr__(self) -> str:
        return f"RoomPieces(version={self._version!r}, rooms={sorted(self._rooms)})"


def load_room_pieces(
    path: Path | None = None,
    taxonomy: CommerceTaxonomy | None = None,
    seating: SeatingSemantics | None = None,
) -> RoomPieces:
    """Load and validate the room registry. Raises on anything malformed."""
    source = path or DEFAULT_ROOM_PIECES_PATH
    try:
        document: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TaxonomyConfigurationError(
            detail=f"cannot read room pieces at {source}: {type(exc).__name__}"
        ) from exc
    except yaml.YAMLError as exc:
        raise TaxonomyConfigurationError(
            detail=f"{source.name} is not valid YAML: {type(exc).__name__}"
        ) from exc
    if not isinstance(document, dict):
        raise TaxonomyConfigurationError(detail=f"{source.name}: top level must be a mapping")
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise TaxonomyConfigurationError(
            detail=f"{source.name}: 'version' must be a non-empty string"
        )
    raw_rooms = document.get("rooms")
    if not isinstance(raw_rooms, dict) or not raw_rooms:
        raise TaxonomyConfigurationError(detail=f"{source.name}: 'rooms' must be a mapping")

    rooms: dict[str, RoomTemplate] = {}
    for kind, raw in raw_rooms.items():
        if not isinstance(kind, str) or not kind or not isinstance(raw, dict):
            raise TaxonomyConfigurationError(detail=f"{source.name}: each room must be a mapping")
        rooms[kind] = _room(kind, raw, source, taxonomy, seating)
    return RoomPieces(version=version, rooms=rooms)


def _room(
    kind: str,
    raw: dict[str, Any],
    source: Path,
    taxonomy: CommerceTaxonomy | None,
    seating: SeatingSemantics | None,
) -> RoomTemplate:
    asks_seats = raw.get("asks_seats", False)
    if not isinstance(asks_seats, bool):
        raise TaxonomyConfigurationError(detail=f"{source.name}: {kind}.asks_seats must be a bool")
    raw_pieces = raw.get("pieces")
    if not isinstance(raw_pieces, list) or not raw_pieces:
        raise TaxonomyConfigurationError(detail=f"{source.name}: {kind} needs pieces")
    pieces = tuple(_piece(kind, entry, source, taxonomy, seating) for entry in raw_pieces)

    keys = [p.key for p in pieces]
    if len(keys) != len(set(keys)):
        raise TaxonomyConfigurationError(detail=f"{source.name}: {kind} repeats a piece key")
    if sum(p.is_seating for p in pieces) > 1:
        raise TaxonomyConfigurationError(detail=f"{source.name}: {kind} has two seating pieces")
    if asks_seats and not any(p.is_seating for p in pieces):
        raise TaxonomyConfigurationError(
            detail=f"{source.name}: {kind} asks for seats but has no seating piece"
        )
    if not any(p.tier is PieceTier.ESSENTIAL for p in pieces):
        raise TaxonomyConfigurationError(detail=f"{source.name}: {kind} has no essential piece")
    return RoomTemplate(kind=kind, pieces=pieces, asks_seats=asks_seats)


def _piece(
    kind: str,
    raw: Any,
    source: Path,
    taxonomy: CommerceTaxonomy | None,
    seating: SeatingSemantics | None,
) -> RoomPiece:
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{source.name}: {kind} pieces must be mappings")
    key, label = raw.get("key"), raw.get("label")
    if not isinstance(key, str) or not key or not isinstance(label, str) or not label:
        raise TaxonomyConfigurationError(
            detail=f"{source.name}: {kind} piece needs a key and a label"
        )
    where = f"{source.name}: {kind}.{key}"
    try:
        tier = PieceTier(str(raw.get("tier")))
    except ValueError as exc:
        raise TaxonomyConfigurationError(detail=f"{where}: unknown tier") from exc
    quantity = raw.get("quantity", 1)
    if (
        not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or not 1 <= quantity <= MAX_PIECE_QUANTITY
    ):
        raise TaxonomyConfigurationError(detail=f"{where}: quantity must be 1-{MAX_PIECE_QUANTITY}")

    types = raw.get("seating")
    if types is not None:
        return _seating_piece(key, label, tier, quantity, types, where, taxonomy, seating)

    category, subcategory = raw.get("category"), raw.get("subcategory")
    if not isinstance(category, str) or not isinstance(subcategory, str):
        raise TaxonomyConfigurationError(detail=f"{where}: needs a category and a subcategory")
    if taxonomy is not None and not taxonomy.is_pair(category, subcategory):
        raise TaxonomyConfigurationError(detail=f"{where}: not an approved pair")
    return RoomPiece(
        key=key,
        label=label,
        tier=tier,
        commerce_category=category,
        commerce_subcategory=subcategory,
        quantity=quantity,
    )


def _seating_piece(
    key: str,
    label: str,
    tier: PieceTier,
    quantity: int,
    types: Any,
    where: str,
    taxonomy: CommerceTaxonomy | None,
    seating: SeatingSemantics | None,
) -> RoomPiece:
    if not isinstance(types, list) or not types or not all(isinstance(t, str) for t in types):
        raise TaxonomyConfigurationError(detail=f"{where}: seating must list subcategories")
    if len(types) != len(set(types)):
        raise TaxonomyConfigurationError(detail=f"{where}: seating repeats a type")
    if quantity != 1:
        raise TaxonomyConfigurationError(detail=f"{where}: seating is sized by head count")
    for value in types:
        if taxonomy is not None and not taxonomy.is_pair(SEATING_CATEGORY, value):
            raise TaxonomyConfigurationError(detail=f"{where}: {value!r} is not approved seating")
        if seating is not None and not (seating.seats_several(value) or seating.seats_one(value)):
            # A type whose seat count nobody reviewed cannot be counted toward
            # a head count (CLAUDE.md 6.2).
            raise TaxonomyConfigurationError(
                detail=f"{where}: {value!r} has no reviewed seat count"
            )
    if seating is not None and not seating.seats_several(types[0]):
        raise TaxonomyConfigurationError(
            detail=f"{where}: the first seating type must seat several"
        )
    return RoomPiece(
        key=key,
        label=label,
        tier=tier,
        commerce_category=SEATING_CATEGORY,
        commerce_subcategory=None,
        seating_types=tuple(types),
    )
