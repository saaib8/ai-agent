"""Our cart follows the store's own cart (CLAUDE.md 36).

The shopper can change Salla's cart on Salla's own cart page, which we never
see. The widget reads that cart and sends it with each message
(`ChatRequest.store_cart`); before the agents start, our cart is made to match
it: what they removed leaves, quantities become the store's, and what they
added there joins - so "what's in my cart?" answers what is really there.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.api.dependencies import picks_runtime, retailer_context_provider
from app.api.errors import register_exception_handlers
from app.api.routes.cart import router as cart_router
from app.core.config import CustomerAgentSettings, SessionSettings
from app.repositories.products import ProductRepository
from app.repositories.stores import StoreRepository
from app.schemas.agent_state import MAX_CART_QUANTITY, AgentStateV1, CartQuantity
from app.schemas.cart import CartSyncRequest, StoreCartLine
from app.schemas.chat import ChatRequest
from app.schemas.retailer import RetailerContext
from app.services.chat_runtime import ChatRuntime
from app.services.picks import PicksRuntime
from app.services.reference_resolver import ProductReferenceResolver
from app.services.store_cart_mirror import StoreCartMirror, mirrored_cart
from app.taxonomy.attributes import load_catalog_attributes
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from tests.unit.test_cart import _cart
from tests.unit.test_chat_api import FakeRetailers, FakeSessionStore
from tests.unit.test_picks import SESSION, STORE, Hydration, _state, _stored
from tests.unit.test_reference_resolver import FakeRepository, _row

CONTEXT = RetailerContext(store_id=STORE)
SALLA_STORE = 306242292

# Our product id for each Salla id the store lists.
CATALOG = {"9001": 101, "9002": 102, "9003": 103, "9004": 104}


class FakeStores:
    def __init__(self, salla_store_id: int | None = SALLA_STORE) -> None:
        self._salla_store_id = salla_store_id

    async def salla_store_id(self, store_id: int) -> int | None:
        return self._salla_store_id


class FakeProducts:
    def __init__(self) -> None:
        self.asked: list[tuple[str, ...]] = []

    async def ids_for_salla_products(
        self, salla_product_ids: Sequence[str], context: RetailerContext
    ) -> dict[str, int]:
        self.asked.append(tuple(salla_product_ids))
        return {
            salla_id: CATALOG[salla_id] for salla_id in salla_product_ids if salla_id in CATALOG
        }


def _mirror(*, salla_store_id: int | None = SALLA_STORE) -> StoreCartMirror:
    return StoreCartMirror(
        cast(StoreRepository, FakeStores(salla_store_id)),
        cast(ProductRepository, FakeProducts()),
    )


def _with_cart(picks: tuple[int, ...], quantities: dict[int, int] | None = None) -> AgentStateV1:
    state = _state(picks=picks)
    lines = tuple(
        CartQuantity(product_id=product_id, quantity=quantity)
        for product_id, quantity in (quantities or {}).items()
    )
    interaction = state.product_interaction.model_copy(update={"cart_quantities": lines})
    return state.model_copy(update={"product_interaction": interaction})


def _store_cart(*lines: tuple[int, int]) -> tuple[StoreCartLine, ...]:
    return tuple(
        StoreCartLine(salla_id=salla_id, quantity=quantity) for salla_id, quantity in lines
    )


def _cart_of(state: AgentStateV1) -> list[tuple[int, int]]:
    interaction = state.product_interaction
    return [(p, interaction.quantity_of(p)) for p in interaction.selected_product_ids]


# ── the cart rebuilt to match ───────────────────────────────────────────────


def test_what_stays_keeps_its_place_and_what_was_added_there_follows() -> None:
    state = _with_cart((101, 102, 103))

    cart = mirrored_cart(state.product_interaction, {103: 1, 104: 1, 101: 1})

    # 102 was removed on the store's page; 104 was added there.
    assert cart.selected_product_ids == (101, 103, 104)


def test_quantities_are_the_stores_and_only_more_than_one_is_stored() -> None:
    state = _with_cart((101, 102), {101: 3})

    cart = mirrored_cart(state.product_interaction, {101: 1, 102: 4})

    assert cart.cart_quantities == (CartQuantity(product_id=102, quantity=4),)


def test_a_quantity_past_what_our_cart_holds_is_kept_at_the_most_it_can() -> None:
    cart = mirrored_cart(_with_cart(()).product_interaction, {101: MAX_CART_QUANTITY + 50})

    assert cart.cart_quantities == (CartQuantity(product_id=101, quantity=MAX_CART_QUANTITY),)


# ── the mirror ──────────────────────────────────────────────────────────────


async def test_products_removed_on_the_stores_page_leave_our_cart() -> None:
    state = _with_cart((101, 102, 103, 104), {101: 2})

    mirrored = await _mirror().mirrored(state, _store_cart((9001, 2), (9003, 1)), CONTEXT)

    assert _cart_of(mirrored) == [(101, 2), (103, 1)]


async def test_an_empty_store_cart_empties_ours() -> None:
    mirrored = await _mirror().mirrored(_with_cart((101, 102)), (), CONTEXT)

    assert _cart_of(mirrored) == []


async def test_a_product_added_on_the_stores_page_joins_ours() -> None:
    mirrored = await _mirror().mirrored(
        _with_cart((101,)), _store_cart((9001, 1), (9004, 3)), CONTEXT
    )

    assert _cart_of(mirrored) == [(101, 1), (104, 3)]


async def test_one_product_in_several_lines_adds_up() -> None:
    # Salla lists the same product once per option chosen (two colours).
    mirrored = await _mirror().mirrored(_with_cart(()), _store_cart((9002, 1), (9002, 2)), CONTEXT)

    assert _cart_of(mirrored) == [(102, 3)]


async def test_a_product_our_catalog_does_not_know_is_left_out() -> None:
    mirrored = await _mirror().mirrored(
        _with_cart((101,)), _store_cart((9001, 1), (5555, 1)), CONTEXT
    )

    assert _cart_of(mirrored) == [(101, 1)]


async def test_a_store_not_on_salla_keeps_its_cart() -> None:
    state = _with_cart((101, 102))

    mirrored = await _mirror(salla_store_id=None).mirrored(state, (), CONTEXT)

    assert mirrored is state


async def test_a_cart_that_already_matches_is_left_untouched() -> None:
    state = _with_cart((101, 102), {102: 2})

    mirrored = await _mirror().mirrored(state, _store_cart((9001, 1), (9002, 2)), CONTEXT)

    assert mirrored is state


async def test_the_liked_list_and_the_screen_stay_as_they_were() -> None:
    state = _with_cart((101, 102))
    interaction = state.product_interaction.model_copy(update={"liked_product_ids": (103,)})
    state = state.model_copy(update={"product_interaction": interaction})

    mirrored = await _mirror().mirrored(state, _store_cart((9001, 1)), CONTEXT)

    after = mirrored.product_interaction
    assert after.liked_product_ids == (103,)
    assert after.presented_product_ids == interaction.presented_product_ids


async def test_the_focus_on_a_product_removed_there_is_let_go() -> None:
    # In focus only because it was in the cart: once it leaves, nothing shows it.
    state = _with_cart((104,))
    interaction = state.product_interaction.model_copy(update={"focused_product_id": 104})
    state = state.model_copy(update={"product_interaction": interaction})

    mirrored = await _mirror().mirrored(state, (), CONTEXT)

    assert mirrored.product_interaction.selected_product_ids == ()
    assert mirrored.product_interaction.focused_product_id is None


# ── the request ─────────────────────────────────────────────────────────────


def test_a_request_may_carry_the_stores_cart() -> None:
    request = ChatRequest(
        session_id=SESSION,
        store_id=STORE,
        message="what's in my cart?",
        store_cart=[{"salla_id": "9001", "quantity": 2}],
    )

    assert request.store_cart == (StoreCartLine(salla_id=9001, quantity=2),)


@pytest.mark.parametrize(
    "line",
    [{"salla_id": 9001, "quantity": 0}, {"salla_id": 0, "quantity": 1}, {"salla_id": 9001}],
)
def test_a_broken_store_cart_line_is_refused(line: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ChatRequest(session_id=SESSION, store_id=STORE, message="hi", store_cart=[line])


# ── at the start of a turn ──────────────────────────────────────────────────


def _runtime(sessions: FakeSessionStore, mirror: StoreCartMirror | None) -> ChatRuntime:
    # Loading a session needs no agents: of the services, only the mirror.
    services = SimpleNamespace(store_cart_mirror=mirror)
    return ChatRuntime(cast(Any, None), cast(Any, services), cast(Any, sessions), SessionSettings())


async def test_the_turn_starts_from_the_stores_cart() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101, 102, 103)))
    request = ChatRequest(
        session_id=SESSION,
        store_id=STORE,
        message="what's in my cart?",
        store_cart=_store_cart((9003, 1)),
    )

    loaded = await _runtime(sessions, _mirror()).load_session(request, CONTEXT)

    assert _cart_of(loaded.envelope.state) == [(103, 1)]
    # Nothing is saved yet: the turn saves it, with its reply, as always.
    assert _cart_of(sessions.saved[(STORE, SESSION)].state) == [(101, 1), (102, 1), (103, 1)]


async def test_without_the_stores_cart_the_turn_starts_from_ours() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101, 102)))
    request = ChatRequest(session_id=SESSION, store_id=STORE, message="what's in my cart?")

    loaded = await _runtime(sessions, _mirror()).load_session(request, CONTEXT)

    assert _cart_of(loaded.envelope.state) == [(101, 1), (102, 1)]


async def test_switched_off_the_stores_cart_is_ignored() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101, 102)))
    request = ChatRequest(session_id=SESSION, store_id=STORE, message="hi", store_cart=())

    loaded = await _runtime(sessions, None).load_session(request, CONTEXT)

    assert _cart_of(loaded.envelope.state) == [(101, 1), (102, 1)]


# ── POST /v1/cart/sync: the widget's __CART_UPDATE__, with no turn ─────────


def _picks_runtime(sessions: FakeSessionStore, mirror: StoreCartMirror | None) -> PicksRuntime:
    return PicksRuntime(
        ProductReferenceResolver(
            cast(ProductRepository, FakeRepository([_row(p) for p in (101, 102, 103, 104)])),
            load_catalog_attributes(),
        ),
        cast(Any, Hydration()),
        cast(Any, sessions),
        CustomerAgentSettings(),
        cart=_cart(),
        store_cart_mirror=mirror,
    )


def _sync(*lines: tuple[int, int]) -> CartSyncRequest:
    return CartSyncRequest(session_id=SESSION, store_id=STORE, store_cart=_store_cart(*lines))


async def test_a_sync_saves_the_stores_cart_and_answers_it() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101, 102, 103)))

    response = await _picks_runtime(sessions, _mirror()).sync_cart(_sync((9002, 2)), CONTEXT)

    assert [(line.pick, line.quantity) for line in response.cart.lines] == [(1, 2)]
    assert response.session_revision == 2
    assert _cart_of(sessions.saved[(STORE, SESSION)].state) == [(102, 2)]


async def test_a_sync_reports_no_changes_so_nothing_is_copied_back() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101,)))

    response = await _picks_runtime(sessions, _mirror()).sync_cart(_sync((9004, 1)), CONTEXT)

    assert response.cart.changes == ()


async def test_a_sync_leaves_the_conversations_words_as_they_were() -> None:
    sessions = FakeSessionStore()
    before = await _stored(sessions, _with_cart((101, 102)))

    await _picks_runtime(sessions, _mirror()).sync_cart(_sync((9001, 1)), CONTEXT)

    assert sessions.saved[(STORE, SESSION)].conversation == before.conversation


async def test_a_sync_that_changes_nothing_saves_nothing() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101,)))
    saves = sessions.saves

    response = await _picks_runtime(sessions, _mirror()).sync_cart(_sync((9001, 1)), CONTEXT)

    assert sessions.saves == saves
    assert response.session_revision == 1


async def test_a_sync_before_the_first_message_starts_the_conversation_with_it() -> None:
    sessions = FakeSessionStore()

    response = await _picks_runtime(sessions, _mirror()).sync_cart(_sync((9003, 1)), CONTEXT)

    assert response.session_revision == 1
    assert _cart_of(sessions.saved[(STORE, SESSION)].state) == [(103, 1)]


async def test_a_sync_that_loses_the_race_to_a_turn_is_tried_again_on_top_of_it() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101, 102)))
    real_save = sessions.save_if_revision
    lost = {"once": False}

    async def a_turn_saves_first(store_id: int, session_id: str, **kwargs: Any) -> bool:
        if not lost["once"]:
            # A chat turn adds 103 and saves first; this sync's save must fail.
            lost["once"] = True
            stored = sessions.saved[(store_id, session_id)]
            turn = stored.advanced(
                state=_with_cart((101, 102, 103)), conversation=stored.conversation
            )
            await real_save(store_id, session_id, expected_revision=1, envelope=turn)
            return False
        return await real_save(store_id, session_id, **kwargs)

    sessions.save_if_revision = a_turn_saves_first  # type: ignore[method-assign]

    response = await _picks_runtime(sessions, _mirror()).sync_cart(_sync((9003, 1)), CONTEXT)

    assert [line.pick for line in response.cart.lines] == [1]
    assert _cart_of(sessions.saved[(STORE, SESSION)].state) == [(103, 1)]
    assert response.session_revision == 3


async def test_switched_off_a_sync_only_reads_the_cart() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101, 102)))

    response = await _picks_runtime(sessions, None).sync_cart(_sync(), CONTEXT)

    assert len(response.cart.lines) == 2


async def test_the_sync_route_answers_the_cart() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101, 102)))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(cart_router, prefix="/v1")
    app.dependency_overrides[picks_runtime] = lambda: _picks_runtime(sessions, _mirror())
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        ok = await client.post(
            "/v1/cart/sync",
            json={
                "session_id": SESSION,
                "store_id": STORE,
                "store_cart": [{"salla_id": 9001, "quantity": 3}],
            },
        )
        broken = await client.post(
            "/v1/cart/sync",
            json={"session_id": SESSION, "store_id": STORE, "store_cart": [{"salla_id": "x"}]},
        )

    assert ok.status_code == 200
    assert [line["quantity"] for line in ok.json()["cart"]["lines"]] == [3]
    assert broken.status_code == 422
