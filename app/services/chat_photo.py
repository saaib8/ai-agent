"""A photo sent with a chat message: checked, room-checked once, kept
(docs/photos-and-cart-pictures-plan.md D2).

On the turn it arrives, before the agents run: the photo is prepared (upright,
bounded, a real image), the existing room check answers once whether it is a
room - one yes or no, nothing else is read from it - and it is kept for the
chat. The expensive steps (the finder's detection, emptying the room) run only
when a tool needs them, from the kept photo.
"""

from __future__ import annotations

import base64
import binascii
import time
import uuid

from app.core.exceptions import ImageRejectedError
from app.core.logging import get_logger
from app.repositories.chat_photos import ChatPhotoStore
from app.schemas.chat_photo import ChatPhoto, ChatPhotoState
from app.schemas.retailer import RetailerContext
from app.services.finder_imaging import prepare_upload
from app.services.room_photo import RoomPhotoService

logger = get_logger(__name__)


class ChatPhotoService:
    def __init__(
        self,
        store: ChatPhotoStore,
        room_photos: RoomPhotoService | None,
        *,
        max_bytes: int,
        max_side: int,
    ) -> None:
        self._store = store
        self._room_photos = room_photos
        """None where room photos are off: the photo is still kept for the
        finder, and whether it is a room is not known."""
        self._max_bytes = max_bytes
        self._max_side = max_side

    async def receive(
        self, photo_b64: str, *, session_id: str, context: RetailerContext
    ) -> ChatPhotoState:
        """Prepare, check and keep the photo; its record for the session."""
        started = time.perf_counter()
        try:
            data = base64.b64decode(photo_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageRejectedError(public_message="That photo could not be read.") from exc
        prepared = prepare_upload(
            data, max_bytes=self._max_bytes, min_side=None, max_side=self._max_side
        )
        is_room = (
            await self._room_photos.is_room(prepared.jpeg, context)
            if self._room_photos is not None
            else None
        )
        photo = ChatPhoto(
            photo_id=uuid.uuid4().hex,
            width=prepared.width,
            height=prepared.height,
            jpeg_b64=base64.b64encode(prepared.jpeg).decode("ascii"),
        )
        await self._store.save(context.store_id, session_id, photo)
        logger.info(
            "chat_photo_received",
            store_id=context.store_id,
            width=photo.width,
            height=photo.height,
            is_room=is_room,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return ChatPhotoState(photo_id=photo.photo_id, is_room=is_room)

    async def load(
        self, state: ChatPhotoState, *, session_id: str, context: RetailerContext
    ) -> ChatPhoto | None:
        """The kept photo, or None when it expired or was replaced."""
        return await self._store.load(context.store_id, session_id, state.photo_id)
