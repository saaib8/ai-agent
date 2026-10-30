"""Their room: what they said about it, measured fit, and the designer's
placements and sizes for the next piece."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.agents.views import session_brief
from app.agents.work import TurnWork
from app.schemas.agent_state import AgentStateV1
from app.schemas.design import (
    NextSize,
)
from app.schemas.dimensions import DimensionStatus, DimensionUnit, NormalisedDimensions
from app.schemas.geometry import RoomGeometry, RoomMeasurement, RoomMeasurementRole
from app.services.agent_state import remember_next_sizes
from app.services.fit import fit_checks, fit_words, floor_share, room_spaces

from tests.unit.agents.fakes import (
    FakeHydration,
    FakePipeline,
    _product,
    agent_services,
    real_loop,
    tool_context,
)

LOOP = real_loop()


async def _call(name: str, args: dict[str, Any], state: AgentStateV1, services: Any) -> Any:
    tool = LOOP._registry.get(name)
    assert tool is not None and tool.handler is not None
    return await tool.handler(
        tool.args.model_validate(args), tool_context(services), TurnWork.starting(state)
    )


def _cm(value: int) -> dict[str, Any]:
    return {"value": value, "unit": "cm"}


async def _noted_room(services: Any) -> AgentStateV1:
    noted = await _call(
        "note_room",
        {
            "room_kind": "living_room",
            "length": {"value": 5, "unit": "m"},
            "width": {"value": 4, "unit": "m"},
            "doorway": _cm(90),
            "walls": [
                {"name": "TV wall", "length": _cm(400), "has": ["tv_point"]},
                {"name": "window wall", "has": ["window"]},
            ],
        },
        AgentStateV1(),
        services,
    )
    return noted.state  # type: ignore[no-any-return]


def _sized(product_id: int, longer: int, shorter: int, height: int = 80) -> Any:
    return _product(product_id).model_copy(
        update={
            "dimensions": NormalisedDimensions(
                length_cm=Decimal(longer),
                width_cm=Decimal(shorter),
                height_cm=Decimal(height),
                unit=DimensionUnit.CENTIMETRE,
                status=DimensionStatus.NORMALISED,
            )
        }
    )


async def test_what_they_say_about_their_room_is_kept_for_every_agent() -> None:
    services = agent_services()
    state = await _noted_room(services)

    brief = (await session_brief(services, state, tool_context(services))).context()

    room = brief["room"]
    assert {"measures": "room_length", "cm": "500"} in room["size"]
    assert room["walls"][0] == {"wall": "TV wall", "cm": "400", "has": ["tv_point"]}
    assert room["walls"][1] == {"wall": "window wall", "has": ["window"]}


async def test_a_wall_said_again_keeps_what_it_does_not_restate() -> None:
    services = agent_services()
    state = await _noted_room(services)

    again = await _call(
        "note_room",
        {"walls": [{"name": "tv wall", "has": ["keep_free"], "note": "cabinet stays"}]},
        state,
        services,
    )

    wall = again.state.room_project.layout[0]
    assert wall.length_cm == Decimal(400)
    assert [str(f) for f in wall.features] == ["tv_point", "keep_free"]
    assert wall.note == "cabinet stays"


def test_a_piece_is_measured_against_each_wall_and_the_doorway() -> None:
    geometry = RoomGeometry(
        measurements=(
            RoomMeasurement(
                role=RoomMeasurementRole.USABLE_WALL, centimetres=Decimal(300), label="TV wall"
            ),
            RoomMeasurement(role=RoomMeasurementRole.DOORWAY_WIDTH, centimetres=Decimal(80)),
        )
    )
    sofa = _sized(1, longer=220, shorter=95, height=85)

    checks = fit_checks([sofa], room_spaces(geometry))

    assert [fit_words(c) for c in checks] == ["fits with 80 cm to spare", "5 cm too big"]


def test_floor_taken_is_counted_only_when_every_size_is_known() -> None:
    room = RoomGeometry(
        measurements=(
            RoomMeasurement(role=RoomMeasurementRole.ROOM_LENGTH, centimetres=Decimal(500)),
            RoomMeasurement(role=RoomMeasurementRole.ROOM_WIDTH, centimetres=Decimal(400)),
        )
    )

    assert floor_share([_sized(1, 250, 80)], room) == 10
    assert floor_share([_sized(1, 250, 80), _product(2)], room) is None


async def test_a_search_in_a_known_room_reports_each_cards_fit() -> None:
    services = agent_services(
        pipeline=FakePipeline(ids=(10, 11)), hydration=FakeHydration(available=(10, 11))
    )
    state = await _noted_room(services)
    search = {"commerce_category": "seating", "commerce_subcategory": "sofa"}

    outcome = await _call("search_products", {"search": search}, state, services)

    fit = outcome.facts["room_fit"]
    assert [f["card"] for f in fit] == [1, 2]
    # The fake sofas list no size: nothing is guessed.
    assert fit[0]["against"]["TV wall"] == "size not listed"


async def test_the_designers_placements_and_next_sizes_reach_the_agents() -> None:
    services = agent_services()
    state = await _noted_room(services)
    notes = {
        "placements": [{"piece": "the sofa", "wall": "TV wall", "note": "facing the screen"}],
        "next_sizes": [
            {
                # The wrong family: corrected from the registry, never dropped.
                "commerce_category": "seating",
                "commerce_subcategory": "carpet",
                "longer_min_cm": 240,
                "longer_max_cm": 280,
                "reason": "under the front legs of the seating",
            }
        ],
    }

    noted = await _call("record_design_notes", notes, state, services)

    # What was kept, never the figures she wrote - those reach the brief.
    assert noted.facts == {"placements_kept": ["the sofa"], "sizes_kept_for": ["carpet"]}
    room = noted.state.room_project
    assert room.placements[0].wall == "TV wall"
    size = room.next_sizes[0]
    assert (size.commerce_category, size.commerce_subcategory) == ("decor", "carpet")
    assert size.longer_target_cm() == 260


async def test_design_notes_need_a_room_and_keep_figures_out_of_placements() -> None:
    services = agent_services()
    placement = {"piece": "the sofa", "wall": "TV wall", "note": "facing the screen"}

    no_room = await _call(
        "record_design_notes", {"placements": [placement]}, AgentStateV1(), services
    )
    state = await _noted_room(services)
    figured = await _call(
        "record_design_notes",
        {"placements": [placement, {**placement, "piece": "the rug", "note": "30 cm out"}]},
        state,
        services,
    )

    assert no_room.code == "no_room_on_record"
    # Only the placement with a figure goes; the rest stands.
    assert figured.facts["placements_kept"] == ["the sofa"]
    assert figured.facts["placements_dropped"]["pieces"] == ["the rug"]
    assert [p.piece for p in figured.state.room_project.placements] == ["the sofa"]


async def test_a_designers_size_orders_the_search_and_hides_nothing() -> None:
    pipeline = FakePipeline(ids=(10, 11))
    services = agent_services(pipeline=pipeline, hydration=FakeHydration(available=(10, 11)))
    search = {"commerce_category": "seating", "commerce_subcategory": "sofa"}

    sized = remember_next_sizes(
        AgentStateV1(),
        (
            NextSize(
                commerce_category="seating",
                commerce_subcategory="sofa",
                longer_min_cm=240,
                longer_max_cm=280,
                reason="no longer than the wall it faces",
            ),
        ),
    )

    outcome = await _call(
        "search_products", {"search": search, "designer_size": True}, sized, services
    )

    assert pipeline.calls[-1].lean.size_target_cm == Decimal(260)
    assert outcome.facts["ordered_near_longer_side_cm"] == 260
    assert outcome.facts["shown"] == 2
