"""Where an uploaded photo waits for the customer to pick something in it.

Every Redis command for finder photos is here. The key extends the session's
own key, so a photo is reachable only from the store and session it was
uploaded in - the same structural scope the conversation has, with no field in
the value that could disagree about it.

Short-lived by design: a photo lives exactly as long as a session does, and it
is written once and never updated. No archive, no copy elsewhere.

A local run also keeps here the anonymous visitor it signed in for the
session, so one session is one visitor on the main platform, as it is for a
shopper on the widget. Never in a deployed environment.
"""

from __future__ import annotations

import asyncio
import json

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import SessionSettings
from app.core.exceptions import SessionStoreUnavailableError
from app.core.logging import get_logger
from app.repositories.sessions import session_key
from app.schemas.furniture_finder import FinderPhoto, ShopperCredentials

logger = get_logger(__name__)


def photo_key(store_id: int, session_id: str, image_id: str) -> str:
    """The session key, validated by the session store's own rule, plus the photo."""
    return f"{session_key(store_id, session_id)}:finder:{image_id}"


def visitor_key(store_id: int, session_id: str) -> str:
    """The session key plus the anonymous visitor a local run signed in for it."""
    return f"{session_key(store_id, session_id)}:finder-visitor"


class FinderPhotoStore:
    def __init__(self, client: Redis, settings: SessionSettings) -> None:
        self._client = client
        self._settings = settings

    async def save(self, store_id: int, session_id: str, photo: FinderPhoto) -> None:
        key = photo_key(store_id, session_id, photo.image_id)
        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                await self._client.set(key, photo.model_dump_json(), ex=self._settings.ttl_s)
        except TimeoutError as exc:
            raise SessionStoreUnavailableError(dependency="redis") from exc
        except RedisError as exc:
            logger.warning("finder_photo_save_failed", error_type=type(exc).__name__)
            raise SessionStoreUnavailableError(dependency="redis") from exc

    async def load(self, store_id: int, session_id: str, image_id: str) -> FinderPhoto | None:
        """The photo, or None when it expired, never existed or is someone else's.

        An unreadable value is also None rather than an error: unlike a
        session, a photo holds nothing the customer built, and the remedy for
        both is the same - upload it again.
        """
        key = photo_key(store_id, session_id, image_id)
        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                raw = await self._client.get(key)
        except TimeoutError as exc:
            raise SessionStoreUnavailableError(dependency="redis") from exc
        except RedisError as exc:
            logger.warning("finder_photo_load_failed", error_type=type(exc).__name__)
            raise SessionStoreUnavailableError(dependency="redis") from exc
        if raw is None:
            return None
        try:
            return FinderPhoto.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("finder_photo_unreadable", error_count=len(exc.errors()))
            return None

    # ── the local anonymous visitor ─────────────────────────────────────────

    async def save_visitor(
        self, store_id: int, session_id: str, visitor: ShopperCredentials, ttl_s: int
    ) -> None:
        """Keep the visitor no longer than its token lives, nor the session does."""
        key = visitor_key(store_id, session_id)
        value = json.dumps(
            {
                "token": visitor.token.get_secret_value(),
                "widget_key": visitor.widget_key,
                "store_public_id": visitor.store_public_id,
            }
        )
        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                await self._client.set(key, value, ex=max(1, min(ttl_s, self._settings.ttl_s)))
        except TimeoutError as exc:
            raise SessionStoreUnavailableError(dependency="redis") from exc
        except RedisError as exc:
            logger.warning("finder_visitor_save_failed", error_type=type(exc).__name__)
            raise SessionStoreUnavailableError(dependency="redis") from exc

    async def load_visitor(self, store_id: int, session_id: str) -> ShopperCredentials | None:
        key = visitor_key(store_id, session_id)
        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                raw = await self._client.get(key)
        except TimeoutError as exc:
            raise SessionStoreUnavailableError(dependency="redis") from exc
        except RedisError as exc:
            logger.warning("finder_visitor_load_failed", error_type=type(exc).__name__)
            raise SessionStoreUnavailableError(dependency="redis") from exc
        if raw is None:
            return None
        try:
            return ShopperCredentials.model_validate_json(raw)
        except ValidationError:
            return None

    async def forget_visitor(self, store_id: int, session_id: str) -> None:
        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                await self._client.delete(visitor_key(store_id, session_id))
        except (TimeoutError, RedisError) as exc:
            logger.warning("finder_visitor_forget_failed", error_type=type(exc).__name__)
