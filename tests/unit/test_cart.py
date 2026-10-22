"""The cart: the customer's picks with how many of each, priced fresh.

Selecting a product adds it to the cart, whatever route selected it, so the
checks here are about what the cart adds on top of the picks: quantities that
only ever name a product in it, totals summed in code per currency, the changes
a client copies into the store's own cart, and every route - a tap, a quantity,
a typed choice with a number, a seating combination, a whole room - landing in
the same place (CLAUDE.md 36).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from app.api.dependencies import picks_runtime, retailer_context_provider
from app.api.errors import register_exception_handlers
from app.api.routes.cart import router as cart_router
from app.core.config import CustomerAgentSettings
from app.core.exceptions import CartQuantityError, PickUnavailableError
from app.repositories.products import ProductRepository, StoreProductIdentity
from app.schemas.agent_state import (
    AgentStateV1,
    CartQuantity,
    ProductInteractionState,
)
from app.schemas.agent_updates import (
    AgentStateUpdate,
    ProductInteractionUpdate,
    RemoveItems,
    SetCartQuantity,
)
from app.schemas.cart import CartQuery
from app.schemas.language import ReplyLanguage
from app.schemas.picks import (
    DeselectPickAction,
    PicksRequest,
    PickView,
    SetQuantityAction,
)
from app.schemas.retailer import RetailerContext
from app.services.agent_state import apply_update, commit_search_results
from app.services.cart import CartService
from app.services.picks import PicksRuntime
from app.services.reference_resolver import ProductReferenceResolver
from app.taxonomy.attributes import load_catalog_attributes
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from tests.unit.test_chat_api import FakeRetailers, FakeSessionStore
from tests.unit.test_picks import STORE, Hydration, _state, _stored
from tests.unit.test_reference_resolver import FakeRepository, _row

CONTEXT = RetailerContext(store_id=STORE)
SESSION = "sess-1"


class Identities:
    """`ProductRepository.store_identities`, from memory."""

    def __init__(self, gone: tuple[int, ...] = ()) -> None:
        self.gone = gone

    async def store_identities(self, product_ids: Any, context: Any) -> list[StoreProductIdentity]:
        return [
            StoreProductIdentity(
                product_id=p,
                uuid=f"00000000-0000-0000-0000-{p:012d}",
                salla_product_id=str(9000 + p) if p % 2 else None,
                product_url=f"https://store.test/p/{p}",
                name_english=f"Sofa {p}",
                name_arabic="كنبة",
            )
            for p in product_ids
            if p not in self.gone
        ]


def _cart(gone: tuple[int, ...] = ()) -> CartService:
    return CartService(cast(ProductRepository, Identities(gone)))


def _pick(position: int, product_id: int, *, price: str = "2000", unit: str = "SAR") -> PickView:
    return PickView(
        pick=position,
        name_english=f"Sofa {product_id}",
        image_url=f"https://example.test/{product_id}.jpg",
        price_amount=Decimal(price),
        price_unit=unit,
        kind="sofa",
    )


def _with_cart(picks: tuple[int, ...], quantities: dict[int, int] | None = None) -> AgentStateV1:
    state = _state(picks=picks)
    return state.model_copy(
        update={
            "product_interaction": ProductInteractionState.model_validate(
                {
                    **state.product_interaction.model_dump(),
                    "cart_quantities": [
                        {"product_id": p, "quantity": q} for p, q in (quantities or {}).items()
                    ],
                }
            )
        }
    )


# ── the state ───────────────────────────────────────────────────────────────


def test_a_quantity_names_a_product_in_the_cart() -> None:
    with pytest.raises(ValidationError):
        ProductInteractionState(
            selected_product_ids=(101,),
            cart_quantities=(CartQuantity(product_id=102, quantity=2),),
        )
    with pytest.raises(ValidationError):
        ProductInteractionState(
            selected_product_ids=(101,),
            cart_quantities=(
                CartQuantity(product_id=101, quantity=2),
                CartQuantity(product_id=101, quantity=3),
            ),
        )


def test_one_is_the_default_and_never_stored() -> None:
    with pytest.raises(ValidationError):
        CartQuantity(product_id=101, quantity=1)
    interaction = ProductInteractionState(selected_product_ids=(101,))

    assert interaction.quantity_of(101) == 1
    # A session that never set a quantity is written exactly as before.
    assert "cart_quantities" not in interaction.model_dump()


def test_setting_a_quantity_and_setting_it_back_to_one() -> None:
    state = _state(picks=(101, 102))
    two = apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                cart_quantities=(SetCartQuantity(product_id=102, quantity=2),)
            )
        ),
    )
    one = apply_update(
        two,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                cart_quantities=(SetCartQuantity(product_id=102, quantity=1),)
            )
        ),
    )

    assert two.product_interaction.quantity_of(102) == 2
    assert one.product_interaction.cart_quantities == ()


def test_a_product_leaving_the_cart_takes_its_quantity_with_it() -> None:
    state = _with_cart((101, 102), {102: 3})
    removed = apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                selected_product_ids=RemoveItems(items=(102,))
            )
        ),
    )

    assert removed.product_interaction.cart_quantities == ()


def test_new_results_keep_the_cart() -> None:
    state = _with_cart((101,), {101: 4})

    after = commit_search_results(state, (201, 202))

    assert after.product_interaction.quantity_of(101) == 4


# ── pricing it ──────────────────────────────────────────────────────────────


async def test_lines_carry_quantities_line_totals_and_the_stores_own_ids() -> None:
    state = _with_cart((101, 102), {101: 2})

    cart = await _cart().view(state, (_pick(1, 101), _pick(2, 102)), CONTEXT, ReplyLanguage.EN)

    assert [(line.pick, line.quantity, line.line_total) for line in cart.lines] == [
        (1, 2, Decimal("4000")),
        (2, 1, Decimal("2000")),
    ]
    assert cart.item_count == 3
    assert [(t.price_unit, t.amount, t.display) for t in cart.totals] == [
        ("SAR", Decimal("6000"), "6,000 SAR")
    ]
    assert cart.lines[0].product.salla_id == "9101"
    assert cart.lines[1].product.salla_id is None
    assert cart.lines[0].product.url == (
        f"https://store.test/p/101?zory_click_id={cart.lines[0].product.uuid}"
    )
    assert cart.changes == (), "a plain read reports nothing changed"


async def test_totals_are_never_summed_across_currencies() -> None:
    state = _state(picks=(101, 102))
    picks = (_pick(1, 101), _pick(2, 102, price="300", unit="USD"))

    cart = await _cart().view(state, picks, CONTEXT, ReplyLanguage.EN)

    assert [(t.price_unit, t.amount) for t in cart.totals] == [
        ("SAR", Decimal("2000")),
        ("USD", Decimal("300")),
    ]


async def test_totals_are_worded_in_the_conversations_language() -> None:
    state = _state(picks=(101,))

    cart = await _cart().view(state, (_pick(1, 101),), CONTEXT, ReplyLanguage.AR)

    assert cart.totals[0].display == "2,000 ريال"


async def test_a_product_the_catalog_no_longer_returns_is_counted_not_priced() -> None:
    state = _state(picks=(101, 102))

    # The picks read left 102 out; its number stays a gap.
    cart = await _cart().view(state, (_pick(1, 101),), CONTEXT, ReplyLanguage.EN)

    assert [line.pick for line in cart.lines] == [1]
    assert cart.unavailable == 1
    assert cart.totals[0].amount == Decimal("2000")


async def test_changes_say_what_to_copy_into_the_stores_cart() -> None:
    before = _with_cart((101, 102), {102: 2})
    after = _with_cart((101, 103), {101: 3})
    picks = (_pick(1, 101), _pick(2, 103))

    cart = await _cart().view(after, picks, CONTEXT, ReplyLanguage.EN, before=before)

    assert [
        (change.product.uuid[-3:], change.quantity_before, change.quantity_after)
        for change in cart.changes
    ] == [("101", 1, 3), ("103", 0, 1), ("102", 2, 0)]


# ── the tray: quantities by tap ─────────────────────────────────────────────


def _runtime(sessions: FakeSessionStore, **settings: Any) -> PicksRuntime:
    repository = FakeRepository([_row(p) for p in (101, 102, 103, 104)])
    return PicksRuntime(
        ProductReferenceResolver(cast(ProductRepository, repository), load_catalog_attributes()),
        Hydration(),  # type: ignore[arg-type]
        sessions,  # type: ignore[arg-type]
        CustomerAgentSettings(**settings),
        cart=_cart(),
    )


def _quantity(pick: int, quantity: int) -> PicksRequest:
    return PicksRequest(
        session_id=SESSION,
        store_id=STORE,
        action=SetQuantityAction(pick=pick, quantity=quantity),
    )


async def test_a_quantity_tap_changes_the_cart_and_says_so() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=(101, 102)))

    response = await _runtime(sessions).apply(_quantity(2, 3), CONTEXT)

    assert response.cart is not None
    assert [line.quantity for line in response.cart.lines] == [1, 3]
    assert response.cart.item_count == 4
    assert [(c.quantity_before, c.quantity_after) for c in response.cart.changes] == [(1, 3)]
    assert response.goes_with is None, "a quantity is not a new pick"


async def test_the_same_quantity_commits_nothing() -> None:
    sessions = FakeSessionStore()
    envelope = await _stored(sessions, _state(picks=(101,)))

    response = await _runtime(sessions).apply(_quantity(1, 1), CONTEXT)

    assert response.session_revision == envelope.session_revision
    assert response.cart is not None and response.cart.changes == ()


async def test_more_than_the_cart_takes_is_refused() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=(101,)))

    with pytest.raises(CartQuantityError) as raised:
        await _runtime(sessions, cart_max_quantity=5).apply(_quantity(1, 6), CONTEXT)

    assert "up to 5" in raised.value.public_message


async def test_a_quantity_for_a_line_not_in_the_cart_is_refused() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=(101,)))

    with pytest.raises(PickUnavailableError):
        await _runtime(sessions).apply(_quantity(2, 2), CONTEXT)


async def test_removing_a_line_reports_it_leaving() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101, 102), {102: 2}))

    response = await _runtime(sessions).apply(
        PicksRequest(session_id=SESSION, store_id=STORE, action=DeselectPickAction(pick=2)),
        CONTEXT,
    )

    assert response.cart is not None
    assert [(c.quantity_before, c.quantity_after) for c in response.cart.changes] == [(2, 0)]


async def test_reading_a_cart_that_has_not_started_is_empty_not_an_error() -> None:
    response = await _runtime(FakeSessionStore()).read_cart(
        CartQuery(session_id="new-one", store_id=STORE), CONTEXT
    )

    assert response.session_revision == 0
    assert response.cart.lines == () and response.cart.totals == ()


# ── the route ───────────────────────────────────────────────────────────────


async def test_the_route_reads_the_cart_and_changes_nothing() -> None:
    sessions = FakeSessionStore()
    envelope = await _stored(sessions, _with_cart((101,), {101: 2}))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(cart_router, prefix="/v1")
    app.dependency_overrides[picks_runtime] = lambda: _runtime(sessions)
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        answer = await client.get("/v1/cart", params={"session_id": SESSION, "store_id": STORE})

    assert answer.status_code == 200
    body = answer.json()
    assert body["session_revision"] == envelope.session_revision
    assert body["cart"]["item_count"] == 2
    assert body["cart"]["lines"][0]["product"]["salla_id"] == "9101"
    assert "product_id" not in answer.text
    assert (await sessions.load(STORE, SESSION)) == envelope


# ── a turn of the conversation ──────────────────────────────────────────────


# ── what the reply may say ──────────────────────────────────────────────────




# ── "the first sofa" after the screen moved on ──────────────────────────────


# ── several at once, a kind alone, and asking before adding ─────────────────




# ── the cart's limit, however a product is added ────────────────────────────
