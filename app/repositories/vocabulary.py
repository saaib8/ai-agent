"""The vocabulary, read from the database - values, never products.

`VocabularyRepository.tables` reads the families, the kinds filed under each,
the colours, styles and materials, each with its Arabic name, and the values
still waiting for one (`vocabulary_pending`) - the backend's tables and nothing
else, never the products (`app/taxonomy/tables.py`, CLAUDE.md 6.2, 12.4, 14.1).

It is not scoped to a store, on purpose: they build the vocabulary
the agents may use and return no product, price or count. Which values a store
carries for a kind is read per store, from its shelf
(`ProductRepository.catalog_overview`), and that is all an agent is offered.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import (
    commerce_category,
    commerce_subcategory,
    product_attribute_value,
    vocabulary_pending,
)
from app.taxonomy.tables import (
    FAMILY_OF_KIND,
    FamilyRow,
    KindRow,
    ValueRow,
    VocabularyTables,
    WaitingRow,
)


class VocabularyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def tables(self) -> VocabularyTables:
        """Every row, oldest first, and the values products carry."""
        families = (
            await self._session.execute(
                select(
                    commerce_category.c.id,
                    commerce_category.c.name,
                    commerce_category.c.name_arabic,
                ).order_by(commerce_category.c.id)
            )
        ).all()
        family_of = {row.id: row.name for row in families}
        kinds = (
            await self._session.execute(
                select(
                    commerce_subcategory.c.name,
                    commerce_subcategory.c.name_arabic,
                    commerce_subcategory.c.category_id,
                ).order_by(commerce_subcategory.c.id)
            )
        ).all()
        values = (
            await self._session.execute(
                select(
                    product_attribute_value.c.kind,
                    product_attribute_value.c.name,
                    product_attribute_value.c.name_arabic,
                )
                .where(product_attribute_value.c.kind.in_(tuple(FAMILY_OF_KIND)))
                .order_by(product_attribute_value.c.id)
            )
        ).all()
        waiting = (
            await self._session.execute(
                select(
                    vocabulary_pending.c.kind,
                    vocabulary_pending.c.name,
                    vocabulary_pending.c.category,
                ).order_by(vocabulary_pending.c.id)
            )
        ).all()
        return VocabularyTables(
            families=tuple(FamilyRow(name=r.name, arabic=r.name_arabic or "") for r in families),
            kinds=tuple(
                KindRow(
                    name=r.name,
                    family=family_of.get(r.category_id),
                    arabic=r.name_arabic or "",
                )
                for r in kinds
            ),
            values=tuple(
                ValueRow(kind=r.kind, name=r.name, arabic=r.name_arabic or "") for r in values
            ),
            waiting=tuple(
                WaitingRow(kind=r.kind, name=r.name, family=r.category or "") for r in waiting
            ),
        )
