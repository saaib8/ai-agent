"""The store's own cart is asked before ours holds a product (CLAUDE.md 36).

Salla's public storefront API says whether a product is the store's, in stock,
and how many one order may take. What it refuses never stays in our cart, so
the reply never says "it's in your cart" about something the store would turn
away - and a Salla that cannot be asked never blocks the cart.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
from app.core.config import SallaSettings
from app.core.exceptions import PickUnavailableError
from app.integrations.salla_storefront import SallaProduct, SallaStorefrontClient
from app.repositories.products import ProductRepository, StoreProductIdentity
from app.repositories.stores import StoreRepository
from app.schemas.cart import CartRefusalReason
from app.schemas.picks import PicksRequest, SelectPickAction, SetQuantityAction
from app.schemas.retailer import RetailerContext
from app.services.store_cart_check import CartCheck, StoreCartCheck

from tests.unit.test_cart import (
    _runtime,
    _with_cart,
)
from tests.unit.test_chat_api import FakeSessionStore
from tests.unit.test_picks import STORE, _state, _stored

CONTEXT = RetailerContext(store_id=STORE)
SALLA_STORE = 306242292


# ── Salla's storefront API ──────────────────────────────────────────────────


def _salla(handler: Any, *, ttl: int = 60, retries: int = 2) -> SallaStorefrontClient:
    return SallaStorefrontClient(
        SallaSettings(cache_ttl_s=ttl, retries=retries, retry_backoff_s=0),
        transport=httpx.MockTransport(handler),
    )


def _details(**data: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": 200, "success": True, "data": data})


async def test_an_available_product_with_a_per_order_limit() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _details(is_available=True, is_out_of_stock=False, max_quantity=3)

    answer = await _salla(handler).product(SALLA_STORE, "1228105825")

    assert answer == SallaProduct(in_store=True, available=True, max_quantity=3)
    assert seen[0].url.path.endswith("/products/1228105825/details")
    assert seen[0].headers["Store-Identifier"] == str(SALLA_STORE)


async def test_out_of_stock_and_not_in_the_store() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "111" in request.url.path:
            return _details(is_available=False, is_out_of_stock=True)
        return httpx.Response(410, json={"status": 410, "success": False})

    salla = _salla(handler)

    assert (await salla.product(SALLA_STORE, "111")) == SallaProduct(
        in_store=True, available=False, max_quantity=None
    )
    assert (await salla.product(SALLA_STORE, "222")) == SallaProduct(
        in_store=False, available=False, max_quantity=None
    )


async def test_a_salla_that_cannot_be_asked_even_again_is_unknown() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="maintenance")

    assert await _salla(down).product(SALLA_STORE, "123") is None
    assert await _salla(broken).product(SALLA_STORE, "123") is None


async def test_a_momentary_failure_is_asked_again_and_the_answer_used() -> None:
    """The screenshot: on stage the rug's answer did not come back, so it went
    in the cart though Salla refuses it. A hiccup is now asked again."""
    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        if len(calls) == 2:
            return httpx.Response(429, text="busy")
        return httpx.Response(410, text="gone")

    answer = await _salla(flaky).product(SALLA_STORE, "1351484480")

    assert answer == SallaProduct(in_store=False, available=False, max_quantity=None)
    assert len(calls) == 3


async def test_an_answer_is_never_asked_again_and_the_retries_are_bounded() -> None:
    calls: list[int] = []

    def gone(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, text="no")

    def failing(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(502, text="bad gateway")

    assert (await _salla(gone).product(SALLA_STORE, "1")).in_store is False  # type: ignore[union-attr]
    assert len(calls) == 1, "a refusal is an answer"
    calls.clear()
    assert await _salla(failing, retries=1).product(SALLA_STORE, "2") is None
    assert len(calls) == 2


async def test_a_product_is_asked_once_within_the_cache_time() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _details(is_available=True)

    salla = _salla(handler)
    await salla.product(SALLA_STORE, "123")
    await salla.product(SALLA_STORE, "123")

    assert len(calls) == 1


async def test_an_id_that_is_not_a_salla_id_is_never_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request")

    answer = await _salla(handler).product(SALLA_STORE, "../admin")

    assert answer is not None and not answer.in_store


# ── the check ───────────────────────────────────────────────────────────────


class Stores:
    def __init__(self, salla_store_id: int | None) -> None:
        self.salla_store_id_value = salla_store_id

    async def salla_store_id(self, store_id: int) -> int | None:
        return self.salla_store_id_value


class Identities:
    def __init__(self, salla_ids: dict[int, str | None]) -> None:
        self.salla_ids = salla_ids

    async def store_identities(self, product_ids: Any, context: Any) -> list[StoreProductIdentity]:
        return [
            StoreProductIdentity(
                product_id=p,
                uuid=f"uuid-{p}",
                salla_product_id=self.salla_ids[p],
                product_url=f"https://store.test/p/{p}",
                name_english=f"Product {p}",
                name_arabic=None,
            )
            for p in product_ids
            if p in self.salla_ids
        ]


class Answers:
    def __init__(self, answers: dict[str, SallaProduct | None]) -> None:
        self.answers = answers

    async def product(self, salla_store_id: int, salla_product_id: str) -> SallaProduct | None:
        return self.answers.get(salla_product_id)


def _check(
    salla_ids: dict[int, str | None],
    answers: dict[str, SallaProduct | None],
    *,
    salla_store: int | None = SALLA_STORE,
) -> StoreCartCheck:
    return StoreCartCheck(
        cast(StoreRepository, Stores(salla_store)),
        cast(ProductRepository, Identities(salla_ids)),
        cast(SallaStorefrontClient, Answers(answers)),
    )


IN_STOCK = SallaProduct(in_store=True, available=True, max_quantity=3)
GONE = SallaProduct(in_store=False, available=False, max_quantity=None)


async def test_each_reason_the_store_refuses() -> None:
    check = _check(
        {1: "11", 2: "22", 3: "33", 4: None, 5: "55"},
        {
            "11": IN_STOCK,
            "22": GONE,
            "33": SallaProduct(in_store=True, available=False, max_quantity=None),
            "55": None,
        },
    )

    refused = await check.refusals({1: 5, 2: 1, 3: 1, 4: 1, 5: 1}, CONTEXT)

    assert {pid: (c.reason, c.takes) for pid, c in refused.items()} == {
        1: (CartRefusalReason.OVER_LIMIT, 3),
        2: (CartRefusalReason.NOT_IN_STORE, 0),
        3: (CartRefusalReason.OUT_OF_STOCK, 0),
        4: (CartRefusalReason.NOT_IN_STORE, 0),
        5: (CartRefusalReason.UNCONFIRMED, 0),
    }, "5 could not be asked, so it is kept out rather than promised"


async def test_switched_off_an_unconfirmed_product_goes_in_as_before() -> None:
    check = StoreCartCheck(
        cast(StoreRepository, Stores(SALLA_STORE)),
        cast(ProductRepository, Identities({5: "55"})),
        cast(SallaStorefrontClient, Answers({"55": None})),
        refuse_unconfirmed=False,
    )

    assert await check.refusals({5: 1}, CONTEXT) == {}


async def test_a_store_not_on_salla_is_never_asked() -> None:
    check = _check({1: "11"}, {"11": GONE}, salla_store=None)

    assert await check.refusals({1: 1}, CONTEXT) == {}


# ── every way of adding ─────────────────────────────────────────────────────


class Refuses:
    """A store cart that turns away the products named, as told."""

    def __init__(self, refused: dict[int, tuple[CartRefusalReason, int, int | None]]) -> None:
        self.refused = refused
        self.asked: list[dict[int, int]] = []

    async def refusals(self, wanted: Any, context: Any) -> dict[int, CartCheck]:
        self.asked.append(dict(wanted))
        return {
            pid: CartCheck(
                product_id=pid,
                name_english=f"Product {pid}",
                name_arabic=None,
                takes=takes,
                reason=reason,
                max_quantity=limit,
                product_url=f"https://example.test/{pid}",
            )
            for pid, (reason, takes, limit) in self.refused.items()
            if pid in wanted
        }


async def test_a_tap_the_store_refuses_says_why() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=()))
    runtime = _runtime(sessions)
    runtime._store_check = Refuses({101: (CartRefusalReason.OUT_OF_STOCK, 0, None)})  # type: ignore[assignment]

    with pytest.raises(PickUnavailableError) as raised:
        await runtime.apply(
            PicksRequest(session_id="sess-1", store_id=STORE, action=SelectPickAction(ordinal=1)),
            CONTEXT,
        )

    assert raised.value.public_message == "That product is out of stock."


async def test_a_quantity_past_one_orders_limit_is_refused_by_tap() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101,)))
    runtime = _runtime(sessions)
    runtime._store_check = Refuses({101: (CartRefusalReason.OVER_LIMIT, 2, 2)})  # type: ignore[assignment]

    with pytest.raises(PickUnavailableError) as raised:
        await runtime.apply(
            PicksRequest(
                session_id="sess-1",
                store_id=STORE,
                action=SetQuantityAction(pick=1, quantity=3),
            ),
            CONTEXT,
        )

    assert "up to 2" in raised.value.public_message


# ── what a refused room says ────────────────────────────────────────────────




async def test_a_tap_salla_could_not_confirm_says_to_try_again() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=()))
    runtime = _runtime(sessions)
    runtime._store_check = Refuses({101: (CartRefusalReason.UNCONFIRMED, 0, None)})  # type: ignore[assignment]

    with pytest.raises(PickUnavailableError) as raised:
        await runtime.apply(
            PicksRequest(session_id="sess-1", store_id=STORE, action=SelectPickAction(ordinal=1)),
            CONTEXT,
        )

    assert raised.value.public_message == (
        "I couldn't check that with the store just now. Please try again."
    )
