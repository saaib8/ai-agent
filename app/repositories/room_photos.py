"""Where the customer's emptied room photo waits to be furnished.

Every Redis command for room photos is here. The key extends the session's own
key, so a room is reachable only from the store and session it was uploaded in
- the same structural scope the conversation has.

One room per session: a new upload replaces the old one under the same key, so
"Change photo" needs no clean-up and a session never holds more than one
picture. The photo's id is checked on load, so a client still holding the
replaced photo's id gets nothing rather than the new room.

Short-lived by design: it lives exactly as long as a session does. No archive,
no copy elsewhere.
"""

from __future__ import annotations

import asyncio

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import SessionSettings
from app.core.exceptions import SessionStoreUnavailableError
from app.core.logging import get_logger
from app.repositories.sessions import session_key
from app.schemas.room_photo import RoomPhoto

logger = get_logger(__name__)


def room_photo_key(store_id: int, session_id: str) -> str:
    """The session key, validated by the session store's own rule, plus the room."""
    return f"{session_key(store_id, session_id)}:room_photo"


class RoomPhotoStore:
    def __init__(self, client: Redis, settings: SessionSettings) -> None:
        self._client = client
        self._settings = settings

    async def save(self, store_id: int, session_id: str, photo: RoomPhoto) -> None:
        key = room_photo_key(store_id, session_id)
        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                await self._client.set(key, photo.model_dump_json(), ex=self._settings.ttl_s)
        except TimeoutError as exc:
            raise SessionStoreUnavailableError(dependency="redis") from exc
        except RedisError as exc:
            logger.warning("room_photo_save_failed", error_type=type(exc).__name__)
            raise SessionStoreUnavailableError(dependency="redis") from exc

    async def load(self, store_id: int, session_id: str, photo_id: str) -> RoomPhoto | None:
        """The room, or None when it expired, was replaced, never existed or is
        someone else's.

        An unreadable value is also None rather than an error: it holds nothing
        the customer built, and the remedy is the same - upload it again.
        """
        key = room_photo_key(store_id, session_id)
        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                raw = await self._client.get(key)
        except TimeoutError as exc:
            raise SessionStoreUnavailableError(dependency="redis") from exc
        except RedisError as exc:
            logger.warning("room_photo_load_failed", error_type=type(exc).__name__)
            raise SessionStoreUnavailableError(dependency="redis") from exc
        if raw is None:
            return None
        try:
            photo = RoomPhoto.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("room_photo_unreadable", error_count=len(exc.errors()))
            return None
        return photo if photo.photo_id == photo_id else None
