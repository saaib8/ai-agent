"""Faysal's side of a turn: the store's own support, offered beside the reply.

Deterministic, like every service behind a persona: no model decides whether
the store can be reached or how. The contacts are the ones the store's owner
set on the platform, read for the store the request is scoped to; a turn
about the store - answered from its documents, or not - carries them, and so
does a shopper who asked to reach support. The reply never writes them: the
channels are drawn beneath it (CLAUDE.md 3.3, 20.2).
"""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import get_logger
from app.repositories.support_contacts import SupportContactsRepository
from app.schemas.retailer import RetailerContext
from app.schemas.support import SupportContacts, SupportGrounding

logger = get_logger(__name__)


class SupportService:
    """The store's support contacts for one turn. Holds no state between turns."""

    def __init__(self, contacts: SupportContactsRepository) -> None:
        self._contacts = contacts

    async def offer(self, context: RetailerContext, *, requested: bool = False) -> SupportGrounding:
        """The store's support, as this turn offers it.

        A catalog that cannot be read offers no contacts rather than failing
        the turn: the answer from the documents still stands, and the reply
        says the store has not shared a way to reach it.
        """
        try:
            contacts = await self._contacts.for_store(context)
        except SQLAlchemyError as exc:
            logger.warning(
                "support_contacts_unavailable",
                store_id=context.store_id,
                error_type=type(exc).__name__,
            )
            contacts = SupportContacts()
        logger.info(
            "support_offered",
            store_id=context.store_id,
            requested=requested,
            email=contacts.email is not None,
            whatsapp=contacts.whatsapp is not None,
        )
        return SupportGrounding(contacts=contacts, requested=requested)


__all__ = ["SupportService"]
