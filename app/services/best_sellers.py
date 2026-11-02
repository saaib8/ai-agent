"""Best sellers, only when a customer asks, and only ever said positively.

A product is a best seller by one rule for every store (`BestSellerRule`),
ranked within its own store's products of its kind. This service answers only
which products are best sellers - never a sales number, and never that a
product is not one: a product that does not qualify is simply not named.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.config import ProductDataSettings
from app.repositories.products import ProductRepository
from app.schemas.product import BestSellerRule
from app.schemas.retailer import RetailerContext


class BestSellerService:
    def __init__(self, repository: ProductRepository, settings: ProductDataSettings) -> None:
        self._repository = repository
        self._enabled = settings.best_sellers_enabled
        self._rule = BestSellerRule(
            min_sold=settings.best_sellers_min_sold,
            top_share=settings.best_sellers_top_share,
            max_per_kind=settings.best_sellers_max_per_kind,
        )

    async def among(self, product_ids: Sequence[int], context: RetailerContext) -> frozenset[int]:
        """Which of these products are best sellers - only those."""
        if not self._enabled or not product_ids:
            return frozenset()
        found = await self._repository.best_sellers(context, self._rule, product_ids=product_ids)
        return frozenset(found)

    async def of_kinds(self, kinds: Sequence[str], context: RetailerContext) -> tuple[int, ...]:
        """The best sellers of these kinds, best first within each kind."""
        if not self._enabled or not kinds:
            return ()
        return tuple(await self._repository.best_sellers(context, self._rule, kinds=kinds))
