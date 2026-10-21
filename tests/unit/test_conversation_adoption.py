"""A guest who signs in keeps their chat, its photos and their cart.

The chat moves from the guest's name to the account's in one transaction, with
its room photo and finder photos, each keeping the time it had left; the
finder's local sign-in stays behind. Asked twice it is already moved, and an
account chat under that name is never overwritten. The guest's cart joins the
account's, a product in both keeping the larger quantity. Only the same chat,
from one shopper to another, may move, and where the platform does not hand
chats over the route refuses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from fnmatch import fnmatchcase
from typing import Any, cast

import pytest
from app.api.dependencies import conversation_adoption_service, retailer_context_provider
from app.api.errors import register_exception_handlers
from app.api.routes.sessions import router as sessions_router
from app.core.config import SessionSettings
from app.core.exceptions import InvalidRequestError, SessionConflictError
from app.repositories.conversation_moves import ConversationMoves
from app.repositories.sessions import SHOPPER_CART_KEY_TEMPLATE, SessionStore, session_key
from app.schemas.agent_state import CartQuantity
from app.schemas.retailer import RetailerContext
from app.schemas.session import ShopperCart
from app.schemas.session_adoption import AdoptionOutcome, ConversationAdoptionRequest
from app.services.conversation_adoption import ConversationAdoptionService, merged_cart
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import WatchError

from tests.unit.test_chat_api import STORE, FakeRetailers

GUEST_SHOPPER, ACCOUNT_SHOPPER = "a" * 16, "b" * 16
GUEST, ACCOUNT = f"{GUEST_SHOPPER}.web-123", f"{ACCOUNT_SHOPPER}.web-123"
PHOTO_ID = "f" * 32
CONTEXT = RetailerContext(store_id=STORE)


class MemoryRedis:
    """The commands the move and the cart use, kept in memory. `races` makes
    the next transactions abort as if another write touched a watched key."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls_ms: dict[str, int] = {}
        self.races = 0

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, px: int | None = None) -> None:
        self.values[key] = value
        self.ttls_ms[key] = px if px is not None else (ex or 0) * 1000

    async def scan_iter(self, match: str) -> AsyncIterator[str]:
        for key in list(self.values):
            if fnmatchcase(key, match):
                yield key

    def pipeline(self) -> MemoryPipeline:
        return MemoryPipeline(self)


class MemoryPipeline:
    def __init__(self, redis: MemoryRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> MemoryPipeline:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def watch(self, *_: str) -> None:
        return None

    async def get(self, key: str) -> str | None:
        return self._redis.values.get(key)

    async def exists(self, key: str) -> int:
        return int(key in self._redis.values)

    async def pttl(self, key: str) -> int:
        return self._redis.ttls_ms.get(key, -2)

    async def reset(self) -> None:
        self._queued.clear()

    def multi(self) -> None:
        self._queued.clear()

    async def set(self, key: str, value: str, px: int) -> None:
        self._queued.append(("set", (key, value, px)))

    async def delete(self, *keys: str) -> None:
        self._queued.append(("delete", keys))

    async def execute(self) -> None:
        if self._redis.races:
            self._redis.races -= 1
            self._queued.clear()
            raise WatchError("a watched key changed")
        for command, args in self._queued:
            if command == "set":
                key, value, px = args
                self._redis.values[key] = value
                self._redis.ttls_ms[key] = px
            else:
                for key in args:
                    self._redis.values.pop(key, None)
                    self._redis.ttls_ms.pop(key, None)


def _cart_key(shopper: str) -> str:
    return SHOPPER_CART_KEY_TEMPLATE.format(store_id=STORE, shopper=shopper)


def _guest_with_a_chat(redis: MemoryRedis) -> str:
    chat = session_key(STORE, GUEST)
    redis.values[chat], redis.ttls_ms[chat] = '{"session_revision": 4}', 900_000
    redis.values[f"{chat}:room_photo"], redis.ttls_ms[f"{chat}:room_photo"] = "ROOM", 800_000
    photo = f"{chat}:finder:{PHOTO_ID}"
    redis.values[photo], redis.ttls_ms[photo] = "PHOTO", 700_000
    redis.values[f"{chat}:finder-visitor"] = "LOCAL CREDENTIAL"
    return chat


def _keep_cart(redis: MemoryRedis, shopper: str, cart: ShopperCart) -> None:
    redis.values[_cart_key(shopper)] = cart.model_dump_json()


def _service(redis: MemoryRedis) -> ConversationAdoptionService:
    client = cast(Redis, redis)
    settings = SessionSettings()
    return ConversationAdoptionService(
        SessionStore(client, settings), ConversationMoves(client, settings)
    )


def _request(guest: str = GUEST, account: str = ACCOUNT) -> ConversationAdoptionRequest:
    return ConversationAdoptionRequest(store_id=STORE, from_session_id=guest, to_session_id=account)


# ── the chat ────────────────────────────────────────────────────────────────


async def test_the_chat_its_room_and_its_photos_move_keeping_their_time() -> None:
    redis = MemoryRedis()
    guest_chat = _guest_with_a_chat(redis)
    account_chat = session_key(STORE, ACCOUNT)

    answer = await _service(redis).adopt(CONTEXT, _request())

    assert answer.outcome is AdoptionOutcome.MOVED
    assert redis.values[account_chat] == '{"session_revision": 4}'
    assert redis.ttls_ms[account_chat] == 900_000
    assert redis.values[f"{account_chat}:room_photo"] == "ROOM"
    assert redis.values[f"{account_chat}:finder:{PHOTO_ID}"] == "PHOTO"
    assert redis.ttls_ms[f"{account_chat}:finder:{PHOTO_ID}"] == 700_000
    assert guest_chat not in redis.values
    assert f"{guest_chat}:room_photo" not in redis.values


async def test_the_finders_local_sign_in_stays_with_the_guest() -> None:
    redis = MemoryRedis()
    guest_chat = _guest_with_a_chat(redis)

    await _service(redis).adopt(CONTEXT, _request())

    assert redis.values[f"{guest_chat}:finder-visitor"] == "LOCAL CREDENTIAL"
    assert f"{session_key(STORE, ACCOUNT)}:finder-visitor" not in redis.values


async def test_asked_twice_the_chat_is_already_moved() -> None:
    redis = MemoryRedis()
    _guest_with_a_chat(redis)
    service = _service(redis)

    await service.adopt(CONTEXT, _request())
    again = await service.adopt(CONTEXT, _request())

    assert again.outcome is AdoptionOutcome.ALREADY_MOVED
    assert redis.values[session_key(STORE, ACCOUNT)] == '{"session_revision": 4}'


async def test_a_guest_with_no_chat_has_nothing_to_move() -> None:
    answer = await _service(MemoryRedis()).adopt(CONTEXT, _request())

    assert answer.outcome is AdoptionOutcome.NOTHING_TO_MOVE


async def test_an_account_chat_under_that_name_is_never_overwritten() -> None:
    redis = MemoryRedis()
    guest_chat = _guest_with_a_chat(redis)
    account_chat = session_key(STORE, ACCOUNT)
    redis.values[account_chat] = '{"session_revision": 1}'

    answer = await _service(redis).adopt(CONTEXT, _request())

    assert answer.outcome is AdoptionOutcome.TARGET_IN_USE
    assert redis.values[account_chat] == '{"session_revision": 1}'
    assert redis.values[guest_chat] == '{"session_revision": 4}'


async def test_a_write_during_the_move_is_tried_once_more() -> None:
    redis = MemoryRedis()
    _guest_with_a_chat(redis)
    redis.races = 1

    answer = await _service(redis).adopt(CONTEXT, _request())

    assert answer.outcome is AdoptionOutcome.MOVED


async def test_a_chat_that_keeps_changing_is_a_conflict_and_nothing_moves() -> None:
    redis = MemoryRedis()
    guest_chat = _guest_with_a_chat(redis)
    redis.races = 2

    with pytest.raises(SessionConflictError):
        await _service(redis).adopt(CONTEXT, _request())

    assert guest_chat in redis.values
    assert session_key(STORE, ACCOUNT) not in redis.values


# ── the cart ────────────────────────────────────────────────────────────────


def test_the_guests_products_follow_the_accounts_and_the_larger_quantity_stays() -> None:
    account = ShopperCart(
        selected_product_ids=(6, 9), cart_quantities=(CartQuantity(product_id=6, quantity=2),)
    )
    guest = ShopperCart(
        selected_product_ids=(5, 6), cart_quantities=(CartQuantity(product_id=6, quantity=3),)
    )

    merged, added = merged_cart(account, guest)

    assert merged.selected_product_ids == (6, 9, 5)
    assert merged.cart_quantities == (CartQuantity(product_id=6, quantity=3),)
    assert added == 1


def test_a_cart_merged_twice_is_the_same_cart() -> None:
    guest = ShopperCart(
        selected_product_ids=(5,), cart_quantities=(CartQuantity(product_id=5, quantity=2),)
    )

    once, _ = merged_cart(None, guest)
    twice, added = merged_cart(once, guest)

    assert twice == once == guest
    assert added == 0


async def test_the_guests_cart_joins_the_one_every_account_chat_loads() -> None:
    redis = MemoryRedis()
    _guest_with_a_chat(redis)
    _keep_cart(redis, GUEST_SHOPPER, ShopperCart(selected_product_ids=(5, 6)))
    _keep_cart(redis, ACCOUNT_SHOPPER, ShopperCart(selected_product_ids=(6, 9)))

    answer = await _service(redis).adopt(CONTEXT, _request())

    kept = ShopperCart.model_validate_json(redis.values[_cart_key(ACCOUNT_SHOPPER)])
    assert kept.selected_product_ids == (6, 9, 5)
    assert answer.cart_lines_added == 1
    assert redis.ttls_ms[_cart_key(ACCOUNT_SHOPPER)] == SessionSettings().shopper_cart_ttl_s * 1000


async def test_the_cart_joins_even_when_the_chat_itself_expired() -> None:
    redis = MemoryRedis()
    _keep_cart(redis, GUEST_SHOPPER, ShopperCart(selected_product_ids=(5,)))

    answer = await _service(redis).adopt(CONTEXT, _request())

    assert answer.outcome is AdoptionOutcome.NOTHING_TO_MOVE
    assert answer.cart_lines_added == 1
    assert ShopperCart.model_validate_json(
        redis.values[_cart_key(ACCOUNT_SHOPPER)]
    ).selected_product_ids == (5,)


async def test_an_empty_guest_cart_leaves_the_accounts_untouched() -> None:
    redis = MemoryRedis()
    _keep_cart(redis, ACCOUNT_SHOPPER, ShopperCart(selected_product_ids=(9,)))
    before = redis.values[_cart_key(ACCOUNT_SHOPPER)]

    answer = await _service(redis).adopt(CONTEXT, _request())

    assert answer.cart_lines_added == 0
    assert redis.values[_cart_key(ACCOUNT_SHOPPER)] == before


# ── only a hand-over ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("guest", "account"),
    [
        (GUEST, f"{GUEST_SHOPPER}.web-123"),  # the same shopper
        (GUEST, f"{ACCOUNT_SHOPPER}.web-999"),  # another chat
        ("web-123", ACCOUNT),  # a name with no shopper
    ],
)
async def test_only_the_same_chat_moves_from_one_shopper_to_another(
    guest: str, account: str
) -> None:
    redis = MemoryRedis()
    _guest_with_a_chat(redis)

    with pytest.raises(InvalidRequestError):
        await _service(redis).adopt(CONTEXT, _request(guest, account))

    assert session_key(STORE, GUEST) in redis.values


# ── the route ───────────────────────────────────────────────────────────────


def _app(service: ConversationAdoptionService | None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(sessions_router, prefix="/v1")
    app.dependency_overrides[conversation_adoption_service] = lambda: service
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()
    return app


async def test_the_route_moves_the_chat() -> None:
    redis = MemoryRedis()
    _guest_with_a_chat(redis)
    body = {"store_id": STORE, "from_session_id": GUEST, "to_session_id": ACCOUNT}

    transport = ASGITransport(app=_app(_service(redis)))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        answer = await client.post("/v1/sessions/adopt", json=body)

    assert answer.status_code == 200
    assert answer.json() == {"outcome": "moved", "cart_lines_added": 0}


async def test_the_route_refuses_where_chats_are_not_handed_over() -> None:
    body = {"store_id": STORE, "from_session_id": GUEST, "to_session_id": ACCOUNT}

    async with AsyncClient(transport=ASGITransport(app=_app(None)), base_url="http://t") as client:
        answer = await client.post("/v1/sessions/adopt", json=body)

    assert answer.status_code == 404
    assert answer.json()["error"]["code"] == "guest_handover_off"
