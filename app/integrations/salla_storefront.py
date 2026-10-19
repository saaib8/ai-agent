"""Salla's public storefront API: what a Salla store's own cart will take.

Keyless - a store is named by its Salla id in `Store-Identifier` - and asked
only before a product goes in the cart (CLAUDE.md 36). Every failure to read
it is "unknown", never "refused": a slow or unreachable Salla must not take
the cart down, and the widget's own copy into Salla has the last word.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from app.core.config import SallaSettings
from app.core.logging import get_logger

logger = get_logger(__name__)

_MAX_CACHED = 5000

_MOMENTARY = frozenset({429, 500, 502, 503, 504})
"""Salla busy or failing for a moment: asked again, never read as an answer."""


@dataclass(frozen=True, slots=True)
class SallaProduct:
    """One product as a Salla store's cart sees it."""

    in_store: bool
    """Salla knows it in this store. A product of another store's - or one
    the store removed - answers 404 or 410."""

    available: bool
    max_quantity: int | None
    """How many one order may take, when Salla limits it."""


class SallaStorefrontClient:
    """One HTTP client per process, with a short memory of what it was told."""

    def __init__(
        self, settings: SallaSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.storefront_url, timeout=settings.timeout_s, transport=transport
        )
        self._ttl_s = settings.cache_ttl_s
        self._retries = settings.retries
        self._backoff_s = settings.retry_backoff_s
        self._cache: dict[tuple[int, str], tuple[float, SallaProduct]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def product(self, salla_store_id: int, salla_product_id: str) -> SallaProduct | None:
        """What this store's cart would make of the product; None when Salla
        could not be asked or answered something unreadable."""
        if not salla_product_id.isdigit():
            return SallaProduct(in_store=False, available=False, max_quantity=None)
        key = (salla_store_id, salla_product_id)
        cached = self._cache.get(key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        answer = await self._ask(salla_store_id, salla_product_id)
        if answer is not None and self._ttl_s > 0:
            if len(self._cache) >= _MAX_CACHED:
                self._cache.clear()
            self._cache[key] = (time.monotonic() + self._ttl_s, answer)
        return answer

    async def _ask(self, salla_store_id: int, salla_product_id: str) -> SallaProduct | None:
        response = None
        for attempt in range(self._retries + 1):
            if attempt:
                await asyncio.sleep(self._backoff_s * attempt)
            try:
                response = await self._client.get(
                    f"/products/{salla_product_id}/details",
                    headers={
                        "Store-Identifier": str(salla_store_id),
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "salla_storefront_unreachable", error=exc.__class__.__name__, attempt=attempt
                )
                response = None
                continue
            if response.status_code not in _MOMENTARY:
                break
            logger.warning("salla_storefront_busy", status=response.status_code, attempt=attempt)
        if response is None:
            return None
        if response.status_code in (404, 410):
            return SallaProduct(in_store=False, available=False, max_quantity=None)
        if response.status_code != 200:
            logger.warning("salla_storefront_refused", status=response.status_code)
            return None
        try:
            data = response.json().get("data")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            logger.warning("salla_storefront_unreadable")
            return None
        limit = data.get("max_quantity")
        return SallaProduct(
            in_store=True,
            available=bool(data.get("is_available")) and not data.get("is_out_of_stock"),
            max_quantity=limit if isinstance(limit, int) and limit > 0 else None,
        )
