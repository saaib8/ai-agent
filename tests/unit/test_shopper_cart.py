"""The cart is the shopper's, not one chat's (CLAUDE.md 36).

The platform names the shopper at the front of every conversation id it sends
(`<16 hex>.<the widget's id>`). Every saved turn keeps that shopper's cart, and
every conversation they load - a new chat, or one they come back to - holds it,
so "what's in my cart?" answers the same wherever they ask. An id that names no
shopper keeps its cart to itself, exactly as before.
"""

from __future__ import annotations

import pytest
from app.repositories.sessions import shopper_of
from app.schemas.agent_state import CartQuantity
from app.schemas.cart import CartQuery
from app.schemas.picks import DeselectPickAction, PicksRequest, SelectPickAction
from app.schemas.session import ShopperCart
from app.services.shopper_cart import with_shopper_cart

from tests.unit.test_cart import _runtime
from tests.unit.test_chat_api import FakeSessionStore
from tests.unit.test_picks import CONTEXT, STORE, _state, _stored

SHOPPER = "0123456789abcdef"
FIRST_CHAT = f"{SHOPPER}.web-first"
NEW_CHAT = f"{SHOPPER}.web-second"


@pytest.mark.parametrize(
    ("session_id", "shopper"),
    [
        (FIRST_CHAT, SHOPPER),
        ("web-0cc2d8b6d39b", None),  # the console, a direct call
        ("0123456789ABCDEF.web-1", None),  # the platform's keys are lowercase hex
        ("0123456789abcde.web-1", None),  # fifteen characters is not a shopper
        (SHOPPER, None),  # no chat after it
    ],
)
def test_the_shopper_is_read_off_the_front_of_the_conversation_id(
    session_id: str, shopper: str | None
) -> None:
    assert shopper_of(session_id) == shopper


# ── putting the cart into a conversation ────────────────────────────────────


def test_a_new_conversation_starts_with_the_shoppers_cart() -> None:
    cart = ShopperCart(
        selected_product_ids=(101, 102),
        cart_quantities=(CartQuantity(product_id=101, quantity=3),),
    )

    state = with_shopper_cart(_state(presented=()), cart, new_conversation=True)

    interaction = state.product_interaction
    assert interaction.selected_product_ids == (101, 102)
    assert interaction.quantity_of(101) == 3


def test_a_product_another_chat_removed_loses_the_focus_here() -> None:
    """Focused only because it was picked, and nothing on screen holds it."""
    stored = _state(picks=(104,), focus=104)

    state = with_shopper_cart(stored, ShopperCart(), new_conversation=False)

    assert state.product_interaction.selected_product_ids == ()
    assert state.product_interaction.focused_product_id is None
    assert state.product_interaction.presented_product_ids == (101, 102, 103), "the screen stays"


def test_the_same_cart_changes_nothing_and_no_cart_is_no_change() -> None:
    stored = _state(picks=(101,))

    assert (
        with_shopper_cart(stored, ShopperCart(selected_product_ids=(101,)), new_conversation=False)
        is stored
    )
    assert with_shopper_cart(stored, None, new_conversation=False) is stored


def test_a_cart_that_cannot_be_applied_leaves_the_conversation_as_it_was() -> None:
    """A quantity for a product not in the cart is refused by the state itself."""
    stored = _state(picks=(101,))
    broken = ShopperCart.model_construct(
        selected_product_ids=(102,),
        cart_quantities=(CartQuantity(product_id=999, quantity=2),),
    )

    assert with_shopper_cart(stored, broken, new_conversation=False) is stored


def test_the_cart_alone_is_the_shoppers() -> None:
    stored = _state(picks=(101,), focus=102)

    state = with_shopper_cart(
        stored, ShopperCart(selected_product_ids=(103,)), new_conversation=False
    )

    assert state.product_interaction.selected_product_ids == (103,)
    assert state.product_interaction.focused_product_id == 102, "on screen, so still in focus"
    assert state.active_search == stored.active_search


# ── across chats, end to end ────────────────────────────────────────────────


def _tick(session_id: str, ordinal: int) -> PicksRequest:
    return PicksRequest(
        session_id=session_id, store_id=STORE, action=SelectPickAction(ordinal=ordinal)
    )


def _untick(session_id: str, pick: int) -> PicksRequest:
    return PicksRequest(session_id=session_id, store_id=STORE, action=DeselectPickAction(pick=pick))


async def _cart_lines(
    sessions: FakeSessionStore, session_id: str, store_id: int = STORE
) -> list[str]:
    runtime = _runtime(sessions)
    read = await runtime.read_cart(CartQuery(session_id=session_id, store_id=store_id), CONTEXT)
    return [line.name_english for line in read.cart.lines]


async def test_a_new_chat_still_has_what_they_added() -> None:
    """The screenshot: added in one chat, a new chat said the cart was empty."""
    sessions = FakeSessionStore()
    await _stored(sessions, _state(), session_id=FIRST_CHAT)
    await _runtime(sessions).apply(_tick(FIRST_CHAT, 2), CONTEXT)

    assert await _cart_lines(sessions, NEW_CHAT) == ["Sofa 102"]


async def test_removing_it_in_the_new_chat_empties_the_first_one_too() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(), session_id=FIRST_CHAT)
    await _runtime(sessions).apply(_tick(FIRST_CHAT, 2), CONTEXT)

    # A chat opened just now: it starts with the cart, so its first pick is the sofa.
    await _runtime(sessions).apply(_untick(NEW_CHAT, 1), CONTEXT)

    assert await _cart_lines(sessions, FIRST_CHAT) == []


async def test_another_shopper_another_store_and_an_id_without_a_shopper_share_nothing() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(), session_id=FIRST_CHAT)
    await _runtime(sessions).apply(_tick(FIRST_CHAT, 1), CONTEXT)
    await _stored(sessions, _state(), session_id="web-console")
    await _runtime(sessions).apply(_tick("web-console", 3), CONTEXT)

    assert await _cart_lines(sessions, "fedcba9876543210.web-first") == []
    assert await _cart_lines(sessions, NEW_CHAT, store_id=STORE + 1) == []
    assert await _cart_lines(sessions, "web-another-console") == []
    assert await _cart_lines(sessions, "web-console") == ["Sofa 103"], "its own cart, as before"
