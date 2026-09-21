"""Resolving catalog scope: the only way a RetailerContext comes into being."""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from app.core.exceptions import StoreNotFoundError
from app.repositories.stores import StoreRepository
from app.schemas.retailer import RetailerContext, StoreRow
from app.services.retailer_context import RetailerContextProvider
from pydantic import ValidationError


class _StubStoreRepository:
    def __init__(self, store: StoreRow | None) -> None:
        self._store = store
        self.requested: list[int] = []

    async def get_active(self, store_id: int) -> StoreRow | None:
        self.requested.append(store_id)
        return self._store


def _provider(store: StoreRow | None) -> tuple[RetailerContextProvider, _StubStoreRepository]:
    stub = _StubStoreRepository(store)
    return RetailerContextProvider(cast(StoreRepository, stub)), stub


async def test_an_active_store_resolves_to_its_scope() -> None:
    store = StoreRow(id=12, uuid=uuid4(), name_english="Baytonia", active_status=True)
    provider, stub = _provider(store)

    context = await provider.resolve(12)

    assert context == RetailerContext(store_id=12)
    assert stub.requested == [12]


async def test_an_unresolvable_store_never_produces_a_context() -> None:
    provider, _ = _provider(None)

    with pytest.raises(StoreNotFoundError) as caught:
        await provider.resolve(404)

    assert caught.value.context == {"requested_store_id": 404}


async def test_the_refusal_reveals_nothing_about_which_stores_exist() -> None:
    """Unknown and inactive stores are refused identically."""
    provider, _ = _provider(None)

    with pytest.raises(StoreNotFoundError) as caught:
        await provider.resolve(1)

    assert caught.value.public_message == "That store is not available."
    assert "1" not in caught.value.public_message


def test_context_is_immutable() -> None:
    context = RetailerContext(store_id=5)
    with pytest.raises(ValidationError):
        context.store_id = 6  # type: ignore[misc]


def test_context_rejects_a_nonsense_store_id() -> None:
    with pytest.raises(ValidationError):
        RetailerContext(store_id=0)
