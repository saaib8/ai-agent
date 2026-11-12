"""The public health check: the agent's own dependencies and the outside
services it calls.

`HealthService` stays the container's readiness probe (PostgreSQL and Redis,
checked live). This adds the outside services - the model provider, the
semantic index and the platform's finder - each probed with a call that costs
nothing, bounded by `health.outside_timeout_s` and kept for
`health.outside_cache_s` (`health.outside_down_cache_s` once one is down), so a
public URL polled by anyone never becomes a stream of provider calls.

What counts as down for the service as a whole: PostgreSQL, Redis or the model
provider, without which no chat can be answered. The index and the finder only
narrow what the agent can do (results fall back to deterministic order; photo
search refuses), so either one down is `degraded`, not down.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.core.config import HealthSettings
from app.core.logging import get_logger
from app.services.health import HealthService

logger = get_logger(__name__)

LLM = "llm"
PINECONE = "pinecone"
ZORY_FINDER = "zory_finder"
REQUIRED_OUTSIDE = frozenset({LLM})
"""Outside services without which the agent cannot answer a chat."""


class ServiceStatus(StrEnum):
    UP = "up"
    DOWN = "down"
    NOT_CONFIGURED = "not_configured"


class OverallStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


@runtime_checkable
class HealthProbe(Protocol):
    async def check_health(self) -> bool: ...


def as_probe(resource: object) -> HealthProbe | None:
    """The resource as a probe; None when it is not configured or cannot be probed."""
    return resource if isinstance(resource, HealthProbe) else None


class ServiceHealthReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: OverallStatus
    dependencies: dict[str, ServiceStatus]


Clock = Callable[[], float]


class OutsideHealthCache:
    """The last outside-probe results, one per process (`AppResources`).

    A refresh runs under a lock, so probes arriving together while the results
    are stale wait for one round of outside calls instead of each making one.
    """

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._results: dict[str, ServiceStatus] | None = None
        self._checked_at = 0.0

    async def get(
        self,
        refresh: Callable[[], Awaitable[dict[str, ServiceStatus]]],
        *,
        max_age_s: float,
        down_max_age_s: float,
    ) -> dict[str, ServiceStatus]:
        fresh = self._fresh(max_age_s, down_max_age_s)
        if fresh is not None:
            return fresh
        async with self._lock:
            fresh = self._fresh(max_age_s, down_max_age_s)  # refreshed while this one waited
            if fresh is not None:
                return fresh
            results = await refresh()
            self._results, self._checked_at = results, self._clock()
            return dict(results)

    def _fresh(self, max_age_s: float, down_max_age_s: float) -> dict[str, ServiceStatus] | None:
        if self._results is None:
            return None
        # A service seen down is asked again sooner: one slow first connection
        # or a blip must not read as an outage for a whole window.
        any_down = ServiceStatus.DOWN in self._results.values()
        if self._clock() - self._checked_at >= (down_max_age_s if any_down else max_age_s):
            return None
        return dict(self._results)


class ServiceHealthService:
    def __init__(
        self,
        core: HealthService,
        *,
        llm: HealthProbe,
        semantic_index: HealthProbe | None,
        zory_finder: HealthProbe | None,
        cache: OutsideHealthCache,
        settings: HealthSettings,
    ) -> None:
        self._core = core
        self._outside: dict[str, HealthProbe | None] = {
            LLM: llm,
            PINECONE: semantic_index,
            ZORY_FINDER: zory_finder,
        }
        self._cache = cache
        self._settings = settings

    async def check(self) -> ServiceHealthReport:
        core, outside = await asyncio.gather(
            self._core.check(),
            self._cache.get(
                self._probe_outside,
                max_age_s=self._settings.outside_cache_s,
                down_max_age_s=self._settings.outside_down_cache_s,
            ),
        )
        dependencies = {
            name: ServiceStatus(status.value) for name, status in core.dependencies.items()
        } | outside
        return ServiceHealthReport(
            status=_overall(core_healthy=core.healthy, outside=outside, all_=dependencies),
            dependencies=dependencies,
        )

    async def _probe_outside(self) -> dict[str, ServiceStatus]:
        names = list(self._outside)
        results = await asyncio.gather(*(self._probe(name, self._outside[name]) for name in names))
        return dict(zip(names, results, strict=True))

    async def _probe(self, name: str, probe: HealthProbe | None) -> ServiceStatus:
        if probe is None:
            return ServiceStatus.NOT_CONFIGURED
        try:
            healthy = await asyncio.wait_for(
                probe.check_health(), timeout=self._settings.outside_timeout_s
            )
        except TimeoutError:
            logger.warning("outside_health_probe_timed_out", dependency=name)
            return ServiceStatus.DOWN
        except Exception as exc:
            # A probe reports, it never fails the health check: an unexpected
            # error is that service being down, logged by type only (CLAUDE.md 21).
            logger.warning(
                "outside_health_probe_failed", dependency=name, error_type=type(exc).__name__
            )
            return ServiceStatus.DOWN
        return ServiceStatus.UP if healthy else ServiceStatus.DOWN


def _overall(
    *,
    core_healthy: bool,
    outside: dict[str, ServiceStatus],
    all_: dict[str, ServiceStatus],
) -> OverallStatus:
    required_down = any(outside.get(name) is ServiceStatus.DOWN for name in REQUIRED_OUTSIDE)
    if not core_healthy or required_down:
        return OverallStatus.DOWN
    if any(status is ServiceStatus.DOWN for status in all_.values()):
        return OverallStatus.DEGRADED
    return OverallStatus.OK
