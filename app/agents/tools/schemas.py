"""What the tools' argument schemas are built from: the registries, once."""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.query import CommerceInterpretation
from app.taxonomy.attributes import CatalogAttributes
from app.taxonomy.rooms import RoomPieces


@dataclass(frozen=True, slots=True)
class ToolSchemas:
    interpretation: type[CommerceInterpretation]
    """The search interpretation restricted to approved values."""
    attributes: CatalogAttributes
    room_pieces: RoomPieces | None = None
