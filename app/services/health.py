"""Dependency readiness checks behind the health endpoint.

Lives in a service rather than the route so the route stays transport-only
(CLAUDE.md 3.2), and so readiness can be reused by future probes.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.integrations.postgres import Database
from app.integrations.redis import RedisClient


class DependencyStatus(StrEnum):
    UP = "up"
    DOWN = "down"


class HealthReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    healthy: bool
    dependencies: dict[str, DependencyStatus]


class HealthService:
    def __init__(self, database: Database, redis_client: RedisClient) -> None:
        self._database = database
        self._redis = redis_client

    async def check(self) -> HealthReport:
        postgres_ok, redis_ok = await asyncio.gather(
            self._database.check_health(), self._redis.check_health()
        )
        dependencies = {
            "postgres": DependencyStatus.UP if postgres_ok else DependencyStatus.DOWN,
            "redis": DependencyStatus.UP if redis_ok else DependencyStatus.DOWN,
        }
        return HealthReport(
            healthy=postgres_ok and redis_ok, dependencies=dependencies
        )
