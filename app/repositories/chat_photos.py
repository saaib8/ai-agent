"""Where the photo sent with a chat message is kept.

Every Redis command for chat photos is here. The key extends the session's own
key, so a photo is reachable only from the store and chat it was sent in. One
photo a chat: a new one replaces it. It lives exactly as long as a session.
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
from app.schemas.chat_photo import ChatPhoto

logger = get_logger(__name__)


def chat_photo_key(store_id: int, session_id: str) -> str:
    """The session key, validated by the session store's own rule, plus the photo."""
    return f"{session_key(store_id, session_id)}:chat_photo"


class ChatPhotoStore:
    def __init__(self, client: Redis, settings: SessionSettings) -> None:
        self._client = client
        self._settings = settings

    async def save(self, store_id: int, session_id: str, photo: ChatPhoto) -> None:
        key = chat_photo_key(store_id, session_id)
        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                await self._client.set(key, photo.model_dump_json(), ex=self._settings.ttl_s)
        except TimeoutError as exc:
            raise SessionStoreUnavailableError(dependency="redis") from exc
        except RedisError as exc:
            logger.warning("chat_photo_save_failed", error_type=type(exc).__name__)
            raise SessionStoreUnavailableError(dependency="redis") from exc

    async def load(self, store_id: int, session_id: str, photo_id: str) -> ChatPhoto | None:
        """The photo, or None when it expired, was replaced or never existed.
        An unreadable value is None too: the remedy is to send it again."""
        key = chat_photo_key(store_id, session_id)
        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                raw = await self._client.get(key)
        except TimeoutError as exc:
            raise SessionStoreUnavailableError(dependency="redis") from exc
        except RedisError as exc:
            logger.warning("chat_photo_load_failed", error_type=type(exc).__name__)
            raise SessionStoreUnavailableError(dependency="redis") from exc
        if raw is None:
            return None
        try:
            photo = ChatPhoto.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("chat_photo_unreadable", error_count=len(exc.errors()))
            return None
        return photo if photo.photo_id == photo_id else None
