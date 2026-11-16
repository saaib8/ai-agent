"""What a room picture draws: the room as it is now (docs/photos-and-cart-pictures-plan.md D1).

A designed room that is not in the cart yet is drawn as designed. Otherwise the
picture is of the cart, one room at a time:

- a designed room in the cart is its cart lines now - its pieces and what was
  bought for it after, without what was taken out;
- any other room is the cart's products for that room.

A product's room is known only from a fact (CLAUDE.md 36): the designed room in
the cart, a finished room, a room the customer named for it, the piece it was
added beside, or a kind that belongs to one room only. Products of no known room
can join the room being drawn when the customer chooses them.

This decides from the session alone; nothing here draws or reads the catalog.
When it cannot decide - several rooms, or which products - it says what to ask.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from app.schemas.agent_state import AgentStateV1, RoomProjectState
from app.services.agent_state import pick_group, room_key, room_of_pick
from app.services.room_cart import current_room_products, room_in_cart, room_words


@dataclass(frozen=True, slots=True)
class DrawDesign:
    """The designed room, not yet in the cart: drawn as designed."""


@dataclass(frozen=True, slots=True)
class DrawCart:
    """Cart products drawn together as one room."""

    room: str
    """The room in customer words ("living room")."""
    products: tuple[tuple[int, int], ...]
    """Each product and how many, in cart order."""
    designed: RoomProjectState | None
    """The room on record when it is this room - its size and look."""
    newly_placed: tuple[int, ...] = ()
    """Products of no known room the customer chose for this one: recorded as
    this room's."""


@dataclass(frozen=True, slots=True)
class AskRoom:
    """Which room to draw: the cart is for several, or its room is not known."""

    rooms: Mapping[str, tuple[int, ...]]
    """Each known room and its cart products."""
    unplaced: tuple[int, ...]
    """Cart products of no known room."""


@dataclass(frozen=True, slots=True)
class AskProducts:
    """Which of this room's cart products to draw."""

    room: str
    products: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class NothingToDraw:
    """No designed room and an empty cart."""


CartPicture = DrawDesign | DrawCart | AskRoom | AskProducts | NothingToDraw


def cart_rooms(
    state: AgentStateV1,
    kinds: Mapping[int, str | None],
    one_room: Callable[[str | None], str | None],
) -> tuple[dict[str, list[int]], list[int]]:
    """The cart's products by room (keyed by `room_key`), and those of no
    known room - both in cart order."""
    cart = state.product_interaction.selected_product_ids
    known: dict[int, str] = {}
    for done in state.finished_rooms:
        for pid in done.product_ids:
            known[pid] = done.room_words
    room = room_in_cart(state)
    if room is not None:
        for pid in current_room_products(state):
            known[pid] = room_words(room)
    rooms: dict[str, list[int]] = {}
    unplaced: list[int] = []
    for pid in cart:
        words = (
            room_of_pick(state, pid)
            or known.get(pid)
            or one_room(kinds.get(pid))
            or one_room(kinds.get(pick_group(state, pid)))
        )
        if words:
            rooms.setdefault(room_key(words), []).append(pid)
        else:
            unplaced.append(pid)
    return rooms, unplaced


def what_to_draw(
    state: AgentStateV1,
    kinds: Mapping[int, str | None],
    one_room: Callable[[str | None], str | None],
    *,
    room: str | None = None,
    chosen: Sequence[int] = (),
    all_of_them: bool = False,
    room_starters: frozenset[str] = frozenset(),
) -> CartPicture:
    """What `visualize_room` draws, or what to ask first.

    `room` is the room they asked for, in words; `chosen` the cart products
    they named; `all_of_them` their "all of them" to the products asked about.
    A product of no known room whose kind starts a room of its own (a sofa -
    `room_starters`) is never offered for another room unasked: the room is
    asked first.
    """
    design = state.room_project
    asked = room_key(room) if room else None
    if (
        design is not None
        and design.bundle_items
        and not design.in_cart
        and (asked is None or _is(design, asked))
    ):
        return DrawDesign()
    cart = state.product_interaction.selected_product_ids
    if not cart:
        return NothingToDraw()
    rooms, unplaced = cart_rooms(state, kinds, one_room)
    in_cart = set(cart)
    picked = [pid for pid in chosen if pid in in_cart]
    if asked is None:
        if picked:
            # The products they named settle the room: the one they share.
            named = {r for r, pids in rooms.items() if set(pids) & set(picked)}
            if len(named) == 1:
                asked = named.pop()
            elif not named:
                asked = _only(rooms)
        else:
            asked = _only(rooms)
            if asked is not None and any(kinds.get(pid) in room_starters for pid in unplaced):
                asked = None
        if asked is None:
            return AskRoom(rooms={r: tuple(p) for r, p in rooms.items()}, unplaced=tuple(unplaced))
    here = rooms.get(asked, [])
    designed = design if design is not None and _is(design, asked) else None
    in_cart_room = room_in_cart(state)
    whole_design = in_cart_room is not None and _is(in_cart_room, asked)
    if picked:
        products = picked
    elif whole_design or all_of_them:
        products = here if whole_design else [*here, *unplaced]
    else:
        candidates = [*here, *unplaced]
        if len(candidates) > 1:
            return AskProducts(room=asked, products=tuple(candidates))
        products = candidates
    if not products:
        return AskRoom(rooms={r: tuple(p) for r, p in rooms.items()}, unplaced=tuple(unplaced))
    quantities = {
        line.product_id: line.quantity for line in state.product_interaction.cart_quantities
    }
    placed = set(unplaced)
    return DrawCart(
        room=asked,
        products=tuple((pid, quantities.get(pid, 1)) for pid in cart if pid in set(products)),
        designed=designed,
        newly_placed=tuple(pid for pid in cart if pid in placed and pid in set(products)),
    )


def _only(rooms: Mapping[str, list[int]]) -> str | None:
    return next(iter(rooms)) if len(rooms) == 1 else None


def _is(room: RoomProjectState, key: str) -> bool:
    names = {room_key(words) for words in (room.room_type, room.room_kind) if words}
    return key in names
