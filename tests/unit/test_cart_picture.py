"""What a room picture draws: the room as it is now (docs/photos-and-cart-pictures-plan.md D1)."""

from __future__ import annotations

from app.schemas.agent_state import (
    AgentStateV1,
    BundleAcquisition,
    BundleItemState,
    BundleItemStatus,
    CartQuantity,
    PickRoom,
    ProductInteractionState,
    RoomProjectState,
)
from app.services.cart_picture import (
    AskProducts,
    AskRoom,
    DrawCart,
    DrawDesign,
    NothingToDraw,
    what_to_draw,
)

SOFA, RUG, LAMP, BED, NIGHTSTAND, CONSOLE = 1, 2, 3, 4, 5, 6
KINDS = {
    SOFA: "sofa",
    RUG: "carpet",
    LAMP: "floor-lamp",
    BED: "bed",
    NIGHTSTAND: "nightstand",
    CONSOLE: "console",
}
ONE_ROOM = {"bed": "bedroom", "nightstand": "bedroom"}


def one_room(kind: str | None) -> str | None:
    return ONE_ROOM.get(kind or "")


def cart(*pids: int, rooms: dict[int, str] | None = None, **state: object) -> AgentStateV1:
    return AgentStateV1(
        product_interaction=ProductInteractionState(
            selected_product_ids=pids,
            pick_rooms=tuple(
                PickRoom(product_id=pid, room=room) for pid, room in (rooms or {}).items()
            ),
            cart_quantities=tuple(state.pop("quantities", ())),  # type: ignore[arg-type]
        ),
        **state,  # type: ignore[arg-type]
    )


def living_room(
    *pieces: int, in_cart: bool = False, before: tuple[int, ...] = ()
) -> RoomProjectState:
    return RoomProjectState(
        room_type="living room",
        room_kind="living_room",
        bundle_items=tuple(
            BundleItemState(
                line_id=n,
                product_id=pid,
                quantity=1,
                acquisition=BundleAcquisition.TO_BUY,
                status=BundleItemStatus.SUGGESTED,
            )
            for n, pid in enumerate(pieces, start=1)
        ),
        next_bundle_line_id=len(pieces) + 1,
        in_cart=in_cart,
        picks_before_cart=before,
    )


def draw(state: AgentStateV1, **asked: object) -> object:
    return what_to_draw(state, KINDS, one_room, **asked)  # type: ignore[arg-type]


def test_a_designed_room_not_in_the_cart_is_drawn_as_designed() -> None:
    assert isinstance(draw(cart(room_project=living_room(SOFA, RUG))), DrawDesign)


def test_an_empty_cart_and_no_design_draws_nothing() -> None:
    assert isinstance(draw(cart()), NothingToDraw)


def test_a_designed_room_in_the_cart_is_drawn_as_it_is_now() -> None:
    # The rug was taken out of the cart; a console was bought for the room after.
    state = cart(SOFA, CONSOLE, room_project=living_room(SOFA, RUG, in_cart=True))

    picture = draw(state)

    assert isinstance(picture, DrawCart)
    assert [pid for pid, _ in picture.products] == [SOFA, CONSOLE]
    assert picture.designed is not None


def test_one_room_with_several_products_asks_which_then_draws_all() -> None:
    state = cart(BED, NIGHTSTAND, quantities=(CartQuantity(product_id=NIGHTSTAND, quantity=2),))

    asked = draw(state)
    assert isinstance(asked, AskProducts)
    assert asked.room == "bedroom" and asked.products == (BED, NIGHTSTAND)

    picture = draw(state, all_of_them=True)
    assert isinstance(picture, DrawCart)
    assert picture.products == ((BED, 1), (NIGHTSTAND, 2))


def test_a_product_that_starts_a_room_of_its_own_asks_the_room_first() -> None:
    # The bed is a bedroom's; nobody said where the sofa goes.
    state = cart(BED, SOFA)

    asked = what_to_draw(state, KINDS, one_room, room_starters=frozenset({"sofa", "bed"}))

    assert isinstance(asked, AskRoom)
    assert dict(asked.rooms) == {"bedroom": (BED,)} and asked.unplaced == (SOFA,)


def test_two_rooms_ask_which_room_first() -> None:
    state = cart(BED, SOFA, rooms={SOFA: "living room"})

    asked = draw(state)
    assert isinstance(asked, AskRoom)
    assert dict(asked.rooms) == {"bedroom": (BED,), "living room": (SOFA,)}

    picture = draw(state, room="Living Room")
    assert isinstance(picture, DrawCart)
    assert [pid for pid, _ in picture.products] == [SOFA]


def test_products_of_no_known_room_ask_the_room_then_are_placed_in_it() -> None:
    state = cart(SOFA, RUG, LAMP)

    asked = draw(state)
    assert isinstance(asked, AskRoom)
    assert asked.unplaced == (SOFA, RUG, LAMP)

    picture = draw(state, room="living room", all_of_them=True)
    assert isinstance(picture, DrawCart)
    assert picture.newly_placed == (SOFA, RUG, LAMP)


def test_products_they_name_settle_the_room_and_what_is_drawn() -> None:
    state = cart(BED, NIGHTSTAND, SOFA, rooms={SOFA: "living room"})

    picture = draw(state, chosen=[BED])

    assert isinstance(picture, DrawCart)
    assert picture.room == "bedroom" and picture.products == ((BED, 1),)


def test_a_room_with_one_product_asks_nothing() -> None:
    picture = draw(cart(BED))

    assert isinstance(picture, DrawCart)
    assert picture.products == ((BED, 1),)


def test_naming_another_room_than_the_design_draws_the_cart() -> None:
    state = cart(BED, room_project=living_room(SOFA))

    picture = draw(state, room="bedroom")

    assert isinstance(picture, DrawCart)
    assert picture.products == ((BED, 1),)
