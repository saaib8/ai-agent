"""The only place that reads the store's support contacts.

The store's owner sets them on the platform; this service only reads them, for
the store the request is scoped to and never one a model named (CLAUDE.md 8).
A value that is not a usable address or number is left out rather than shown.

Where the store saved none, its owner's own email and phone - given when their
account was created - stand in (`customer_agent.support_owner_fallback`), each
one only where the store left that one empty. A contact from the store's
documents is never one: nobody checked it.
"""

from __future__ import annotations

import re

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.tables import account_user, core_business, core_store
from app.schemas.retailer import RetailerContext
from app.schemas.support import SupportContacts

logger = get_logger(__name__)

PHONE_SEPARATORS = re.compile(r"[\s()+.-]")


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _phone_digits(value: str | None) -> str | None:
    """An owner's phone as typed (+966 55 123 4567, 00966...) in digits only."""
    digits = PHONE_SEPARATORS.sub("", value or "")
    digits = digits[2:] if digits.startswith("00") else digits
    return digits or None


def _contacts(email: str | None, whatsapp: str | None, store_id: int) -> SupportContacts:
    """Both where both are usable; otherwise whichever one is."""
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


class SupportContactsRepository:
    def __init__(self, session: AsyncSession, *, owner_fallback: bool = False) -> None:
        self._session = session
        self._owner_fallback = owner_fallback

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
        store = _contacts(_clean(row.support_email), _clean(row.support_whatsapp), store_id)
        if not self._owner_fallback or (store.email and store.whatsapp):
            return store
        owner = await self._owner(store_id)
        return SupportContacts(
            email=store.email or owner.email,
            whatsapp=store.whatsapp or owner.whatsapp,
        )

    async def _owner(self, store_id: int) -> SupportContacts:
        """The store's owner's email and phone; nothing when they cannot be read.

        Read inside a savepoint, so a platform that does not let this service
        see its businesses or users leaves the rest of the turn untouched.
        """
        try:
            async with self._session.begin_nested():
                result = await self._session.execute(
                    select(account_user.c.email, account_user.c.phone)
                    .select_from(
                        core_store.join(
                            core_business, core_business.c.id == core_store.c.business_id
                        ).join(account_user, account_user.c.id == core_business.c.owner_id)
                    )
                    .where(core_store.c.id == store_id)
                )
                row = result.first()
        except SQLAlchemyError as exc:
            logger.warning(
                "support_owner_unavailable", store_id=store_id, error_type=type(exc).__name__
            )
            return SupportContacts()
        if row is None:
            return SupportContacts()
        email = _clean(row.email.lower() if row.email else None)
        return _contacts(email, _phone_digits(row.phone), store_id)


__all__ = ["SupportContactsRepository"]
