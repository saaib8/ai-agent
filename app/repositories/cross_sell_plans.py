"""Nora's plans for what goes with a product, kept for the next shopper.

A plan for one product on its own - which kinds go with it, and how each card
leans - is a designer's judgement about the product, not about the shopper, so
the next shopper who picks the same product alone is shown it without asking
her again (CLAUDE.md 10.3). Their own taste still ranks first in every search.

Keyed by store, version and product - the version names the vocabulary and
Nora's instructions it was made under - so a plan is only ever read back for
the shop it was made in, and a change to either plans afresh (CLAUDE.md 8). A plan that cannot
be read or written is not an outage: Nora is asked instead.
"""

from __future__ import annotations

import asyncio

from pydantic import TypeAdapter, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger
from app.repositories.sessions import cross_sell_plan_key
from app.schemas.agent_state import MAX_CROSS_SELL_KINDS, CrossSellKindPlan
from app.schemas.retailer import RetailerContext

logger = get_logger(__name__)

_KINDS = TypeAdapter(tuple[CrossSellKindPlan, ...])


class CrossSellPlanCache:
    def __init__(self, client: Redis, ttl_s: int, timeout_s: float, plan_version: str) -> None:
        self._client = client
        self._ttl_s = ttl_s
        self._timeout_s = timeout_s
        self._plan_version = plan_version

    async def load(
        self, retailer: RetailerContext, product_id: int
    ) -> tuple[CrossSellKindPlan, ...] | None:
        """The kept plan's kinds, or None when absent, unreadable or Redis is
        unreachable."""
        key = cross_sell_plan_key(retailer.store_id, self._plan_version, product_id)
        try:
            async with asyncio.timeout(self._timeout_s):
                raw = await self._client.get(key)
        except (TimeoutError, RedisError) as exc:
            logger.warning("cross_sell_plan_cache_read_failed", error_type=type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            kinds = _KINDS.validate_json(raw)
        except ValidationError as exc:
            logger.warning("cross_sell_plan_cache_unreadable", error_count=len(exc.errors()))
            return None
        return kinds if 0 < len(kinds) <= MAX_CROSS_SELL_KINDS else None

    async def save(
        self, retailer: RetailerContext, product_id: int, kinds: tuple[CrossSellKindPlan, ...]
    ) -> None:
        key = cross_sell_plan_key(retailer.store_id, self._plan_version, product_id)
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._client.set(key, _KINDS.dump_json(kinds), ex=self._ttl_s)
        except (TimeoutError, RedisError) as exc:
            logger.warning("cross_sell_plan_cache_write_failed", error_type=type(exc).__name__)
