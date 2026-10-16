"""The store's own customer support, and the turns that hand a shopper to it.

Faysal is the assistant a reply is shown as when it is about the store itself:
its delivery, returns, warranty or payment, a problem only the store can
solve, or a shopper asking to reach it. Like every persona it is a label; the
facts behind it come from the store's documents and from the support contacts
the store's owner set on the main platform, never from a model.

Support is offered through channels. Today each channel is a link the shopper
follows - an email address, a WhatsApp chat - and a channel that hands the
conversation over itself is one more value here, with nothing else changing.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.furniture_finder import SESSION_ID_PATTERN

MAX_EMAIL_CHARS = 254
WHATSAPP_PATTERN = r"^[1-9][0-9]{7,14}$"
"""A number in international form, digits only, as the platform stores it."""
HANDOVER_MESSAGES = 6
"""How many of the shopper's own messages a request to the store carries."""
HANDOVER_MESSAGE_CHARS = 500
"""Each message is cut to this many characters; the store reads it on a phone."""


class SupportChannel(StrEnum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"


class SupportContacts(BaseModel):
    """Where this store's support can be reached. Either may be unset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    email: str | None = Field(default=None, min_length=3, max_length=MAX_EMAIL_CHARS)
    whatsapp: str | None = Field(default=None, pattern=WHATSAPP_PATTERN)

    @property
    def any(self) -> bool:
        return self.email is not None or self.whatsapp is not None


class SupportGrounding(BaseModel):
    """The store's support, offered beside this turn's reply.

    `requested` is a shopper who asked to reach support - a tapped "Contact
    support" - rather than a question about the store that offers it too.
    No contacts is a real outcome: the store has not set any, and the reply
    says so rather than invent one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contacts: SupportContacts = SupportContacts()
    requested: bool = False
    handover: bool = False
    """Whether the platform can send the shopper's request on these channels."""


class SupportChannelView(BaseModel):
    """One way to reach the store's support, as a client draws it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: SupportChannel
    label: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=1, max_length=300)
    handover: bool = False
    """Whether the platform can send the shopper's request on this channel to
    the store's team. A client asks for their number or email first; `url`
    stays the link to follow when sending is not possible."""


class SupportPresentation(BaseModel):
    """The store's support buttons beneath a reply. Built by the application."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channels: tuple[SupportChannelView, ...] = Field(min_length=1)


class ContactSupportAction(BaseModel):
    """The shopper tapped "Contact support": the store's channels, no model.

    Any part of the application may send it - a chip beside a reply, or the
    platform on a problem it could not solve - so the hand-over to the store
    does not depend on how a question was worded (CLAUDE.md 3.6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["contact_support"] = "contact_support"


SupportActionRequest = ContactSupportAction
"""Every support action a client may send. One today."""


class SupportHandoverRequest(BaseModel):
    """Which conversation the platform is handing to the store's support."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, pattern=SESSION_ID_PATTERN)
    store_id: int = Field(ge=1)


class SupportHandover(BaseModel):
    """What the store's team is told the shopper said, oldest first.

    The shopper's own messages only - never the assistant's replies, which
    the store did not write and cannot stand behind. Empty for a conversation
    that has not started or has expired.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[str, ...] = Field(default=(), max_length=HANDOVER_MESSAGES)


__all__ = [
    "HANDOVER_MESSAGES",
    "HANDOVER_MESSAGE_CHARS",
    "MAX_EMAIL_CHARS",
    "WHATSAPP_PATTERN",
    "ContactSupportAction",
    "SupportActionRequest",
    "SupportChannel",
    "SupportChannelView",
    "SupportContacts",
    "SupportGrounding",
    "SupportHandover",
    "SupportHandoverRequest",
    "SupportPresentation",
]
