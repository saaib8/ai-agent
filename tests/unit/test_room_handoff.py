"""A room starts from what shopping already learned
(docs/designer-led-shopping-plan.md, phase 6).

Someone who has been shopping for a sofa for the four of them, in beige, for a
300 cm wall, and then asks to design the living room, is asked only what is
new - the budget. The head count, the colours and styles they said and the wall
are the room's; what was only learned from likes and picks is not, and picks
join only when they ask.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.schemas.agent_state import ActiveSearchState, ProductInteractionState
from app.schemas.discovery import ProductSearchRequest, SeatingCapacityConstraint
from app.schemas.geometry import RoomMeasurementRole
from app.schemas.query import ConstraintSemantics, RankingLean
from app.schemas.room_opener import RoomQuestionKind
from app.taxonomy.attributes import AttributeFamily

from tests.unit.test_room_questions_turn import (
    BEIGE,
    _handoff,
    _parts,
    _room,
)
from tests.unit.test_turn_coordinator import _state, _turn

SOFAS = ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa")


def _shopped(
    *,
    seat_preference: int | None = 4,
    capacity: int | None = None,
    wall: str | None = "300",
    said: tuple[Any, ...] = (BEIGE,),
    room: Any = None,
) -> Any:
    search = ActiveSearchState(
        request=SOFAS.model_copy(
            update={
                "seating_capacity": SeatingCapacityConstraint(min_capacity=capacity)
                if capacity
                else None
            }
        ),
        semantics=ConstraintSemantics(),
        seat_preference=seat_preference,
        lean=RankingLean(space_cm=Decimal(wall)) if wall else None,
        revision=1,
    )
    state = _state(room=room if room is not None else _room())
    return state.model_copy(
        update={
            "active_search": search,
            "customer_preferences": state.customer_preferences.model_copy(
                update={"semantic_preferences": said}
            ),
        }
    )


async def test_the_room_keeps_what_shopping_learned_and_asks_only_the_budget() -> None:
    coordinator, parts = _parts(decision=_handoff(), room_handoff=True)

    result = await coordinator.run(_turn(_shopped(), "now design my living room"))

    room = result.state.room_project
    assert room is not None
    assert room.regular_seating_count == 4
    assert [p.canonical_value for p in room.design_preferences] == ["Beige"]
    assert room.geometry is not None
    (wall,) = room.geometry.of(RoomMeasurementRole.USABLE_WALL)
    assert wall.centimetres == Decimal("300")
    assert result.room_question is not None
    assert result.room_question.kind is RoomQuestionKind.BUDGET
    assert result.room_carried is not None
    assert (result.room_carried.seats, result.room_carried.colours) == (4, ("Beige",))
    assert result.room_carried.wall == Decimal("300")
    assert parts["design"].requests == []


async def test_a_seat_count_from_a_search_is_the_rooms_head_count_unasked() -> None:
    coordinator, _ = _parts(decision=_handoff(), room_handoff=True)
    state = _shopped(seat_preference=None, capacity=9, wall=None, said=())

    result = await coordinator.run(_turn(state, "design the living room"))

    assert result.state.room_project is not None
    assert result.state.room_project.regular_seating_count == 9


async def test_what_the_room_already_has_is_never_overwritten() -> None:
    from app.schemas.query import ConstraintStrength, SemanticPreference

    grey = SemanticPreference(
        family=AttributeFamily.COLOR,
        raw_value="grey",
        canonical_value="Grey",
        strength=ConstraintStrength.PREFERRED,
    )
    coordinator, _ = _parts(decision=_handoff(), room_handoff=True)
    state = _shopped(room=_room(regular_seating_count=6, design_preferences=(grey,)))

    result = await coordinator.run(_turn(state, "back to the room"))

    room = result.state.room_project
    assert room is not None and room.regular_seating_count == 6
    assert [p.canonical_value for p in room.design_preferences] == ["Grey"]


async def test_picks_join_only_when_they_ask() -> None:
    coordinator, _ = _parts(decision=_handoff(), room_handoff=True)
    state = _shopped().model_copy(
        update={"product_interaction": ProductInteractionState(selected_product_ids=(20,))}
    )

    result = await coordinator.run(_turn(state, "design my living room"))

    assert result.state.room_project is not None
    assert result.state.room_project.anchor_product_ids == ()


async def test_switched_off_the_room_asks_everything_as_before() -> None:
    coordinator, _ = _parts(decision=_handoff(), room_handoff=False)

    result = await coordinator.run(_turn(_shopped(), "design my living room"))

    room = result.state.room_project
    assert room is not None and room.regular_seating_count is None
    assert room.design_preferences == ()
    assert result.room_carried is None


async def test_around_their_picks_the_picks_seats_are_confirmed_not_a_search() -> None:
    from app.schemas.agent_decision import AgentAction, CustomerAgentDecision

    coordinator, _ = _parts(
        decision=CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF, anchor_picks=True),
        room_handoff=True,
    )

    result = await coordinator.run(_turn(_shopped(), "build my living room around my picks"))

    assert result.state.room_project is not None
    assert result.state.room_project.regular_seating_count is None


async def test_a_colour_said_in_the_search_itself_is_the_rooms() -> None:
    coordinator, _ = _parts(decision=_handoff(), room_handoff=True)
    state = _shopped(said=())
    state = state.model_copy(
        update={
            "active_search": state.active_search.model_copy(
                update={"semantic_preferences": (BEIGE,)}
            )
        }
    )

    result = await coordinator.run(_turn(state, "design my living room"))

    room = result.state.room_project
    assert room is not None
    assert [p.canonical_value for p in room.design_preferences] == ["Beige"]


async def test_a_colour_no_approved_value_names_is_not_carried() -> None:
    from app.schemas.query import ConstraintStrength, SemanticPreference

    mustard = SemanticPreference(
        family=AttributeFamily.COLOR,
        raw_value="mustardy-ish",
        canonical_value=None,
        strength=ConstraintStrength.PREFERRED,
    )
    coordinator, _ = _parts(decision=_handoff(), room_handoff=True)

    result = await coordinator.run(_turn(_shopped(said=(mustard,)), "design my living room"))

    room = result.state.room_project
    assert room is not None and room.design_preferences == ()


async def test_a_search_seeded_from_a_product_is_not_what_they_said() -> None:
    """More like this sets the product's own colour and seats: neither is the
    customer's, so the room asks for them."""
    coordinator, _ = _parts(decision=_handoff(), room_handoff=True)
    state = _shopped(said=())
    state = state.model_copy(
        update={
            "active_search": state.active_search.model_copy(
                update={"semantic_preferences": (BEIGE,), "from_product": True}
            )
        }
    )

    result = await coordinator.run(_turn(state, "design my living room"))

    room = result.state.room_project
    assert room is not None
    assert room.design_preferences == () and room.regular_seating_count is None


async def test_a_search_for_a_piece_the_room_does_not_hold_lends_it_nothing() -> None:
    """A wardrobe's wall and colour are not a living room's."""
    coordinator, _ = _parts(decision=_handoff(), room_handoff=True)
    state = _shopped(said=(), seat_preference=None)
    state = state.model_copy(
        update={
            "active_search": state.active_search.model_copy(
                update={
                    "request": ProductSearchRequest(
                        commerce_category="bedroom", commerce_subcategory="wardrobe"
                    ),
                    "semantic_preferences": (BEIGE,),
                }
            )
        }
    )

    result = await coordinator.run(_turn(state, "design my living room"))

    room = result.state.room_project
    assert room is not None
    assert room.design_preferences == () and room.geometry is None


async def test_a_newer_seating_search_outranks_an_old_combination_offer() -> None:
    from app.schemas.agent_state import SeatingOfferState

    coordinator, _ = _parts(decision=_handoff(), room_handoff=True)
    state = _shopped(seat_preference=4).model_copy(
        update={"seating_offer": SeatingOfferState(target_seats=9)}
    )

    result = await coordinator.run(_turn(state, "design my living room"))

    assert result.state.room_project is not None
    assert result.state.room_project.regular_seating_count == 4


async def test_a_carried_head_count_gives_way_to_the_picks_seats() -> None:
    from app.schemas.agent_decision import AgentAction, CustomerAgentDecision

    from tests.unit.test_room_around_picks import SEATING, THREE_SEATER, Catalog

    carried = _room(regular_seating_count=4, seats_carried=True)
    coordinator, _ = _parts(
        decision=CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF, anchor_picks=True),
        hydration=Catalog(),
        seating=SEATING,
        room_handoff=True,
    )
    state = _shopped(room=carried).model_copy(
        update={
            "product_interaction": ProductInteractionState(selected_product_ids=(THREE_SEATER,))
        }
    )

    result = await coordinator.run(_turn(state, "build it around my picks"))

    room = result.state.room_project
    assert room is not None and room.anchor_product_ids == (THREE_SEATER,)
    assert room.regular_seating_count is None and not room.seats_carried
