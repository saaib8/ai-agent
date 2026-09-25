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

import re

from app.core.logging import get_logger
from app.repositories.products import CatalogOverviewRow, ProductRepository
from app.schemas.catalog_overview import (
    CatalogOverview,
    SeatingSpread,
    SubcategoryShelf,
)
from app.schemas.retailer import (
    RetailerCatalogCapabilities,
    RetailerCatalogCapability,
    RetailerContext,
)
from app.taxonomy.registry import CommerceTaxonomy

logger = get_logger(__name__)

_CURRENCY_CODE = re.compile(r"\A[A-Z]{3}\Z")
"""A clean, ISO-4217-shaped currency code. Deliberately strict: it is what
separates a store that names its currency ("SAR") from one whose `price_unit`
column holds Arabic nouns, numbers or "test" (CLAUDE.md, R3)."""


class CatalogCapabilityService:
    """Live catalog -> the product types a planner may rely on."""

    def __init__(self, repository: ProductRepository, taxonomy: CommerceTaxonomy) -> None:
        self._repository = repository
        self._taxonomy = taxonomy

    async def capabilities(self, context: RetailerContext) -> RetailerCatalogCapabilities:
        """What this retailer stocks, in approved vocabulary only.

        A stored pair the registry does not approve is dropped and logged, not
        surfaced: a stale or mistaken classification must not become a
        capability claim a plan is then built on (CLAUDE.md 14.3). Dropping it
        is safe in the direction that matters - the planner proposes less, not
        something that cannot be bought.
        """
        types = await self._repository.supported_commerce_types(context)
        approved: list[RetailerCatalogCapability] = []
        rejected: list[str] = []

        for category, subcategory, active_count in types:
            if self._is_approved(category, subcategory):
                approved.append(
                    RetailerCatalogCapability(
                        commerce_category=category,
                        commerce_subcategory=subcategory,
                        active_product_count=active_count,
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

    async def overview(self, context: RetailerContext) -> CatalogOverview:
        """The store's shelf as the agent reads it before deciding a move.

        The richer companion to :meth:`capabilities`, under the same
        approved-vocabulary discipline: a pair the registry does not approve is
        dropped and logged, never surfaced as a capability a move is then built
        on (CLAUDE.md 14.3). It carries the seat ceilings, price bands and
        palettes a salesperson reasons with - so the agent can compose a bundle
        when no single piece fits, or offer the nearest type when the exact one
        is absent, instead of running a search that comes back empty.

        Not cached in this first cut: it is one grouped scan, and correctness
        comes before the cache TTL the design calls for (CLAUDE.md 9).
        """
        rows = await self._repository.catalog_overview(context)
        shelves: list[SubcategoryShelf] = []
        units: set[str] = set()
        rejected: list[str] = []

        for row in rows:
            if not self._is_approved(row.commerce_category, row.commerce_subcategory):
                rejected.append(f"{row.commerce_category}/{row.commerce_subcategory}")
                continue
            shelves.append(_shelf_from_row(row))
            units.update(row.price_units)

        if rejected:
            logger.warning(
                "catalog_overview_unapproved_pairs",
                store_id=context.store_id,
                rejected=rejected,
            )
        logger.info(
            "catalog_overview_resolved",
            store_id=context.store_id,
            taxonomy_version=self._taxonomy.version,
            shelf_count=len(shelves),
            rejected_count=len(rejected),
        )
        return CatalogOverview(
            store_id=context.store_id,
            currency=_single_currency(units),
            shelves=tuple(shelves),
        )

    def _is_approved(self, category: str, subcategory: str | None) -> bool:
        if subcategory is None:
            return self._taxonomy.is_category(category)
        return self._taxonomy.is_pair(category, subcategory)


def _shelf_from_row(row: CatalogOverviewRow) -> SubcategoryShelf:
    """One aggregation row as an agent-facing shelf.

    A seating spread is attached only when the catalog actually recorded seat
    counts for the type; where every piece has a NULL capacity the shelf carries
    no spread, which is the honest "unverified" rather than a zero (CLAUDE.md 6.2).
    """
    seating: SeatingSpread | None = None
    if row.seat_known > 0 and row.seat_minimum is not None and row.seat_maximum is not None:
        seating = SeatingSpread(
            known_count=row.seat_known,
            minimum=row.seat_minimum,
            maximum=row.seat_maximum,
        )
    return SubcategoryShelf(
        commerce_category=row.commerce_category,
        commerce_subcategory=row.commerce_subcategory,
        active_count=row.active_count,
        price_minimum=row.price_minimum,
        price_maximum=row.price_maximum,
        seating=seating,
        colours=row.colours,
    )


def _single_currency(units: set[str]) -> str | None:
    """One clean currency, or None.

    A store whose ``price_unit`` column names exactly one ISO-shaped code is
    taken at its word; a mixed or junk column yields None rather than a guess
    (CLAUDE.md 21, R3). This is the deterministic answer to the G2 gap - derive
    the currency when it is unambiguous, ask otherwise.
    """
    if len(units) != 1:
        return None
    (unit,) = units
    return unit if _CURRENCY_CODE.fullmatch(unit) else None
