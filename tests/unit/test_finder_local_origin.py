"""A local run's finder names a website Zory approves.

The platform refuses widget calls without an approved `Origin` (strict
origins), and a local visitor has no browser to send one - so the website is
configured (`furniture_finder.local_origin`) and every call of that visitor
carries it: the sign-in, the detection and the matches.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.core.config import SessionSettings
from app.repositories.finder_photos import FinderPhotoStore
from app.schemas.furniture_finder import ShopperCredentials
from pydantic import SecretStr, ValidationError

from tests.unit.test_furniture_finder import (
    CONTEXT,
    SESSION,
    STORE,
    STORE_UUID,
    a_client,
    a_photo_file,
    a_service,
    finder_settings,
)

APPROVED = "https://stage.zory.ai"


async def sign_in(handler: Any, origin: str | None) -> ShopperCredentials:
    client = a_client(handler)
    try:
        return await client.anonymous_visitor(
            widget_key="furniture-detection",
            store_public_id=str(STORE_UUID),
            anonymous_id="ai-agent-0123456789abcdef",
            origin=origin,
        )
    finally:
        await client.close()


def signed_in(seen: list[httpx.Request]) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"tokens": {"access": "v.t.k"}, "anonymous": True})

    return handle


class TestSignIn:
    async def test_the_sign_in_names_the_configured_website(self) -> None:
        seen: list[httpx.Request] = []
        visitor = await sign_in(signed_in(seen), APPROVED)
        assert seen[0].headers["Origin"] == APPROVED
        assert visitor.origin == APPROVED

    async def test_without_one_no_origin_is_sent_as_before(self) -> None:
        seen: list[httpx.Request] = []
        visitor = await sign_in(signed_in(seen), None)
        assert "Origin" not in seen[0].headers
        assert visitor.origin is None


class TestSettings:
    def test_it_is_off_unless_set(self) -> None:
        assert finder_settings().local_origin is None

    @pytest.mark.parametrize(
        "origin", ["https://stage.zory.ai/page", "stage.zory.ai", "javascript:alert(1)"]
    )
    def test_only_a_bare_website_is_accepted(self, origin: str) -> None:
        with pytest.raises(ValidationError):
            finder_settings(local_origin=origin)


class TestLocalVisitor:
    async def test_the_visitor_signs_in_with_it_and_scans_as_it(self) -> None:
        service, parts = a_service(local_origin=APPROVED)

        await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)

        assert parts["zory"].visitor_calls[0]["origin"] == APPROVED
        assert parts["zory"].scan_calls[0][0].origin == APPROVED

    async def test_unset_the_visitor_signs_in_as_before(self) -> None:
        service, parts = a_service()

        await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)

        assert parts["zory"].visitor_calls[0]["origin"] is None


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


class TestKeptVisitor:
    async def test_a_kept_visitor_keeps_its_website(self) -> None:
        """A second photo in the chat reuses the visitor: without its website
        it would be refused, as the first one was before."""
        store = FinderPhotoStore(FakeRedis(), SessionSettings())  # type: ignore[arg-type]
        visitor = ShopperCredentials(
            token=SecretStr("v.t.k"),
            widget_key="furniture-detection",
            store_public_id=str(STORE_UUID),
            origin=APPROVED,
        )

        await store.save_visitor(STORE, SESSION, visitor, ttl_s=600)
        kept = await store.load_visitor(STORE, SESSION)

        assert kept is not None
        assert kept.origin == APPROVED
        assert kept.token.get_secret_value() == "v.t.k"
