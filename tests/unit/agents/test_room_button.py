"""The room to design, offered beside the grid after an add (CLAUDE.md 10.3)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.agents.services import AgentServices
from app.agents.tools.sales import _room_to_design
from app.schemas.agent_state import (
    AgentStateV1,
    BundleAcquisition,
    BundleItemState,
    BundleItemStatus,
    GoalKind,
    GoalState,
    PickRoom,
    ProductInteractionState,
    RoomProjectState,
)
from app.schemas.geometry import RoomGeometry, RoomMeasurement, RoomMeasurementRole
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics

from tests.unit.agents.fakes import TAXONOMY, _product

SEATING = load_seating_semantics(taxonomy=TAXONOMY)
SERVICES = cast(
    AgentServices,
    SimpleNamespace(
        taxonomy=TAXONOMY,
        seating=SEATING,
        room_pieces=load_room_pieces(taxonomy=TAXONOMY, seating=SEATING),
    ),
)
SOFA, BED, NIGHTSTAND, RUG, LAMP, CONSOLE = (
    _product(1, subcategory="sofa"),
    _product(2, subcategory="bed"),
    _product(3, subcategory="nightstand"),
    _product(4, subcategory="carpet"),
    _product(5, subcategory="floor-lamp"),
    _product(6, subcategory="console"),
)


def cart(*pieces: Any, rooms: dict[int, str] | None = None, **state: Any) -> AgentStateV1:
    return AgentStateV1(
        product_interaction=ProductInteractionState(
            selected_product_ids=tuple(p.product_id for p in pieces),
            pick_rooms=tuple(PickRoom(product_id=k, room=v) for k, v in (rooms or {}).items()),
        ),
        **state,
    )


def living_room(*pieces: Any, in_cart: bool = False) -> RoomProjectState:
    return RoomProjectState(
        room_type="living room",
        room_kind="living_room",
        bundle_items=tuple(
            BundleItemState(
                line_id=n,
                product_id=p.product_id,
                quantity=1,
                acquisition=BundleAcquisition.TO_BUY,
                status=BundleItemStatus.SUGGESTED,
            )
            for n, p in enumerate(pieces, start=1)
        ),
        next_bundle_line_id=len(pieces) + 1,
        in_cart=in_cart,
    )


def room_button(added: Any, state: AgentStateV1, *in_cart: Any) -> list[tuple[str, str | None]]:
    chips = _room_to_design(SERVICES, added, added, state, in_cart or (added,))
    return [(c.offers, c.room) for c in chips]


NOTED_SIZE = RoomProjectState(
    room_type="living room",
    geometry=RoomGeometry(
        measurements=(
            RoomMeasurement(role=RoomMeasurementRole.ROOM_LENGTH, centimetres=Decimal("500")),
            RoomMeasurement(role=RoomMeasurementRole.ROOM_WIDTH, centimetres=Decimal("400")),
        )
    ),
)


@pytest.mark.parametrize(
    ("case", "added", "state", "in_cart", "expected"),
    [
        ("a sofa, its room not known", SOFA, cart(SOFA), (SOFA,), [("whole_room", None)]),
        ("a bed is a bedroom's", BED, cart(BED), (BED,), [("whole_room", "bedroom")]),
        (
            "a nightstand belongs to a bedroom",
            NIGHTSTAND,
            cart(NIGHTSTAND),
            (NIGHTSTAND,),
            [("whole_room", "bedroom")],
        ),
        (
            "a rug added after a sofa grows the sofa's room",
            RUG,
            cart(SOFA, RUG),
            (SOFA, RUG),
            [("whole_room", None)],
        ),
        (
            "a sofa and a bed: which room is asked",
            SOFA,
            cart(BED, SOFA),
            (BED, SOFA),
            [("a_room", None)],
        ),
        (
            "a sofa placed in the living room, with a bed: still asked",
            SOFA,
            cart(BED, SOFA, rooms={1: "living room"}),
            (BED, SOFA),
            [("a_room", None)],
        ),
        (
            "a sofa they placed names its room",
            SOFA,
            cart(SOFA, rooms={1: "living room"}),
            (SOFA,),
            [("whole_room", "living room")],
        ),
        (
            "a room only noted hides nothing",
            SOFA,
            cart(SOFA, room_project=NOTED_SIZE),
            (SOFA,),
            [("whole_room", None)],
        ),
        (
            "the living room in the cart, a console after it: nothing",
            CONSOLE,
            cart(SOFA, CONSOLE, room_project=living_room(SOFA, in_cart=True)),
            (SOFA, CONSOLE),
            [],
        ),
        (
            "the living room designed, a bed added: the bedroom",
            BED,
            cart(BED, room_project=living_room(SOFA)),
            (BED,),
            [("whole_room", "bedroom")],
        ),
        (
            "the living room designed, an unplaced sofa added: nothing",
            SOFA,
            cart(SOFA, room_project=living_room(_product(9, subcategory="sofa"))),
            (SOFA,),
            [],
        ),
        (
            "a seating goal still open: nothing yet",
            SOFA,
            cart(
                SOFA,
                goals=(GoalState(goal=1, kind=GoalKind.SEATING, summary="seat ten", seats=10),),
            ),
            (SOFA,),
            [],
        ),
        ("a lamp belongs to no one room: nothing", LAMP, cart(LAMP), (LAMP,), []),
    ],
)
def test_the_room_button(
    case: str, added: Any, state: AgentStateV1, in_cart: Any, expected: list[Any]
) -> None:
    assert room_button(added, state, *in_cart) == expected, case
