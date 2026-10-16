"""What the store's support is told when a shopper's request is sent to it.

The platform sends the request - to the store's own WhatsApp or email - and
asks here what the shopper said, so the store's team does not have to ask
again. Read-only and deterministic: no model summarises it, and nothing in the
session changes. The session is read for the scoped store only, so one store
can never be handed another's conversation (CLAUDE.md 8, 19).
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.repositories.sessions import SessionStore
from app.schemas.conversation import ConversationContext, ConversationRole
from app.schemas.retailer import RetailerContext
from app.schemas.support import HANDOVER_MESSAGE_CHARS, HANDOVER_MESSAGES, SupportHandover

logger = get_logger(__name__)


def recent_messages(conversation: ConversationContext) -> tuple[str, ...]:
    """The shopper's last messages, oldest first, each cut to a phone's read."""
    said = [m.content for m in conversation.messages if m.role is ConversationRole.USER]
    return tuple(_cut(text) for text in said[-HANDOVER_MESSAGES:])


def _cut(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= HANDOVER_MESSAGE_CHARS:
        return text
    return text[: HANDOVER_MESSAGE_CHARS - 1].rstrip() + "…"


class SupportHandoverService:
    """One conversation's request for the store's support. Holds no state."""

    def __init__(self, sessions: SessionStore) -> None:
        self._sessions = sessions

    async def handover(self, context: RetailerContext, session_id: str) -> SupportHandover:
        stored = await self._sessions.load(context.store_id, session_id)
        messages = recent_messages(stored.conversation) if stored is not None else ()
        logger.info(
            "support_handover_read",
            store_id=context.store_id,
            found=stored is not None,
            messages=len(messages),
        )
        return SupportHandover(messages=messages)


__all__ = ["SupportHandoverService", "recent_messages"]
