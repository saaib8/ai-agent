"""Will it fit (docs/designer-led-shopping-plan.md, phase 7).

Code measures, the designer decides. Each piece asked about is measured against
every wall and doorway the customer gave - a wall against its longer side, a
doorway against the smaller of its depth and height - and the design specialist
judges whether it works in their room, with the room's size, which is asked
for first when it is not on record.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarificationReason,
    CustomerAgentDecision,
    DesignScope,
)
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    ProductInteractionState,
    RoomProjectState,
)
from app.schemas.design import DesignGuidance, FitVerdict, GuidanceTopic, InteriorDesignResult
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import ProductSearchRequest
from app.schemas.geometry import RoomGeometry, RoomMeasurement, RoomMeasurementRole
from app.schemas.product import ProductCandidate
from app.schemas.query import RankingLean
from app.services.fit import Space, fit_checks, knows_room_size, spaces_of

from tests.unit.test_turn_coordinator import FakeDesign, FakeHydration, _coordinator, _product
from tests.unit.test_turn_coordinator import _turn as turn_of

WALL = Space(kind="wall", centimetres=Decimal("300"))
DOOR = Space(kind="doorway", centimetres=Decimal("80"))


def _piece(length: str | None, width: str | None, height: str | None = None) -> ProductCandidate:
    return _product(1).model_copy(
        update={
            "dimensions": NormalisedDimensions(
                status=DimensionStatus.NORMALISED,
                length_cm=Decimal(length) if length else None,
                width_cm=Decimal(width) if width else None,
                height_cm=Decimal(height) if height else None,
            )
        }
    )


def _measure(role: RoomMeasurementRole, cm: str, label: str | None = None) -> RoomMeasurement:
    return RoomMeasurement(role=role, centimetres=Decimal(cm), label=label)


ROOM = RoomGeometry(
    measurements=(
        _measure(RoomMeasurementRole.ROOM_LENGTH, "500"),
        _measure(RoomMeasurementRole.ROOM_WIDTH, "400"),
        _measure(RoomMeasurementRole.USABLE_WALL, "300", "sofa wall"),
        _measure(RoomMeasurementRole.DOORWAY_WIDTH, "80"),
    )
)


# ── what code measures ══════════════════════════════════════════════════════


def test_a_wall_is_measured_against_the_longer_side_whatever_column_holds_it() -> None:
    (within,) = fit_checks([_piece("90", "260")], [WALL])
    (too_wide,) = fit_checks([_piece("315", "95")], [WALL])

    assert (within.verdict, within.margin_cm) == (FitVerdict.WITHIN_KNOWN_LIMIT, Decimal("40"))
    assert (too_wide.verdict, too_wide.margin_cm) == (
        FitVerdict.EXCEEDS_KNOWN_LIMIT,
        Decimal("15"),
    )


def test_a_doorway_is_measured_against_the_smaller_of_depth_and_height() -> None:
    (through,) = fit_checks([_piece("220", "90", height="75")], [DOOR])
    (no_height,) = fit_checks([_piece("220", "90")], [DOOR])

    assert (through.verdict, through.piece_cm) == (FitVerdict.WITHIN_KNOWN_LIMIT, Decimal("75"))
    assert no_height.verdict is FitVerdict.INSUFFICIENT_GEOMETRY and no_height.piece_cm is None


def test_the_spaces_are_the_rooms_walls_and_doors_and_a_searchs_space() -> None:
    state = AgentStateV1(
        room_project=RoomProjectState(geometry=ROOM),
        active_search=ActiveSearchState(
            request=ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa"),
            revision=1,
            lean=RankingLean(space_cm=Decimal("250")),
        ),
    )

    assert [(s.kind, s.centimetres, s.label) for s in spaces_of(state)] == [
        ("wall", Decimal("300"), "sofa wall"),
        ("doorway", Decimal("80"), None),
        ("wall", Decimal("250"), None),
    ]


def test_a_room_size_is_its_length_and_width() -> None:
    wall_only = RoomGeometry(measurements=(_measure(RoomMeasurementRole.USABLE_WALL, "300"),))

    assert knows_room_size(ROOM)
    assert not knows_room_size(wall_only) and not knows_room_size(None)


def test_a_door_said_after_a_wall_keeps_the_wall() -> None:
    from app.schemas.agent_updates import AgentStateUpdate, RoomProjectUpdate
    from app.services.agent_state import apply_update

    def said(*measurements: RoomMeasurement) -> AgentStateUpdate:
        return AgentStateUpdate(
            room_project=RoomProjectUpdate(geometry=RoomGeometry(measurements=measurements))
        )

    state = apply_update(
        AgentStateV1(),
        said(
            _measure(RoomMeasurementRole.USABLE_WALL, "300"),
            _measure(RoomMeasurementRole.ROOM_LENGTH, "500"),
        ),
    )
    state = apply_update(state, said(_measure(RoomMeasurementRole.DOORWAY_WIDTH, "80")))
    state = apply_update(state, said(_measure(RoomMeasurementRole.ROOM_LENGTH, "550")))

    assert state.room_project is not None and state.room_project.geometry is not None
    assert {(m.role, m.centimetres) for m in state.room_project.geometry.measurements} == {
        (RoomMeasurementRole.USABLE_WALL, Decimal("300")),
        (RoomMeasurementRole.DOORWAY_WIDTH, Decimal("80")),
        (RoomMeasurementRole.ROOM_LENGTH, Decimal("550")),
    }


# ── what the designer decides ═══════════════════════════════════════════════

FIT = CustomerAgentDecision(
    action=AgentAction.DESIGN_HANDOFF,
    design_scope=DesignScope.ADVICE,
    fit_question=True,
    design_question="Will these sofas fit my living room?",
)


def _engine(*, on: bool = True) -> Any:
    design = FakeDesign(
        InteriorDesignResult(
            guidance=(
                DesignGuidance(
                    topic=GuidanceTopic.SPACING,
                    summary="The first works; the second crowds the walkway.",
                ),
            )
        )
    )
    return _coordinator(
        FIT,
        design=design,
        hydration=FakeHydration(available=(20, 21)),
        designer_fit=on,
    )


def _screen(geometry: RoomGeometry | None) -> AgentStateV1:
    return AgentStateV1(
        room_project=RoomProjectState(geometry=geometry) if geometry else None,
        active_search=ActiveSearchState(
            request=ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa"),
            revision=1,
        ),
        product_interaction=ProductInteractionState(
            presented_product_ids=(20, 21), presented_search_revision=1
        ),
    )


async def test_without_the_rooms_size_the_size_is_asked_first() -> None:
    coordinator, parts = _engine()

    result = await coordinator.run(turn_of(_screen(None), "will these fit my living room?"))

    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is BlockingClarificationReason.MISSING_ROOM_SIZE
    assert parts["design"].requests == []


async def test_with_the_rooms_size_the_designer_judges_every_card_with_its_measurements() -> None:
    coordinator, parts = _engine()

    result = await coordinator.run(turn_of(_screen(ROOM), "will these fit my living room?"))

    (request,) = parts["design"].requests
    assert request.geometry == ROOM and len(request.anchors) == 2
    assert {(c.piece, c.space) for c in request.fit_checks} == {
        (1, "wall"),
        (1, "doorway"),
        (2, "wall"),
        (2, "doorway"),
    }
    assert result.grounding.design_guidance


async def test_switched_off_a_fit_question_is_ordinary_advice() -> None:
    coordinator, parts = _engine(on=False)

    await coordinator.run(turn_of(_screen(None), "will these fit my living room?"))

    (request,) = parts["design"].requests
    assert request.fit_checks == ()


def test_a_fit_question_is_always_the_designers() -> None:
    with pytest.raises(ValueError, match="designer"):
        CustomerAgentDecision(action=AgentAction.ANSWER, fit_question=True)


def test_each_check_carries_its_card_and_spaces_keep_the_newest() -> None:
    checks = fit_checks([_piece("90", "260"), _piece("315", "95")], [WALL], cards=(2, 4))
    walls = RoomGeometry(
        measurements=tuple(
            _measure(RoomMeasurementRole.USABLE_WALL, str(200 + n), f"wall {n}") for n in range(6)
        )
    )

    assert [(c.piece, c.card) for c in checks] == [(1, 2), (2, 4)]
    assert [
        s.label for s in spaces_of(AgentStateV1(room_project=RoomProjectState(geometry=walls)))
    ] == [
        "wall 2",
        "wall 3",
        "wall 4",
        "wall 5",
    ]


def test_another_room_starts_its_measurements_again() -> None:
    from app.schemas.agent_updates import AgentStateUpdate, RoomProjectUpdate
    from app.services.agent_state import apply_update

    state = apply_update(
        AgentStateV1(),
        AgentStateUpdate(room_project=RoomProjectUpdate(room_type="living room", geometry=ROOM)),
    )
    state = apply_update(
        state,
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                room_type="bedroom",
                geometry=RoomGeometry(
                    measurements=(_measure(RoomMeasurementRole.ROOM_LENGTH, "400"),)
                ),
            )
        ),
    )

    assert state.room_project is not None and state.room_project.geometry is not None
    assert [m.role for m in state.room_project.geometry.measurements] == [
        RoomMeasurementRole.ROOM_LENGTH
    ]


async def test_a_comparison_over_the_current_list_is_what_they_are_asking_about() -> None:
    coordinator, parts = _engine()
    state = _screen(ROOM)
    state = state.model_copy(
        update={
            "product_interaction": state.product_interaction.model_copy(
                update={"compared_product_ids": (21,), "compared_search_revision": 1}
            )
        }
    )

    await coordinator.run(turn_of(state, "will these fit my living room?"))

    (request,) = parts["design"].requests
    assert len(request.anchors) == 1
    assert {(c.piece, c.card) for c in request.fit_checks} == {(1, 1)}


async def test_a_card_the_catalog_no_longer_has_keeps_the_others_numbered_as_on_screen() -> None:
    design = FakeDesign(
        InteriorDesignResult(
            guidance=(DesignGuidance(topic=GuidanceTopic.SPACING, summary="Fine."),)
        )
    )
    coordinator, parts = _coordinator(
        FIT, design=design, hydration=FakeHydration(available=(21,)), designer_fit=True
    )

    await coordinator.run(turn_of(_screen(ROOM), "will these fit my living room?"))

    (request,) = parts["design"].requests
    assert {(c.piece, c.card) for c in request.fit_checks} == {(1, 2)}
