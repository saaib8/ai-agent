"""The store's shelf, as a designer reads it before choosing pieces.

Nora designs a room from what the shop really holds: the kinds it stocks, what
each costs from and to, how wide a choice each offers, and - for the kinds she
is weighing - the colours, styles and seats they come in. Salman reads it to
choose what goes with a piece. It is a summary of capability, never inventory
(CLAUDE.md 9): no product, no id, and no count - how much the store holds is
never said (CLAUDE.md 10.2), so the depth of a range is a word.

The room guide (`room_pieces_v1.yaml`) is a hint here, never a rule: a room may
hold any stocked kind.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from pydantic import Field, create_model

from app.agents.context import ToolContext
from app.agents.facts import plain
from app.agents.services import AgentServices
from app.agents.tools.base import ToolArgs, ToolOutcome, ZoryTool, one_of
from app.agents.tools.schemas import ToolSchemas
from app.agents.work import ToolStatus, TurnWork
from app.core.exceptions import IntegrationUnavailableError
from app.schemas.agent_persona import AgentPersona
from app.schemas.catalog_overview import SubcategoryShelf
from app.schemas.retailer import RetailerContext
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.rooms import RoomPieces
from app.taxonomy.seating import SeatingSemantics
from app.taxonomy.words import customer_words

SHELF_READERS = frozenset({AgentPersona.NORA, AgentPersona.SALMAN})
MAX_SHELF_KINDS = 14
"""Kinds looked at closely in one call - a room's pieces at most."""


_ROOM_HINT = (
    "The room being designed, for a hint of the kinds such a room often holds - a "
    "guide, never a rule. Any other room: leave it out and choose from the shelf."
)


def shelf_args(schemas: ToolSchemas) -> type[ToolArgs]:
    """The room is a closed choice of the rooms the guide knows (CLAUDE.md 17.1)."""
    rooms = schemas.room_pieces
    kinds = tuple(rooms.kinds) if rooms is not None else ()
    return cast(
        type[ToolArgs],
        create_model(
            "ShelfArgs",
            __base__=_ShelfBase,
            room_type=(
                (one_of(kinds) | None, Field(default=None, description=_ROOM_HINT))
                if kinds
                else (None, None)
            ),
        ),
    )


class _ShelfBase(ToolArgs):
    kinds: list[str] = Field(
        default_factory=list,
        max_length=MAX_SHELF_KINDS,
        description=(
            "Kinds to look at closely (their `kind`): the colours, styles and seats each "
            "comes in. Omit for the whole shelf."
        ),
    )


def shelf_tool(schemas: ToolSchemas) -> ZoryTool[Any]:
    async def read(args: Any, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        try:
            overview = await services.capabilities.overview(context.retailer)
        except IntegrationUnavailableError:
            return ToolOutcome(status=ToolStatus.REFUSED, code="catalog_unavailable")
        shelves = [s for s in overview.shelves if s.commerce_subcategory is not None]
        room_type = getattr(args, "room_type", None)
        hints = _room_hints(services.room_pieces, room_type)
        bounds = services.shelf_depth
        facts: dict[str, Any] = {}
        if overview.currency:
            facts["currency"] = overview.currency
        if not args.kinds:
            facts["families"] = _families(
                shelves, hints, bounds, sorted(services.taxonomy.categories)
            )
            if room_type and not hints and services.room_pieces is not None:
                facts["room_guide"] = "none for this room - choose from the shelf"
            return ToolOutcome(facts=facts)
        by_kind = {s.commerce_subcategory: s for s in shelves}
        taxonomy = services.taxonomy
        asked = list(dict.fromkeys(args.kinds))
        unknown = [k for k in asked if taxonomy.category_of(k) is None]
        wanted = [k for k in asked if k not in unknown]
        facts["kinds"] = [
            _close_look(by_kind[k], hints, bounds, services.attributes, services.seating)
            for k in wanted
            if k in by_kind
        ]
        missing = [k for k in wanted if k not in by_kind]
        if missing:
            facts["not_stocked"] = missing
        if unknown:
            # Not a kind at all - never "not stocked", which she might repeat.
            facts["not_approved_kinds"] = unknown
        return ToolOutcome(facts=facts)

    return ZoryTool(
        name="store_shelf",
        description=(
            "What this store stocks, as a designer reads it before choosing pieces: each "
            "family's kinds with their price range and how wide a choice each offers "
            "(range_depth), and with room_type a hint of what such a room often holds. "
            "With kinds: the colours, styles and seats each of those comes in."
        ),
        args=shelf_args(schemas),
        owners=SHELF_READERS,
        handler=read,
    )


def _families(
    shelves: Sequence[SubcategoryShelf],
    hints: frozenset[str],
    bounds: tuple[int, int],
    order: Sequence[str],
) -> list[dict[str, Any]]:
    families: list[dict[str, Any]] = []
    for category in order:
        kinds = sorted(
            (s for s in shelves if s.commerce_category == category),
            key=lambda s: -s.active_count,
        )
        if kinds:
            families.append(
                {
                    "label": customer_words(category),
                    "kinds": [_entry(s, hints, bounds) for s in kinds],
                }
            )
    return families


def _entry(
    shelf: SubcategoryShelf, hints: frozenset[str], bounds: tuple[int, int]
) -> dict[str, Any]:
    kind = shelf.commerce_subcategory or shelf.commerce_category
    entry: dict[str, Any] = {
        "label": customer_words(kind),
        "kind": kind,
        "price_from": plain(shelf.price_minimum),
        "price_to": plain(shelf.price_maximum),
        "range_depth": _depth(shelf.active_count, bounds),
    }
    if kind in hints:
        entry["in_this_room"] = True
    return entry


def _close_look(
    shelf: SubcategoryShelf,
    hints: frozenset[str],
    bounds: tuple[int, int],
    attributes: CatalogAttributes,
    seating: SeatingSemantics | None,
) -> dict[str, Any]:
    look = _entry(shelf, hints, bounds)
    look["colours"] = _canonical(attributes, AttributeFamily.COLOR, shelf.colours)
    look["styles"] = _canonical(attributes, AttributeFamily.STYLE, shelf.styles)
    if seating is not None and not seating.counts_as_seats(shelf.commerce_subcategory):
        # A room may hold one, but it never fills a seat of the head count.
        look["seats_counted"] = False
    elif shelf.seating is not None:
        look["seats_from"] = shelf.seating.minimum
        look["seats_to"] = shelf.seating.maximum
    elif shelf.implied_seats is not None:
        look["seats_each"] = shelf.implied_seats
    if shelf.long_and_shallow:
        look["long_and_shallow"] = True
    return look




async def stocked_kinds(services: AgentServices, retailer: RetailerContext) -> dict[str, Any]:
    """The kinds the store stocks, by family - names only, a few dozen words -
    so Nora plans a room from what exists without a tool call. Colours, seats
    and prices stay with the checks and the search (CLAUDE.md 10.1). Empty
    when the catalog cannot be read now."""
    try:
        overview = await services.capabilities.overview(retailer)
    except IntegrationUnavailableError:
        return {}
    families: dict[str, list[str]] = {}
    for shelf in sorted(overview.shelves, key=lambda s: -s.active_count):
        if shelf.commerce_subcategory is not None:
            families.setdefault(shelf.commerce_category, []).append(shelf.commerce_subcategory)
    return {"store_stocks": dict(sorted(families.items()))} if families else {}


def _room_hints(rooms: RoomPieces | None, room_type: str | None) -> frozenset[str]:
    """The kinds the room guide lists for this room - a hint that such a room
    often holds them, never a rank: no kind is put first or last for it."""
    template = rooms.template(room_type) if rooms is not None else None
    return frozenset(
        kind for piece in (template.pieces if template else ()) for kind in piece.types
    )


def _depth(count: int, bounds: tuple[int, int]) -> str:
    few, some = bounds
    return "few" if count <= few else "some" if count <= some else "many"


def _canonical(
    attributes: CatalogAttributes, family: AttributeFamily, stored: Sequence[str]
) -> list[str]:
    names = (attributes.canonical(family, value) for value in stored)
    return list(dict.fromkeys(n for n in names if n is not None))
