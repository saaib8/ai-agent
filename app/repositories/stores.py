"""The only place that issues SQL against ``core_store``.

Read-only, like the product repository. Resolving a store is what makes a
:class:`~app.schemas.retailer.RetailerContext` trustworthy, so an inactive or
unknown store must never produce one.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import core_store
from app.schemas.retailer import StoreRow

_SELECTED_COLUMNS = (
    core_store.c.id,
    core_store.c.uuid,
    core_store.c.name_english,
    core_store.c.active_status,
)


class StoreRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _active(self, store_id: int) -> Select[Any]:
        return select(*_SELECTED_COLUMNS).where(
            core_store.c.id == store_id,
            core_store.c.active_status.is_(True),
        )

    async def get_active(self, store_id: int) -> StoreRow | None:
        """The store, if it exists and is active. ``None`` otherwise."""
        result = await self._session.execute(self._active(store_id))
        row = result.first()
        if row is None:
            return None
        return StoreRow(
            id=row.id,
            uuid=row.uuid,
            name_english=row.name_english,
            active_status=row.active_status,
        )
