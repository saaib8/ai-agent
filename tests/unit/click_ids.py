"""A stand-in for the catalog's click-id lookup, for tests that mount a route."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.schemas.retailer import RetailerContext
from app.services.click_tracking import ClickTrackingService


class FakeClickIds:
    """Answers `click_ids_by_url` from a fixed map, recording each lookup."""

    def __init__(self, click_ids: Mapping[str, str] | None = None) -> None:
        self.click_ids = dict(click_ids or {})
        self.lookups: list[tuple[tuple[str, ...], int]] = []

    async def click_ids_by_url(
        self, product_urls: Sequence[str], context: RetailerContext
    ) -> dict[str, str]:
        self.lookups.append((tuple(product_urls), context.store_id))
        return {url: self.click_ids[url] for url in product_urls if url in self.click_ids}


def no_click_ids() -> ClickTrackingService:
    """The route dependency for a test that is not about click ids."""
    return ClickTrackingService(FakeClickIds())
