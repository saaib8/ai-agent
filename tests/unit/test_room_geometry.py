"""Room measurements: captured as stated, converted once, never invented.

The rule every test here turns on is that a room measurement is the customer's
own. Nothing derives one from a room type, a photograph or a design model's
guess, and a unit nobody recognised is a question rather than an assumption.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.agent_decision import (
    BlockingClarificationReason,
    CustomerStateProposal,
    RoomGeometryProposal,
    RoomMeasurementProposal,
)
from app.schemas.agent_state import AgentStateV1, RoomProjectState
from app.schemas.agent_updates import AgentStateUpdate, RoomProjectUpdate
from app.schemas.geometry import (
    MeasurementAuthority,
    RoomGeometry,
    RoomMeasurement,
    RoomMeasurementRole,
)
from app.services.agent_state import apply_update
from app.services.proposal_mapping import map_proposals
from pydantic import ValidationError


def _m(role: RoomMeasurementRole, cm: str, label: str | None = None) -> RoomMeasurement:
    return RoomMeasurement(role=role, centimetres=Decimal(cm), label=label)


def _proposal(
    *measurements: RoomMeasurementProposal,
) -> CustomerStateProposal:
    return CustomerStateProposal(
        room_geometry=RoomGeometryProposal(measurements=measurements)
    )


# ── the contract ────────────────────────────────────────────────────────────


def test_a_room_measurement_is_always_the_customers_own() -> None:
    """Fixed by the type, so a measurement cannot be built with another
    provenance."""
    measurement = _m(RoomMeasurementRole.ROOM_LENGTH, "500")

    assert measurement.authority is MeasurementAuthority.USER_PROVIDED
    with pytest.raises(ValidationError):
        RoomMeasurement.model_validate(
            {
                "role": RoomMeasurementRole.ROOM_LENGTH.value,
                "centimetres": "500",
                "authority": MeasurementAuthority.GENERAL_GUIDANCE.value,
            }
        )


def test_a_room_has_one_of_each_singular_measurement() -> None:
    with pytest.raises(ValidationError, match="one of each"):
        RoomGeometry(
            measurements=(
                _m(RoomMeasurementRole.ROOM_LENGTH, "500"),
                _m(RoomMeasurementRole.ROOM_LENGTH, "600"),
            )
        )


def test_a_room_may_have_several_usable_walls() -> None:
    """Which wall a piece is destined for is the customer's to say."""
    geometry = RoomGeometry(
        measurements=(
            _m(RoomMeasurementRole.USABLE_WALL, "320", "by the window"),
            _m(RoomMeasurementRole.USABLE_WALL, "280"),
        )
    )

    assert len(geometry.of(RoomMeasurementRole.USABLE_WALL)) == 2
    assert geometry.one(RoomMeasurementRole.USABLE_WALL) is None, "which one?"


def test_a_partial_room_is_an_ordinary_room() -> None:
    """Knowing the usable wall answers a fit question with no room size at
    all."""
    geometry = RoomGeometry(measurements=(_m(RoomMeasurementRole.USABLE_WALL, "320"),))

    assert geometry.one(RoomMeasurementRole.ROOM_LENGTH) is None
    assert geometry.one(RoomMeasurementRole.USABLE_WALL) is not None
    assert not geometry.is_empty


def test_a_measurement_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _m(RoomMeasurementRole.ROOM_LENGTH, "0")


def test_the_contract_holds_no_geometry_engine() -> None:
    """Measurements, not a floor plan (CLAUDE.md scope)."""
    fields = set(RoomMeasurement.model_fields) | set(RoomGeometry.model_fields)

    for forbidden in ("x", "y", "coordinates", "polygon", "position", "placement"):
        assert forbidden not in fields


# ── capture ─────────────────────────────────────────────────────────────────


def test_five_by_four_metres_is_recorded_in_centimetres() -> None:
    mapped = map_proposals(
        _proposal(
            RoomMeasurementProposal(
                role=RoomMeasurementRole.ROOM_LENGTH, value="5", unit="m"
            ),
            RoomMeasurementProposal(
                role=RoomMeasurementRole.ROOM_WIDTH, value="4", unit="m"
            ),
        ),
        None,
    )

    assert mapped.update.room_project is not None
    geometry = mapped.update.room_project.geometry
    assert geometry is not None
    length = geometry.one(RoomMeasurementRole.ROOM_LENGTH)
    width = geometry.one(RoomMeasurementRole.ROOM_WIDTH)
    assert length is not None and length.centimetres == Decimal("500")
    assert width is not None and width.centimetres == Decimal("400")
    assert mapped.clarification is None


@pytest.mark.parametrize(
    ("unit", "value", "centimetres"),
    [("m", "5", "500"), ("cm", "320", "320"), ("mm", "3200", "320")],
)
def test_every_recognised_unit_converts_through_the_shared_vocabulary(
    unit: str, value: str, centimetres: str
) -> None:
    """The same units the catalog understands, so a room and a product are
    comparable without converting twice."""
    mapped = map_proposals(
        _proposal(
            RoomMeasurementProposal(
                role=RoomMeasurementRole.USABLE_WALL, value=value, unit=unit
            )
        ),
        None,
    )

    assert mapped.update.room_project is not None
    geometry = mapped.update.room_project.geometry
    assert geometry is not None
    assert geometry.measurements[0].centimetres == Decimal(centimetres)


def test_a_measurement_with_no_unit_is_asked_about_not_assumed() -> None:
    """Five could be metres or feet, and recording the wrong room is worse than
    recording none."""
    mapped = map_proposals(
        _proposal(
            RoomMeasurementProposal(role=RoomMeasurementRole.ROOM_LENGTH, value="5")
        ),
        None,
    )

    assert mapped.update.room_project is None, "nothing recorded"
    assert mapped.clarification is not None
    assert mapped.clarification.reason is (
        BlockingClarificationReason.MISSING_DIMENSION_UNIT
    )


def test_an_unrecognised_unit_is_also_refused() -> None:
    mapped = map_proposals(
        _proposal(
            RoomMeasurementProposal(
                role=RoomMeasurementRole.ROOM_LENGTH, value="5", unit="parsecs"
            )
        ),
        None,
    )

    assert mapped.update.room_project is None
    assert mapped.clarification is not None


def test_other_room_facts_survive_a_missing_unit() -> None:
    """One unanswerable measurement does not discard what they also said."""
    mapped = map_proposals(
        CustomerStateProposal(
            room_type="living room",
            room_geometry=RoomGeometryProposal(
                measurements=(
                    RoomMeasurementProposal(
                        role=RoomMeasurementRole.ROOM_LENGTH, value="5"
                    ),
                )
            ),
        ),
        None,
    )

    assert mapped.update.room_project is not None
    assert mapped.update.room_project.room_type == "living room"
    assert mapped.update.room_project.geometry is None
    assert mapped.clarification is not None


def test_a_proposal_cannot_set_and_clear_geometry_at_once() -> None:
    with pytest.raises(ValidationError, match="room_geometry"):
        CustomerStateProposal(
            clear_room_geometry=True,
            room_geometry=RoomGeometryProposal(
                measurements=(
                    RoomMeasurementProposal(
                        role=RoomMeasurementRole.ROOM_LENGTH, value="5", unit="m"
                    ),
                )
            ),
        )


def test_an_empty_geometry_proposal_is_rejected() -> None:
    """A proposal that measures nothing is a model slip, not a room."""
    with pytest.raises(ValidationError):
        RoomGeometryProposal(measurements=())


# ── persistence through the reducer ─────────────────────────────────────────


def test_geometry_persists_so_a_later_turn_can_use_it() -> None:
    """Someone gives their room size in one turn and asks about fit in the
    next. That is the whole reason this is state."""
    state = apply_update(
        AgentStateV1(),
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                geometry=RoomGeometry(
                    measurements=(_m(RoomMeasurementRole.USABLE_WALL, "320"),)
                )
            )
        ),
    )

    later = apply_update(state, AgentStateUpdate())

    assert later.room_project is not None
    assert later.room_project.geometry is not None
    assert later.room_project.geometry.one(
        RoomMeasurementRole.USABLE_WALL
    ).centimetres == Decimal("320")  # type: ignore[union-attr]


def test_geometry_can_be_cleared() -> None:
    state = AgentStateV1(
        room_project=RoomProjectState(
            geometry=RoomGeometry(
                measurements=(_m(RoomMeasurementRole.ROOM_LENGTH, "500"),)
            )
        )
    )

    cleared = apply_update(
        state, AgentStateUpdate(room_project=RoomProjectUpdate(clear_geometry=True))
    )

    assert cleared.room_project is not None
    assert cleared.room_project.geometry is None


def test_an_update_that_omits_geometry_leaves_it_alone() -> None:
    """Absence means untouched, as everywhere else in the reducer."""
    state = AgentStateV1(
        room_project=RoomProjectState(
            room_type="bedroom",
            geometry=RoomGeometry(
                measurements=(_m(RoomMeasurementRole.ROOM_LENGTH, "500"),)
            ),
        )
    )

    updated = apply_update(
        state, AgentStateUpdate(room_project=RoomProjectUpdate(room_type="living room"))
    )

    assert updated.room_project is not None
    assert updated.room_project.room_type == "living room"
    assert updated.room_project.geometry is not None


def test_geometry_survived_the_later_state_changes() -> None:
    """Geometry was additive and did not move the version; M12E-1's bundle
    change did, because it *removed* fields. The distinction is the whole rule:
    a version marks a shape a reader could get wrong."""
    from app.schemas.agent_state import AGENT_STATE_VERSION

    assert AGENT_STATE_VERSION == "agent_state_v3"
    assert AgentStateV1().schema_version == "agent_state_v3"
    # A state carrying only the fields geometry added still validates.
    assert AgentStateV1.model_validate({"room_project": {"room_type": "bedroom"}})
    assert "geometry" in RoomProjectState.model_fields
