"""What the active retailer can actually supply.

Three things that are easy to confuse and must never be conflated
(CLAUDE.md 9.1):

* the **global taxonomy** - every product type ZORY understands, from the
  registry, independent of any retailer;
* **retailer capability** - what this store's live catalog contains, which is
  what this service answers;
* **availability for a constrained search** - whether anything survives a price
  or colour filter, which only a search can answer.

A design specialist that plans around types the retailer does not stock
produces a room nobody can buy, which is worse than no plan. So this runs
before planning, never after.

Deterministic: one scoped query and a registry check. No model, no cache.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.repositories.products import ProductRepository
from app.schemas.retailer import (
    RetailerCatalogCapabilities,
    RetailerCatalogCapability,
    RetailerContext,
)
from app.taxonomy.registry import CommerceTaxonomy

logger = get_logger(__name__)


class CatalogCapabilityService:
    """Live catalog -> the product types a planner may rely on."""

    def __init__(
        self, repository: ProductRepository, taxonomy: CommerceTaxonomy
    ) -> None:
        self._repository = repository
        self._taxonomy = taxonomy

    async def capabilities(
        self, context: RetailerContext
    ) -> RetailerCatalogCapabilities:
        """What this retailer stocks, in approved vocabulary only.

        A stored pair the registry does not approve is dropped and logged, not
        surfaced: a stale or mistaken classification must not become a
        capability claim a plan is then built on (CLAUDE.md 14.3). Dropping it
        is safe in the direction that matters - the planner proposes less, not
        something that cannot be bought.
        """
        pairs = await self._repository.supported_commerce_pairs(context)
        approved: list[RetailerCatalogCapability] = []
        rejected: list[str] = []

        for category, subcategory in pairs:
            if self._is_approved(category, subcategory):
                approved.append(
                    RetailerCatalogCapability(
                        commerce_category=category, commerce_subcategory=subcategory
                    )
                )
            else:
                rejected.append(f"{category}/{subcategory}")

        if rejected:
            logger.warning(
                "catalog_capability_unapproved_pairs",
                store_id=context.store_id,
                rejected=rejected,
            )
        logger.info(
            "catalog_capabilities_resolved",
            store_id=context.store_id,
            taxonomy_version=self._taxonomy.version,
            supported_count=len(approved),
            rejected_count=len(rejected),
        )
        return RetailerCatalogCapabilities(capabilities=tuple(approved))

    def _is_approved(self, category: str, subcategory: str | None) -> bool:
        if subcategory is None:
            return self._taxonomy.is_category(category)
        return self._taxonomy.is_pair(category, subcategory)
