"""The store's support contacts: read for the scoped store, drawn as buttons."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from app.core.exceptions import CatalogSchemaError
from app.db.catalog_schema import verify_support_schema
from app.repositories.support_contacts import SupportContactsRepository
from app.schemas.language import ReplyLanguage
from app.schemas.support import SupportChannel, SupportContacts
from app.services.support_channels import channels_for
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from tests.unit.test_repository_scoping import CONTEXT, STORE_ID, RecordingSession, _sql


def _row(email: str | None, whatsapp: str | None) -> SimpleNamespace:
    return SimpleNamespace(support_email=email, support_whatsapp=whatsapp)


async def _read(*rows: SimpleNamespace) -> tuple[SupportContacts, str]:
    session = RecordingSession(rows)
    contacts = await SupportContactsRepository(cast(AsyncSession, session)).for_store(CONTEXT)
    return contacts, _sql(session.statements[0])


@pytest.mark.asyncio
async def test_the_contacts_are_read_for_the_active_scoped_store_only() -> None:
    contacts, sql = await _read(_row("support@store.sa", "966551234567"))

    assert contacts == SupportContacts(email="support@store.sa", whatsapp="966551234567")
    assert f"core_store.id = {STORE_ID}" in sql
    assert "core_store.active_status IS true" in sql


@pytest.mark.asyncio
async def test_none_set_is_an_empty_result() -> None:
    assert (await _read(_row("", "")))[0] == SupportContacts()
    assert (await _read())[0] == SupportContacts()


@pytest.mark.asyncio
async def test_an_unusable_value_is_left_out_and_the_other_kept() -> None:
    contacts, _ = await _read(_row("support@store.sa", "0551234567"))

    assert contacts == SupportContacts(email="support@store.sa")


def test_each_contact_is_a_button_whatsapp_first() -> None:
    support = channels_for(SupportContacts(email="help@store.sa", whatsapp="966551234567"))

    assert support is not None
    assert [c.channel for c in support.channels] == [SupportChannel.WHATSAPP, SupportChannel.EMAIL]
    assert support.channels[0].url == "https://wa.me/966551234567"
    assert support.channels[1].url == "mailto:help@store.sa"


def test_buttons_are_worded_in_the_reply_language() -> None:
    support = channels_for(SupportContacts(whatsapp="966551234567"), ReplyLanguage.AR)

    assert support is not None
    assert support.channels[0].label == "واتساب"


def test_no_contacts_draws_no_buttons() -> None:
    assert channels_for(SupportContacts()) is None


def test_buttons_take_the_shoppers_request_only_where_handover_is_on() -> None:
    contacts = SupportContacts(email="help@store.sa", whatsapp="966551234567")
    plain = channels_for(contacts)
    handover = channels_for(contacts, handover=True)

    assert plain is not None and handover is not None
    assert [c.handover for c in plain.channels] == [False, False]
    assert [c.handover for c in handover.channels] == [True, True]
    assert handover.channels[0].url == "https://wa.me/966551234567"


class _Columns:
    def __init__(self, *names: str) -> None:
        self._names = names

    async def execute(self, *_: object, **__: object) -> list[tuple[str]]:
        return [(name,) for name in self._names]


@pytest.mark.asyncio
async def test_startup_refuses_a_catalog_without_the_support_columns() -> None:
    with pytest.raises(CatalogSchemaError) as raised:
        await verify_support_schema(cast(AsyncConnection, _Columns("support_email")))

    assert raised.value.context["missing_columns"] == ["support_whatsapp"]
    complete = _Columns("support_email", "support_whatsapp")
    await verify_support_schema(cast(AsyncConnection, complete))
