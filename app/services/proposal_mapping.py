"""Turning what the model proposed into typed state updates.

A decision carries *proposals* - what the customer said about themselves. This
turns them into `AgentStateUpdate`, which is what the reducer accepts. The two
are deliberately different types: a model that could emit an `AgentStateUpdate`
could clear a room budget or replace a preference list directly, so the
translation is application code's and the reducer still decides whether the
result is a state that may exist (CLAUDE.md 3.3).

Pure functions. No I/O, no clock, no reasoning.

One asymmetry is worth naming. A price the customer stated arrives as strings
with an optional currency, while a persisted budget requires one. When it is
missing, nothing is guessed and nothing invalid is built: the budget is left
out and the omission is reported as a clarification, while every other field in
the same proposal still applies. A malformed *amount* is a different thing
entirely - that is a defect in what the model produced, not a question for the
customer, so it raises.
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple

from pydantic import ValidationError

from app.core.exceptions import LLMResponseInvalidError
from app.core.numbers import parse_stated_amount, parse_stated_decimal
from app.schemas.agent_decision import (
    BlockingClarificationReason,
    CustomerStateProposal,
    DerivedCommerceProposal,
    PreferenceProposal,
    PreferenceProposalOp,
    PriceProposal,
    RoomGeometryProposal,
)
from app.schemas.agent_updates import (
    AddItems,
    AgentStateUpdate,
    CustomerPreferenceUpdate,
    DerivedCommerceUpdate,
    PreferenceListUpdate,
    RemoveItems,
    ReplaceItems,
    RoomProjectUpdate,
)
from app.schemas.dimensions import parse_unit, to_centimetres
from app.schemas.discovery import PriceConstraint
from app.schemas.geometry import RoomGeometry, RoomMeasurement
from app.schemas.resolution import DeterministicClarification


class MappedProposals(NamedTuple):
    """The update to apply, and anything the customer must settle first."""

    update: AgentStateUpdate
    clarification: DeterministicClarification | None = None


def map_proposals(
    state_proposal: CustomerStateProposal | None,
    commerce_proposal: DerivedCommerceProposal | None,
) -> MappedProposals:
    """Both proposals from one decision, as a single typed update.

    Combined rather than applied separately so the reducer sees one transition:
    two sequential updates would each rebuild the state, and a failure between
    them would leave half of what the customer said recorded.
    """
    customer, room, clarification = _customer_state(state_proposal)
    return MappedProposals(
        update=AgentStateUpdate(
            customer_preferences=customer,
            room_project=room,
            derived_commerce=_derived_commerce(commerce_proposal),
        ),
        clarification=clarification,
    )


def _customer_state(
    proposal: CustomerStateProposal | None,
) -> tuple[
    CustomerPreferenceUpdate | None,
    RoomProjectUpdate | None,
    DeterministicClarification | None,
]:
    if proposal is None:
        return None, None, None

    customer = (
        CustomerPreferenceUpdate(semantic_preferences=_preferences(proposal.customer_preferences))
        if proposal.customer_preferences is not None
        else None
    )

    budget, clarification = _budget(proposal.room_budget)
    geometry, geometry_clarification = _geometry(proposal.room_geometry)
    # One question per turn, and the currency outranks the unit only because
    # something has to: both are the same kind of gap, and the coordinator
    # surfaces whichever it is given.
    clarification = clarification or geometry_clarification
    touches_room = (
        proposal.room_type is not None
        or proposal.clear_room_type
        or budget is not None
        or proposal.clear_room_budget
        or geometry is not None
        or proposal.clear_room_geometry
        or proposal.design_preferences is not None
        or proposal.regular_seating_count is not None
        or proposal.clear_regular_seating_count
        or proposal.room_kind is not None
        or proposal.room_skip_questions
    )
    room = (
        RoomProjectUpdate(
            room_type=proposal.room_type,
            clear_room_type=proposal.clear_room_type,
            geometry=geometry,
            clear_geometry=proposal.clear_room_geometry,
            budget=budget,
            clear_budget=proposal.clear_room_budget,
            design_preferences=_preferences(proposal.design_preferences),
            regular_seating_count=proposal.regular_seating_count,
            clear_regular_seating_count=proposal.clear_regular_seating_count,
            # The kind is checked against the room registry, and the chosen
            # pieces resolved to its keys, by the coordinator - which has the
            # registry this pure mapping deliberately does not.
            room_kind=proposal.room_kind,
            questions_done=proposal.room_skip_questions,
        )
        if touches_room
        else None
    )
    return customer, room, clarification


def _preferences(proposal: PreferenceProposal | None) -> PreferenceListUpdate | None:
    if proposal is None:
        return None
    match proposal.op:
        case PreferenceProposalOp.ADD:
            return AddItems(items=proposal.preferences)
        case PreferenceProposalOp.REMOVE:
            return RemoveItems(items=proposal.preferences)
        case PreferenceProposalOp.REPLACE:
            return ReplaceItems(items=proposal.preferences)


def _budget(
    proposal: PriceProposal | None,
) -> tuple[PriceConstraint | None, DeterministicClarification | None]:
    """A budget, or the reason it could not be recorded.

    A stated amount with no currency is not an error and not a guess: the
    retailer's currency is never inferred (CLAUDE.md 15), so the figure is held
    back and the customer is asked which currency they meant.
    """
    if proposal is None:
        return None, None
    currency = (proposal.currency or "").strip()
    if not currency:
        return None, DeterministicClarification(
            reason=BlockingClarificationReason.MISSING_PRICE_CURRENCY
        )
    try:
        budget = PriceConstraint(
            currency=currency,
            min_amount=_amount(proposal.min_amount, field="min_amount", money=True),
            max_amount=_amount(proposal.max_amount, field="max_amount", money=True),
        )
    except ValidationError as exc:
        # A negative, non-finite or inverted budget. The figures parsed, but
        # they cannot be what the customer said - a defect in what the model
        # produced, exactly like an amount that is not a number at all.
        raise LLMResponseInvalidError(reason="room budget invalid") from exc
    return budget, None


def _geometry(
    proposal: RoomGeometryProposal | None,
) -> tuple[RoomGeometry | None, DeterministicClarification | None]:
    """Stated measurements in centimetres, or the reason they were not recorded.

    A measurement with no usable unit is held back whole, not converted on an
    assumption: five could be metres or feet, and recording the wrong room is
    worse than recording none. The customer is asked which they meant.
    """
    if proposal is None:
        return None, None
    measurements: list[RoomMeasurement] = []
    for stated in proposal.measurements:
        unit = parse_unit(stated.unit)
        if unit is None:
            return None, DeterministicClarification(
                reason=BlockingClarificationReason.MISSING_DIMENSION_UNIT
            )
        value = _amount(stated.value, field="room measurement")
        assert value is not None
        try:
            measurement = RoomMeasurement(
                role=stated.role,
                centimetres=to_centimetres(value, unit),
                label=stated.label,
            )
        except ValidationError as exc:
            # Zero, negative or non-finite: no room has that length.
            raise LLMResponseInvalidError(reason="room measurement invalid") from exc
        measurements.append(measurement)
    try:
        return RoomGeometry(measurements=tuple(measurements)), None
    except ValidationError as exc:
        # Two different lengths for one room. The model contradicted itself,
        # which is not something the customer can settle.
        raise LLMResponseInvalidError(reason="room geometry invalid") from exc


def _amount(raw: str | None, *, field: str, money: bool = False) -> Decimal | None:
    """A stated figure as a decimal, or a defect.

    Never asked about: the customer said a number, and our failure to read what
    the model returned is not something they can fix.
    """
    if raw is None:
        return None
    try:
        return parse_stated_amount(raw) if money else parse_stated_decimal(raw)
    except ValueError as exc:
        raise LLMResponseInvalidError(reason=f"{field} was not a usable decimal") from exc


def _derived_commerce(
    proposal: DerivedCommerceProposal | None,
) -> DerivedCommerceUpdate | None:
    """The system's own read, kept in its own domain (CLAUDE.md 6.1)."""
    if proposal is None:
        return None
    return DerivedCommerceUpdate(
        purchase_stage=proposal.purchase_stage,
        clear_purchase_stage=proposal.clear_purchase_stage,
    )
