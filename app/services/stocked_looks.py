"""What one kind comes in at this store: its approved colours and styles.

A design direction may name only values the shop can show for that kind, so a
match never leans towards a colour nobody stocks (CLAUDE.md 12.4). Read from
the store's shelf summary, which is cached, so checking a direction costs no
query of its own.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.schemas.design import StockedLook
from app.schemas.retailer import RetailerContext
from app.services.catalog_capability import CatalogCapabilityService
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes


class StockedLooks:
    def __init__(
        self, capabilities: CatalogCapabilityService, attributes: CatalogAttributes
    ) -> None:
        self._capabilities = capabilities
        self._attributes = attributes

    async def of(self, subcategory: str, context: RetailerContext) -> StockedLook:
        """The approved colours and styles this kind is stocked in."""
        overview = await self._capabilities.overview(context)
        shelves = [s for s in overview.shelves if s.commerce_subcategory == subcategory]
        return StockedLook(
            commerce_subcategory=subcategory,
            colours=self._approved(AttributeFamily.COLOR, (c for s in shelves for c in s.colours)),
            styles=self._approved(AttributeFamily.STYLE, (t for s in shelves for t in s.styles)),
        )

    def _approved(self, family: AttributeFamily, stored: Iterable[str]) -> tuple[str, ...]:
        names = (self._attributes.canonical(family, value) for value in stored)
        return tuple(dict.fromkeys(n for n in names if n is not None))
