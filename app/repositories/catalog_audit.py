"""Catalog-wide, read-only reads for operational auditing.

Deliberately separate from :mod:`app.repositories.products`. Those queries are
store-scoped because they serve customers; these deliberately are not, because
an audit asks what the whole catalog contains. Keeping them apart means the
customer-facing repository keeps its invariant that every query carries a
``store_id`` predicate.

Nothing here may be reached from a customer request path. It returns taxonomy
tokens and counts only - never product rows.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import core_product


class CommercePairCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str
    subcategory: str | None
    product_count: int
    active_count: int


class CommerceCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_products: int
    classified_products: int
    null_category_count: int
    null_subcategory_count: int


class CatalogAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def distinct_commerce_pairs(self) -> list[CommercePairCount]:
        """Every distinct non-null category, with its subcategory and counts."""
        active_count = func.count().filter(core_product.c.is_active.is_(True))
        statement = (
            select(
                core_product.c.commerce_category,
                core_product.c.commerce_subcategory,
                func.count().label("product_count"),
                active_count.label("active_count"),
            )
            .where(core_product.c.commerce_category.is_not(None))
            .group_by(
                core_product.c.commerce_category, core_product.c.commerce_subcategory
            )
            .order_by(
                core_product.c.commerce_category, core_product.c.commerce_subcategory
            )
        )
        result = await self._session.execute(statement)
        return [
            CommercePairCount(
                category=row.commerce_category,
                subcategory=row.commerce_subcategory,
                product_count=row.product_count,
                active_count=row.active_count,
            )
            for row in result
        ]

    async def commerce_coverage(self) -> CommerceCoverage:
        """How much of the catalog carries reviewed commerce classification."""
        statement = select(
            func.count().label("total"),
            func.count(core_product.c.commerce_category).label("classified"),
            func.count()
            .filter(core_product.c.commerce_category.is_(None))
            .label("null_category"),
            func.count()
            .filter(core_product.c.commerce_subcategory.is_(None))
            .label("null_subcategory"),
        ).select_from(core_product)
        row = (await self._session.execute(statement)).one()
        return CommerceCoverage(
            total_products=row.total,
            classified_products=row.classified,
            null_category_count=row.null_category,
            null_subcategory_count=row.null_subcategory,
        )
