"""A designed room once it is in the cart (CLAUDE.md 36).

"Add all to cart" makes the cart the room: the package becomes a record of how
it was designed, and what they pick next is for that room until they say they
are done with it. These read, from the session alone, which products in the
cart belong to which room - so a cart line can name its room and a reply can
say a new pick was added for it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from app.prompts.visualization.v1 import ROOM_TYPES
from app.schemas.agent_state import AgentStateV1, FinishedRoom, RoomProjectState
from app.schemas.language import ReplyLanguage
from app.schemas.visualization import RoomType
from app.services.agent_state import pick_group, room_key, room_of_pick
from app.services.media_wording import ARABIC_ROOMS
from app.taxonomy.words import customer_words_or_none


def room_words(room: RoomProjectState) -> str:
    """The room in customer words ("living room")."""
    # Room kinds are keyed with "_" (living_room), product kinds with "-".
    words = customer_words_or_none(room.room_kind or room.room_type)
    return words.replace("_", " ") if words else "room"


def is_this_room(tag: str, room_type: str | None, room_kind: str | None) -> bool:
    """Whether a cart product's room tag is the room being designed - by its
    words or its kind ("living room" and living_room are one room)."""
    names = {room_key(words) for words in (room_type, room_kind) if words}
    return room_key(tag) in names


@dataclass(frozen=True, slots=True)
class DesignPicks:
    """What a room design may take from the cart, and what to ask first."""

    takes: frozenset[int]
    ask: tuple[int, ...] = ()
    """Products whose room must be asked before the design - the piece it is
    designed around first, when another room is already in the cart."""
    rooms: Mapping[int, str | None] = field(default_factory=dict)
    """Each cart product's room as known: tagged, beside a tagged piece, or
    its kind's one room."""


def picks_for_design(
    state: AgentStateV1,
    room_type: str | None,
    room_kind: str | None,
    kinds: Mapping[int, str | None],
    room_starters: frozenset[str],
    named: frozenset[int] = frozenset(),
    one_room: Callable[[str | None], str | None] = lambda _: None,
) -> DesignPicks:
    """Which cart products a room design may take, and which it must ask
    about first (CLAUDE.md 36).

    A product's room is known from a fact: its tag, the piece it was added
    beside, or a kind that belongs to one room only (a bed, a dining chair -
    `room_pieces_v1.yaml` `one_room`). It joins only its own room. The cart is
    ambiguous when another room is in it, or two groups that each start a room
    (`room_starters`). Then the piece the design is around is asked about when
    its own room is not known - "is the sofa for your bedroom with the bed, or
    another room?" - and any other piece of unknown room joins only when it
    goes with what the design is around. One room alone in the cart asks
    nothing.
    """
    cart = state.product_interaction.selected_product_ids
    group = {pid: pick_group(state, pid) for pid in cart}
    rooms = {
        pid: room_of_pick(state, pid) or one_room(kinds.get(pid)) or one_room(kinds.get(group[pid]))
        for pid in cart
    }
    here = {pid for pid, room in rooms.items() if room and is_this_room(room, room_type, room_kind)}
    elsewhere = {pid for pid, room in rooms.items() if room and pid not in here}
    unknown = [pid for pid in cart if rooms[pid] is None]
    named_here = {pid for pid in named if pid in group}
    starting = {group[pid] for pid in cart if kinds.get(pid) in room_starters}
    if not elsewhere and len(starting) <= 1:
        return DesignPicks(frozenset(here | named_here | set(unknown)), (), rooms)
    open_named = [pid for pid in named_here if rooms[pid] is None] if elsewhere else []
    roots = {group[pid] for pid in (*named_here, *here) if pid not in open_named}
    asked = {group[pid] for pid in open_named}
    rest = [pid for pid in unknown if group[pid] not in roots and group[pid] not in asked]
    takes = here | (named_here - set(open_named))
    takes |= {pid for pid in unknown if group[pid] in roots and pid not in open_named}
    return DesignPicks(frozenset(takes), (*open_named, *rest), rooms)


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
    as a cart line shows it ("Living room") - its tag first (CLAUDE.md 36),
    then the rooms put in the cart, the one still in it last, so a product
    counted in two is named by the newer."""
    names: dict[int, str] = {}
    for product_id in state.product_interaction.selected_product_ids:
        if (tagged := room_of_pick(state, product_id)) is not None:
            names[product_id] = display_name(None, tagged, language)
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
