"""A store that saved no support contact is reached through its owner.

The owner's email and phone were given when their account was created, so
they are real and theirs; a contact in an uploaded document is checked by
nobody (store 145 carried two sample companies' in its PDFs). So where a
store left a field empty, its owner's stands in - never a document's.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.core.config import CustomerAgentSettings
from app.repositories.support_contacts import SupportContactsRepository
from app.schemas.support import SupportContacts
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_repository_scoping import CONTEXT, _FakeResult, _sql


class _Nested:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> bool:
        return False


class _Session:
    """The store's row, then its owner's; or an error where users are not readable."""

    def __init__(self, store: SimpleNamespace, owner: SimpleNamespace | None, error: bool = False):
        self._results = [[store], [owner] if owner else []]
        self._error = error
        self.statements: list[Any] = []

    def begin_nested(self) -> _Nested:
        return _Nested()

    async def execute(self, statement: Any, *_: Any, **__: Any) -> _FakeResult:
        self.statements.append(statement)
        if self._error and len(self.statements) == 2:
            raise ProgrammingError(
                "select", {}, Exception("permission denied for table account_user")
            )
        return _FakeResult(self._results[len(self.statements) - 1])


def _store(email: str = "", whatsapp: str = "") -> SimpleNamespace:
    return SimpleNamespace(support_email=email, support_whatsapp=whatsapp)


def _owner(
    email: str = "Owner@Store.SA", phone: str | None = "+966 55 123 4567"
) -> SimpleNamespace:
    return SimpleNamespace(email=email, phone=phone)


async def _read(session: _Session, *, fallback: bool = True) -> SupportContacts:
    repository = SupportContactsRepository(cast(AsyncSession, session), owner_fallback=fallback)
    return await repository.for_store(CONTEXT)


@pytest.mark.asyncio
async def test_a_store_with_none_saved_is_reached_through_its_owner() -> None:
    session = _Session(_store(), _owner())

    assert await _read(session) == SupportContacts(email="owner@store.sa", whatsapp="966551234567")
    sql = _sql(session.statements[1])
    assert f"core_store.id = {CONTEXT.store_id}" in sql
    assert "core_business.id = core_store.business_id" in sql
    assert "account_user.id = core_business.owner_id" in sql


@pytest.mark.asyncio
async def test_what_the_store_saved_comes_first_field_by_field() -> None:
    session = _Session(_store(email="care@store.sa"), _owner())

    assert await _read(session) == SupportContacts(email="care@store.sa", whatsapp="966551234567")


@pytest.mark.asyncio
async def test_a_store_with_both_saved_never_reads_its_owner() -> None:
    session = _Session(_store("care@store.sa", "966500000000"), _owner())

    assert await _read(session) == SupportContacts(email="care@store.sa", whatsapp="966500000000")
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_an_owner_phone_without_country_code_is_left_out() -> None:
    session = _Session(_store(), _owner(phone="0551234567"))

    assert await _read(session) == SupportContacts(email="owner@store.sa")


@pytest.mark.asyncio
async def test_an_international_prefix_is_read() -> None:
    session = _Session(_store(), _owner(phone="00966-55-123-4567"))

    assert (await _read(session)).whatsapp == "966551234567"


@pytest.mark.asyncio
async def test_users_that_cannot_be_read_leave_the_stores_own() -> None:
    session = _Session(_store(email="care@store.sa"), _owner(), error=True)

    assert await _read(session) == SupportContacts(email="care@store.sa")


@pytest.mark.asyncio
async def test_no_owner_and_none_saved_is_an_empty_result() -> None:
    assert await _read(_Session(_store(), None)) == SupportContacts()


@pytest.mark.asyncio
async def test_switched_off_only_the_stores_own_are_read() -> None:
    session = _Session(_store(), _owner())

    assert await _read(session, fallback=False) == SupportContacts()
    assert len(session.statements) == 1


def test_the_fallback_is_on_by_default() -> None:
    assert CustomerAgentSettings().support_owner_fallback is True
