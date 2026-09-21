"""Redis access: one lifespan-managed connection pool per process.

Nothing stores state here yet — session state arrives in its own milestone.
What exists now is the client lifecycle and a health probe, so the dependency
is wired, observable and disposable from day one (CLAUDE.md 24).
"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import RedisSettings
from app.core.exceptions import SessionStoreUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class RedisClient:
    def __init__(self, client: Redis) -> None:
        self._client = client

    @classmethod
    def create(cls, settings: RedisSettings) -> RedisClient:
        client: Redis = Redis.from_url(
            settings.url.get_secret_value(),
            socket_connect_timeout=settings.connect_timeout_s,
            socket_timeout=settings.socket_timeout_s,
            max_connections=settings.max_connections,
            decode_responses=True,
        )
        return cls(client)

    @property
    def client(self) -> Redis:
        return self._client

    async def check_health(self) -> bool:
        try:
            await self._client.ping()
        except RedisError as exc:
            logger.warning("redis_health_check_failed", error_type=type(exc).__name__)
            return False
        return True

    async def close(self) -> None:
        await self._client.aclose()


async def require_healthy(redis_client: RedisClient) -> None:
    """Raise the public-safe session-store error when Redis is unreachable."""
    if not await redis_client.check_health():
        raise SessionStoreUnavailableError(dependency="redis")
