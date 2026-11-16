"""A photo sent with a chat message (docs/photos-and-cart-pictures-plan.md D2).

The photo itself is kept beside the session (`ChatPhotoStore`); the session
holds only a small record of it (`ChatPhotoState`), so every agent's brief can
say a photo is there and what it is - never the picture.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

CHAT_PHOTO_ID_PATTERN: Final[str] = r"^[0-9a-f]{32}$"

MAX_CHAT_PHOTO_CHARS: Final[int] = 24 * 1024 * 1024
"""The most a chat request's `photo` may hold, base64 - far above a photo the
gateway prepared (a few hundred kilobytes), so only absurd input is refused
here; the photo's own checks decide the rest."""


class ChatPhoto(BaseModel):
    """The photo as it arrived - checked and kept for the chat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    photo_id: str = Field(pattern=CHAT_PHOTO_ID_PATTERN)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    jpeg_b64: str = Field(min_length=1)


class ChatPhotoState(BaseModel):
    """What the session knows of the chat's photo - never the picture."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    photo_id: str = Field(pattern=CHAT_PHOTO_ID_PATTERN)
    is_room: bool | None = None
    """The room check's answer: a photo of a room, not a room, or None when the
    check could not be made - never read as "not a room"."""
    room_photo_id: str | None = Field(default=None, pattern=CHAT_PHOTO_ID_PATTERN)
    """The emptied room made from it, once their cart was drawn into it."""
    finder_image_id: str | None = Field(default=None, pattern=CHAT_PHOTO_ID_PATTERN)
    """The finder's detection of it, once furniture was looked for in it."""
    pieces: tuple[str, ...] = ()
    """What the finder found in it, by kind, numbered by place - what
    `find_in_photo`'s `number` names."""
