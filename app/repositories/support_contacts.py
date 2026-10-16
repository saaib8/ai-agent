"""The only place that reads the store's support contacts from ``core_store``.

The store's owner sets them on the platform; this service only reads them, for
the store the request is scoped to and never one a model named (CLAUDE.md 8).
A value that is not a usable address or number is left out rather than shown.
"""

from __future__ import annotations

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.tables import core_store
from app.schemas.retailer import RetailerContext
from app.schemas.support import SupportContacts

logger = get_logger(__name__)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


class SupportContactsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_store(self, context: RetailerContext) -> SupportContacts:
        """The active store's support contacts; none set is an empty result."""
        store_id = context.store_id
        result = await self._session.execute(
            select(core_store.c.support_email, core_store.c.support_whatsapp).where(
                core_store.c.id == store_id,
                core_store.c.active_status.is_(True),
            )
        )
        row = result.first()
        if row is None:
            return SupportContacts()
        email, whatsapp = _clean(row.support_email), _clean(row.support_whatsapp)
        try:
            return SupportContacts(email=email, whatsapp=whatsapp)
        except ValidationError:
            logger.warning("support_contacts_unreadable", store_id=store_id)
        try:
            return SupportContacts(email=email)
        except ValidationError:
            pass
        try:
            return SupportContacts(whatsapp=whatsapp)
        except ValidationError:
            return SupportContacts()


__all__ = ["SupportContactsRepository"]
