"""The colours, styles and materials the catalog holds - values, not products.

The one read that is not scoped to a store, on purpose: it builds the
vocabulary the agents may use (`CatalogAttributes.with_stored`), as the
taxonomy registry does, and returns no product, price or count. Which values a
store actually carries for a kind is read per store, from its shelf
(`ProductRepository.catalog_overview`), and that is all an agent is offered.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import core_product
from app.repositories.products import style_tokens
from app.taxonomy.attributes import AttributeFamily


class StoredVocabularyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def values(self) -> dict[AttributeFamily, tuple[str, ...]]:
        """Each family's distinct stored values among active products, sorted."""
        token = func.unnest(style_tokens()).label("value")
        styles = select(token).where(core_product.c.is_active.is_(True)).subquery()
        found: dict[AttributeFamily, tuple[str, ...]] = {}
        for family, statement in (
            (AttributeFamily.COLOR, _distinct(core_product.c.main_color)),
            (AttributeFamily.MATERIAL, _distinct(core_product.c.material)),
            (AttributeFamily.STYLE, select(styles.c.value).distinct()),
        ):
            rows = (await self._session.execute(statement)).scalars()
            found[family] = tuple(sorted({v.strip() for v in rows if v and v.strip()}))
        return found


def _distinct(column: Any) -> Any:
    return select(column).where(core_product.c.is_active.is_(True), column.is_not(None)).distinct()
