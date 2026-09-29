"""The whole room offered beside a pick, and built around it (CLAUDE.md 10, 27).

A sofa or a bed the customer picks is where a room starts. The reviewed room
registry says which types start which room; the coordinator offers that room as
a chip while none is under way, and tapping it runs the ordinary whole-room
handoff with the piece kept - through the room's questions, which the anchor
waits out, into the built room as a locked line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision, CustomerStateProposal
from app.schemas.agent_state import (
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    RoomAnchorState,
    RoomProjectState,
)
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.agent_updates import AgentStateUpdate, RoomProjectUpdate
from app.schemas.grounding import TurnFailureCode
from app.schemas.product_action import (
    CompanionOffer,
    GoesWithPickAction,
    RoomAroundPickAction,
    RoomOffer,
)
from app.schemas.room_opener import RoomQuestionKind
from app.services.agent_state import apply_update
from app.services.cross_sell import companion_choices
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics

from tests.unit.test_room_questions_turn import BUDGET, LIVING_STOCK
from tests.unit.test_turn_coordinator import (
    CONTEXT,
    OFF_SCREEN,
    FakeCapabilities,
    FakeHydration,
    _coordinator,
    _state,
)

TAXONOMY = load_taxonomy()
SEATING = load_seating_semantics(taxonomy=TAXONOMY)
ROOMS = load_room_pieces(taxonomy=TAXONOMY, seating=SEATING)

SOFA = OFF_SCREEN
"""The fakes resolve every reference to this product, and hydrate it as a sofa."""


def _answer() -> CustomerAgentDecision:
    return CustomerAgentDecision(action=AgentAction.ANSWER)


def _tap(state: AgentStateV1, action: Any) -> CustomerTurnInput:
    return CustomerTurnInput(message="tap", state=state, context=CONTEXT, product_action=action)


def _parts(decision: CustomerAgentDecision | None = None, **kwargs: Any) -> tuple[Any, Any]:
    kwargs.setdefault("capabilities", FakeCapabilities(pairs=LIVING_STOCK))
    return _coordinator(decision or _answer(), rooms=ROOMS, seating=SEATING, **kwargs)


def _waiting(**fields: Any) -> RoomProjectState:
    return RoomProjectState(
        room_kind="living_room",
        pending_anchor=RoomAnchorState(product_id=SOFA, acquisition=BundleAcquisition.TO_BUY),
        **fields,
    )


# ── the registry ────────────────────────────────────────────────────────────


def test_a_sofa_starts_a_living_room_and_a_bed_a_bedroom() -> None:
    living, bedroom = ROOMS.built_around("sofa"), ROOMS.built_around("bed")
    assert living is not None and living.kind == "living_room"
    assert living.label == "living room"
    assert bedroom is not None and bedroom.kind == "bedroom"


@pytest.mark.parametrize("subcategory", ["nightstand", "carpet", "chair", None])
def test_a_piece_of_a_room_starts_none(subcategory: str | None) -> None:
    assert ROOMS.built_around(subcategory) is None


def _write(tmp_path: Path, rooms: str) -> Path:
    path = tmp_path / "rooms.yaml"
    path.write_text(f"version: v1\nrooms:\n{rooms}", encoding="utf-8")
    return path


BED_PIECE = "      - {key: bed, label: Bed, category: bedroom, subcategory: bed, tier: essential}\n"


@pytest.mark.parametrize(
    ("rooms", "problem"),
    [
        (f"  bedroom:\n    anchors: [sofa]\n    pieces:\n{BED_PIECE}", "not one of its pieces"),
        (f"  bedroom:\n    anchors: [bed, bed]\n    pieces:\n{BED_PIECE}", "repeats an anchor"),
        (f"  bedroom:\n    anchors: bed\n    pieces:\n{BED_PIECE}", "must list"),
        (f"  bedroom:\n    add_ons: [rug]\n    pieces:\n{BED_PIECE}", "add-on 'rug' is not"),
        (f"  bedroom:\n    add_ons: [bed, bed]\n    pieces:\n{BED_PIECE}", "repeats an add-on"),
        (
            f"  bedroom:\n    anchors: [bed]\n    pieces:\n{BED_PIECE}"
            f"  guest_room:\n    anchors: [bed]\n    pieces:\n{BED_PIECE}",
            "shares an anchor",
        ),
    ],
)
def test_a_malformed_anchor_stops_startup(tmp_path: Path, rooms: str, problem: str) -> None:
    with pytest.raises(TaxonomyConfigurationError, match=problem):
        load_room_pieces(_write(tmp_path, rooms), taxonomy=TAXONOMY, seating=SEATING)


# ── the state ───────────────────────────────────────────────────────────────


def test_a_different_room_lets_the_waiting_piece_go() -> None:
    state = AgentStateV1(room_project=_waiting())

    after = apply_update(
        state, AgentStateUpdate(room_project=RoomProjectUpdate(room_kind="bedroom"))
    )

    assert after.room_project is not None and after.room_project.pending_anchor is None


def test_an_answer_to_a_room_question_keeps_the_waiting_piece() -> None:
    state = AgentStateV1(room_project=_waiting())

    after = apply_update(state, AgentStateUpdate(room_project=RoomProjectUpdate(budget=BUDGET)))

    assert after.room_project is not None
    assert after.room_project.pending_anchor is not None


# ── the chip ────────────────────────────────────────────────────────────────


def test_the_room_chip_leads_the_companion_chips() -> None:
    chips = companion_choices(
        (CompanionOffer(category="decor", subcategory="carpet", label="rugs"),),
        RoomOffer(room_kind="living_room", label="living room"),
    )

    assert [chip.label for chip in chips] == [
        "Design the whole living room around it",
        "Matching rugs",
    ]
    assert isinstance(chips[0].product_action, RoomAroundPickAction)


def test_without_a_room_the_chips_are_the_companions_alone() -> None:
    chips = companion_choices(
        (CompanionOffer(category="decor", subcategory="carpet", label="rugs"),)
    )
    assert [chip.label for chip in chips] == ["Matching rugs"]


# ── the offer beside a pick ─────────────────────────────────────────────────


async def test_a_picked_sofa_offers_the_living_room() -> None:
    coordinator, _ = _parts()

    result = await coordinator.run(_tap(_state(selected=(SOFA,)), GoesWithPickAction(pick=1)))

    assert result.room_offer == RoomOffer(room_kind="living_room", label="living room")


async def test_a_store_with_nothing_else_for_the_room_offers_no_room() -> None:
    coordinator, _ = _parts(capabilities=FakeCapabilities(pairs=(("seating", "sofa"),)))

    result = await coordinator.run(_tap(_state(selected=(SOFA,)), GoesWithPickAction(pick=1)))

    assert result.room_offer is None


async def test_a_room_already_under_way_is_not_offered_again() -> None:
    room = RoomProjectState(
        room_kind="living_room",
        bundle_items=(
            BundleItemState(
                line_id=1,
                product_id=99,
                quantity=1,
                acquisition=BundleAcquisition.TO_BUY,
                status=BundleItemStatus.SUGGESTED,
            ),
        ),
        next_bundle_line_id=2,
        bundle_revision=1,
    )
    coordinator, _ = _parts(hydration=FakeHydration(available=(SOFA, 99)))

    result = await coordinator.run(
        _tap(_state(selected=(SOFA,), room=room), GoesWithPickAction(pick=1))
    )

    assert result.room_offer is None


async def test_an_unreachable_catalog_offers_no_room_and_still_shows_the_pick() -> None:
    from app.core.exceptions import IntegrationUnavailableError

    coordinator, _ = _parts(
        capabilities=FakeCapabilities(error=IntegrationUnavailableError(detail="down"))
    )

    result = await coordinator.run(_tap(_state(selected=(SOFA,)), GoesWithPickAction(pick=1)))

    assert result.room_offer is None
    assert result.grounding.failure is None


# ── tapping it ──────────────────────────────────────────────────────────────


async def test_tapping_asks_the_first_room_question_and_keeps_the_sofa_waiting() -> None:
    coordinator, parts = _parts()

    result = await coordinator.run(_tap(_state(focus=SOFA, room=None), RoomAroundPickAction()))

    assert result.room_question is not None
    assert result.room_question.kind is RoomQuestionKind.BUDGET
    room = result.state.room_project
    assert room is not None and room.room_kind == "living_room"
    assert room.pending_anchor is not None and room.pending_anchor.product_id == SOFA
    # Nothing is locked yet: a line now would read as a room already started
    # and skip every question.
    assert room.bundle_items == ()
    assert parts["design"].requests == []


async def test_a_typed_room_around_a_sofa_keeps_it_through_the_questions() -> None:
    from app.schemas.agent_decision import DesignAnchorIntent
    from app.schemas.product_reference import FocusedProduct

    decision = CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF,
        design_anchor=DesignAnchorIntent(reference=FocusedProduct()),
        state_proposal=CustomerStateProposal(room_kind="living_room"),
    )
    coordinator, _ = _parts(decision)

    result = await coordinator.run(
        CustomerTurnInput(
            message="design my living room around this sofa",
            state=_state(focus=SOFA, room=None),
            context=CONTEXT,
        )
    )

    room = result.state.room_project
    assert result.room_question is not None
    assert room is not None and room.pending_anchor is not None


async def test_the_built_room_keeps_the_waiting_sofa_locked() -> None:
    coordinator, parts = _parts(CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF))
    state = _state(room=_waiting(budget=BUDGET, questions_done=True))

    result = await coordinator.run(
        CustomerTurnInput(message="just design it", state=state, context=CONTEXT)
    )

    (request,) = parts["optimizer"].requests
    assert [lock.product.product_id for lock in request.locked] == [SOFA]
    room = result.state.room_project
    assert room is not None and room.pending_anchor is None


async def test_a_waiting_piece_that_left_the_catalog_is_let_go_and_said() -> None:
    coordinator, parts = _parts(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        hydration=FakeHydration(available=()),
    )
    state = _state(selected=(), room=_waiting(budget=BUDGET, questions_done=True))

    result = await coordinator.run(
        CustomerTurnInput(message="just design it", state=state, context=CONTEXT)
    )

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.PRODUCT_UNAVAILABLE
    assert result.state.room_project is not None
    assert result.state.room_project.pending_anchor is None
    assert parts["optimizer"].requests == []


async def test_a_pick_that_starts_no_room_is_refused() -> None:
    from tests.unit.test_turn_coordinator import _product

    class Nightstands(FakeHydration):
        async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[Any, ...]:
            return tuple(_product(p, subcategory="chair") for p in product_ids)

    coordinator, parts = _parts(hydration=Nightstands())

    result = await coordinator.run(_tap(_state(focus=SOFA), RoomAroundPickAction()))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.REFERENCE_UNRESOLVED
    assert parts["design"].requests == []
