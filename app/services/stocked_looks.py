"""What one kind comes in at this store: its approved colours and styles.

A design direction may name only values the shop can show for that kind, so a
match never leans towards a colour nobody stocks (CLAUDE.md 12.4).
"""

from __future__ import annotations

from app.repositories.products import ProductRepository
from app.schemas.design import StockedLook
from app.schemas.retailer import RetailerContext
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes


class StockedLooks:
    def __init__(self, repository: ProductRepository, attributes: CatalogAttributes) -> None:
        self._repository = repository
        self._attributes = attributes

    async def of(self, subcategory: str, context: RetailerContext) -> StockedLook:
        """The approved colours and styles this kind is stocked in, most
        stocked first."""
        facts = await self._repository.brief_facts((subcategory,), context)
        return StockedLook(
            commerce_subcategory=subcategory,
            colours=self._approved(AttributeFamily.COLOR, facts.colors),
            styles=self._approved(AttributeFamily.STYLE, facts.styles),
        )

    def _approved(
        self, family: AttributeFamily, counted: tuple[tuple[str, int], ...]
    ) -> tuple[str, ...]:
        names = (self._attributes.canonical(family, value) for value, _ in counted)
        return tuple(dict.fromkeys(n for n in names if n is not None))
