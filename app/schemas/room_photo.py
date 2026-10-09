"""The customer's own room photo: uploaded once, emptied once, furnished often.

A photo of the customer's room is checked to be a room, emptied of its
furniture and decor, and kept for the session so the pieces can be placed in it
again after the room changes - without another upload or another emptying.
Only the emptied room is kept: it is what every render is drawn on.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

ROOM_PHOTO_ID_PATTERN: Final[str] = r"^[0-9a-f]{32}$"


class RoomCheck(BaseModel):
    """A vision model's answer to "is this a photo of a room?"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    is_room: bool
    """An indoor room - furnished or empty - photographed from inside it."""


class RoomPhoto(BaseModel):
    """The emptied room, as the session keeps it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    photo_id: str = Field(pattern=ROOM_PHOTO_ID_PATTERN)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    jpeg_b64: str = Field(min_length=1)
    """The emptied room, at the size the customer's photo was prepared to."""


class RoomPhotoResponse(BaseModel):
    """What a client needs to render in the room: its handle and its shape.
    Never the picture: the customer never sees the emptied room on its own."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_photo_id: str = Field(pattern=ROOM_PHOTO_ID_PATTERN)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
