"""The vocabulary's tables as last read, kept so the service can always start.

Saved after every good read, with no expiry; read only at startup when the
database cannot be (CLAUDE.md 14.1). It holds vocabulary - names and their
Arabic - never a product, a price or anything of a shopper's. A copy that cannot
be read or written is not an outage: startup then waits for the database.
"""

from __future__ import annotations

import asyncio

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger
from app.repositories.sessions import VOCABULARY_COPY_KEY
from app.taxonomy.tables import VocabularyTables

logger = get_logger(__name__)


class VocabularyCopy:
    def __init__(self, client: Redis, timeout_s: float) -> None:
        self._client = client
        self._timeout_s = timeout_s

    async def load(self) -> VocabularyTables | None:
        try:
            async with asyncio.timeout(self._timeout_s):
                raw = await self._client.get(VOCABULARY_COPY_KEY)
        except (TimeoutError, RedisError, OSError) as exc:
            logger.warning("vocabulary_copy_read_failed", error_type=type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            return VocabularyTables.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("vocabulary_copy_unreadable", error_count=len(exc.errors()))
            return None

    async def save(self, tables: VocabularyTables) -> None:
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._client.set(VOCABULARY_COPY_KEY, tables.model_dump_json())
        except (TimeoutError, RedisError, OSError) as exc:
            logger.warning("vocabulary_copy_write_failed", error_type=type(exc).__name__)
