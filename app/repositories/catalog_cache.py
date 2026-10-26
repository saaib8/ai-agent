"""The store's shelf summary, kept briefly so every tool reads one scan.

Every Redis command for the catalog overview is here. The key names the store
and the taxonomy version, so an overview is only ever read back for the shop
it was built from and under the vocabulary it was validated against
(CLAUDE.md 8, 9).

Django owns catalog writes and tells this service nothing, so the summary is
held only for a short configurable time and then rebuilt: a product added or
removed shows up within that window. A cache that cannot be reached is not an
outage - the overview is read from PostgreSQL instead.
"""

from __future__ import annotations

import asyncio

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger
from app.repositories.sessions import catalog_overview_key
from app.schemas.catalog_overview import CatalogOverview

logger = get_logger(__name__)


class OverviewCache:
    def __init__(self, client: Redis, ttl_s: int, timeout_s: float) -> None:
        self._client = client
        self._ttl_s = ttl_s
        self._timeout_s = timeout_s

    async def load(self, store_id: int, taxonomy_version: str) -> CatalogOverview | None:
        """The cached overview, or None when absent, unreadable, someone
        else's or Redis is unreachable."""
        try:
            async with asyncio.timeout(self._timeout_s):
                raw = await self._client.get(catalog_overview_key(store_id, taxonomy_version))
        except (TimeoutError, RedisError) as exc:
            logger.warning("catalog_overview_cache_read_failed", error_type=type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            overview = CatalogOverview.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("catalog_overview_cache_unreadable", error_count=len(exc.errors()))
            return None
        return overview if overview.store_id == store_id else None

    async def save(self, overview: CatalogOverview, taxonomy_version: str) -> None:
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._client.set(
                    catalog_overview_key(overview.store_id, taxonomy_version),
                    overview.model_dump_json(),
                    ex=self._ttl_s,
                )
        except (TimeoutError, RedisError) as exc:
            logger.warning("catalog_overview_cache_write_failed", error_type=type(exc).__name__)
