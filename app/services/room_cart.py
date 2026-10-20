"""A designed room once it is in the cart (CLAUDE.md 36).

"Add all to cart" makes the cart the room: the package becomes a record of how
it was designed, and what they pick next is for that room until they say they
are done with it. These read, from the session alone, which products in the
cart belong to which room - so a cart line can name its room and a reply can
say a new pick was added for it.
"""

from __future__ import annotations

from app.prompts.visualization.v1 import ROOM_TYPES
from app.schemas.agent_state import AgentStateV1, FinishedRoom, RoomProjectState
from app.schemas.language import ReplyLanguage
from app.schemas.visualization import RoomType
from app.services.media_wording import ARABIC_ROOMS
from app.taxonomy.words import customer_words_or_none


def room_words(room: RoomProjectState) -> str:
    """The room in customer words ("living room")."""
    return customer_words_or_none(room.room_kind or room.room_type) or "room"


def room_in_cart(state: AgentStateV1) -> RoomProjectState | None:
    """The room on record, when its pieces went into the cart."""
    room = state.room_project
    return room if room is not None and room.in_cart else None


def current_room_products(state: AgentStateV1) -> frozenset[int]:
    """The products in the cart that are the room in it: its own pieces, and
    whatever was picked after it went in."""
    room = room_in_cart(state)
    if room is None:
        return frozenset()
    pieces = {line.product_id for line in room.bundle_items}
    before = set(room.picks_before_cart)
    later = {
        product_id
        for product_id in state.product_interaction.selected_product_ids
        if product_id not in before
    }
    return frozenset(pieces | later)


def picked_for_room(before: AgentStateV1, after: AgentStateV1) -> str | None:
    """The room a pick made this turn is for, in customer words - a product
    that entered the cart while the room was in it and is not one of its own
    pieces. None when the turn picked nothing for a room."""
    room = room_in_cart(after)
    if room is None or not room_in_cart(before):
        return None
    pieces = {line.product_id for line in room.bundle_items}
    earlier = set(before.product_interaction.selected_product_ids)
    added = [
        product_id
        for product_id in after.product_interaction.selected_product_ids
        if product_id not in earlier and product_id not in pieces
    ]
    return room_words(room) if added else None


def finished(state: AgentStateV1) -> FinishedRoom | None:
    """The room in the cart, as a finished room - None when there is none."""
    room = room_in_cart(state)
    if room is None:
        return None
    in_cart = set(state.product_interaction.selected_product_ids)
    return FinishedRoom(
        room_kind=room.room_kind,
        room_words=room_words(room),
        product_ids=tuple(p for p in sorted(current_room_products(state)) if p in in_cart),
    )


def room_names(state: AgentStateV1, language: ReplyLanguage) -> dict[int, str]:
    """Each product in the cart that belongs to a room, and that room's name
    as a cart line shows it ("Living room") - the room still in the cart last,
    so a product counted in two is named by the newer."""
    names: dict[int, str] = {}
    for done in state.finished_rooms:
        name = display_name(done.room_kind, done.room_words, language)
        names.update(dict.fromkeys(done.product_ids, name))
    room = room_in_cart(state)
    if room is not None:
        name = display_name(room.room_kind, room_words(room), language)
        names.update(dict.fromkeys(current_room_products(state), name))
    return names


def display_name(kind: str | None, words: str, language: ReplyLanguage) -> str:
    """A room's name as a label shows it, in the reply's language."""
    try:
        room_type = RoomType(kind) if kind is not None else None
    except ValueError:
        room_type = None
    if room_type is None:
        return words[:1].upper() + words[1:]
    if language is ReplyLanguage.AR:
        return ARABIC_ROOMS[room_type]
    return ROOM_TYPES[room_type][0]
