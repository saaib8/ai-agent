"""The store's support channels, as buttons beneath a reply.

Built by the application from the contacts the store's owner set, never by a
model: a link a model wrote could point anywhere. Each channel is one way to
reach the store's own team. A channel that hands the conversation over itself
(a WhatsApp conversation rather than a link to one) is a new
`SupportChannel` and one more builder here; nothing that offers support
changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from urllib.parse import quote

from app.schemas.language import ReplyLanguage
from app.schemas.support import (
    SupportChannel,
    SupportChannelView,
    SupportContacts,
    SupportPresentation,
)

LABELS: Mapping[ReplyLanguage, Mapping[SupportChannel, str]] = MappingProxyType(
    {
        ReplyLanguage.EN: MappingProxyType(
            {SupportChannel.WHATSAPP: "WhatsApp", SupportChannel.EMAIL: "Email"}
        ),
        ReplyLanguage.AR: MappingProxyType(
            {SupportChannel.WHATSAPP: "واتساب", SupportChannel.EMAIL: "البريد الإلكتروني"}
        ),
    }
)
"""Reviewed by a native speaker before Arabic reaches production
(docs/arabic-replies-plan.md 3.4)."""


def channels_for(
    contacts: SupportContacts, language: ReplyLanguage = ReplyLanguage.EN
) -> SupportPresentation | None:
    """The buttons for every contact the store set, WhatsApp first; None for none."""
    labels = LABELS[language]
    channels: list[SupportChannelView] = []
    if contacts.whatsapp is not None:
        channels.append(
            SupportChannelView(
                channel=SupportChannel.WHATSAPP,
                label=labels[SupportChannel.WHATSAPP],
                url=f"https://wa.me/{contacts.whatsapp}",
            )
        )
    if contacts.email is not None:
        channels.append(
            SupportChannelView(
                channel=SupportChannel.EMAIL,
                label=labels[SupportChannel.EMAIL],
                url=f"mailto:{quote(contacts.email, safe='@.+-_')}",
            )
        )
    return SupportPresentation(channels=tuple(channels)) if channels else None


__all__ = ["LABELS", "channels_for"]
