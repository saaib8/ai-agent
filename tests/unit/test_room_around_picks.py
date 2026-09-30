"""A room built around the products the customer picked (CLAUDE.md 10.3, 27).

"Build my living room around these" after picking a 3-seater, a centre table
and a 2-seater: every pick that belongs in the room is saved before the room's
questions, the seats question confirms what the picked sofas already seat, and
when the room is built the picks are locked into it - so nothing is bought
twice, the extra seats fill only what the sofas leave, and the picks' prices
count against the budget.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision, CustomerStateProposal
from app.schemas.agent_state import AgentStateV1, BundleItemStatus, RoomProjectState
from app.schemas.agent_updates import AgentStateUpdate, RoomProjectUpdate
from app.schemas.bundle import BundleOptimizationRequest, LockedBundleProduct
from app.schemas.design import DesignCategoryNeed, DesignPriority
from app.schemas.design_discovery import DesignDiscoveryResult, DesignNeedCandidates
from app.schemas.discovery import PriceConstraint, SeatingCapacityConstraint
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.relaxation import StopReason
from app.schemas.resolution import CandidatePoolResult, RankedProductCandidate
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerCatalogCapability
from app.schemas.room_opener import RoomQuestion, RoomQuestionKind
from app.services.agent_state import apply_update
from app.services.bundle_optimizer import BundleOptimizer
from app.services.room_composition import next_question, piece_for
from app.services.room_presentation import room_answer_choices
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics

from tests.unit.test_room_questions_turn import LIVING_STOCK, _parts
from tests.unit.test_turn_coordinator import (
    FakeCapabilities,
    FakeOptimizer,
    _product,
    _state,
    _turn,
)

TAXONOMY = load_taxonomy()
SEATING = load_seating_semantics(taxonomy=TAXONOMY)
ROOMS = load_room_pieces(taxonomy=TAXONOMY, seating=SEATING)
LIVING = ROOMS.template("living_room")
assert LIVING is not None
BUDGET = PriceConstraint.at_most(Decimal("15000"), "SAR")

THREE_SEATER, TABLE, TWO_SEATER, BED = 101, 102, 103, 104


def _item(product_id: int, category: str, subcategory: str, seats: int | None, price: str):
    return _product(product_id, price=price).model_copy(
        update={
            "commerce": CommerceClassification(
                category=category, subcategory=subcategory, seating_capacity=seats
            )
        }
    )


CATALOG: dict[int, ProductCandidate] = {
    THREE_SEATER: _item(THREE_SEATER, "seating", "sofa", 3, "2950"),
    TABLE: _item(TABLE, "tables", "center-table", None, "1130"),
    TWO_SEATER: _item(TWO_SEATER, "seating", "sofa", 2, "3950"),
    BED: _item(BED, "bedroom", "bed", None, "2000"),
}


class Catalog:
    """Hydration that knows each product's real type and seats."""

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[ProductCandidate, ...]:
        return tuple(CATALOG[p] for p in product_ids if p in CATALOG)


def _handoff(**decision: Any) -> CustomerAgentDecision:
    proposal = decision.pop("proposal", None)
    return CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF,
        state_proposal=CustomerStateProposal(**proposal) if proposal else None,
        **decision,
    )


def _with_picks(state: AgentStateV1, *picks: int) -> AgentStateV1:
    interaction = state.product_interaction.model_copy(update={"selected_product_ids": picks})
    return state.model_copy(update={"product_interaction": interaction})


def _coordinator(decision: CustomerAgentDecision, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
    return _parts(decision=decision, hydration=Catalog(), seating=SEATING, **kwargs)


# ── saving the picks ────────────────────────────────────────────────────────


async def test_around_my_picks_saves_every_pick_that_belongs_in_the_room() -> None:
    """The bed is a pick, but not a living-room piece: it is left out."""
    coordinator, _ = _coordinator(
        _handoff(anchor_picks=True, proposal={"room_kind": "living_room"})
    )
    state = _with_picks(_state(room=None), THREE_SEATER, TABLE, TWO_SEATER, BED)

    result = await coordinator.run(_turn(state, "build my living room around these products"))

    room = result.state.room_project
    assert room is not None
    assert room.anchor_product_ids == (THREE_SEATER, TABLE, TWO_SEATER)
    # The budget question comes next, and asking it does not lose them.
    assert result.room_question is not None
    assert result.room_question.kind is RoomQuestionKind.BUDGET


async def test_without_the_flag_no_pick_is_saved() -> None:
    coordinator, _ = _coordinator(_handoff(proposal={"room_kind": "living_room"}))
    state = _with_picks(_state(room=None), THREE_SEATER, TABLE)

    result = await coordinator.run(_turn(state, "design my living room"))

    assert result.state.room_project is not None
    assert result.state.room_project.anchor_product_ids == ()


def test_a_new_room_kind_forgets_the_saved_picks() -> None:
    state = AgentStateV1(
        room_project=RoomProjectState(room_kind="living_room", anchor_product_ids=(THREE_SEATER,))
    )

    moved = apply_update(
        state, AgentStateUpdate(room_project=RoomProjectUpdate(room_kind="bedroom"))
    )

    assert moved.room_project is not None
    assert moved.room_project.anchor_product_ids == ()


# ── the questions ───────────────────────────────────────────────────────────


async def test_the_seats_question_confirms_what_the_picked_sofas_seat() -> None:
    """3 + 2 = 5, never the 2 from their last sofa search."""
    coordinator, _ = _coordinator(_handoff())
    room = RoomProjectState(
        room_kind="living_room",
        budget=BUDGET,
        pieces=("sofa", "center-table", "rug"),
        anchor_product_ids=(THREE_SEATER, TABLE, TWO_SEATER),
    )

    result = await coordinator.run(_turn(_state(room=room), "choose the pieces for me"))

    question = result.room_question
    assert question is not None and question.kind is RoomQuestionKind.SEATS
    assert question.picked_seat_count == 5
    assert question.earlier_seat_count is None


async def test_the_pieces_question_shows_the_picks_as_theirs() -> None:
    coordinator, _ = _coordinator(_handoff())
    room = RoomProjectState(
        room_kind="living_room",
        budget=BUDGET,
        anchor_product_ids=(THREE_SEATER, TABLE),
    )

    result = await coordinator.run(_turn(_state(room=room), "under 15000"))

    question = result.room_question
    assert question is not None and question.kind is RoomQuestionKind.PIECES
    picked = {offer.key: offer for offer in question.pieces if offer.picked}
    assert set(picked) == {"sofa", "center-table"}
    assert all(offer.selected and offer.label.endswith("· your pick") for offer in picked.values())


def test_the_seat_chips_confirm_the_picks_or_add_to_them() -> None:
    question = RoomQuestion(
        room_kind="living_room", kind=RoomQuestionKind.SEATS, picked_seat_count=5
    )

    assert [chip.label for chip in room_answer_choices(question)] == [
        "Yes, 5",
        "6 people",
        "7 people",
        "8+ people",
    ]


def test_the_picks_seats_replace_an_earlier_head_count() -> None:
    room = RoomProjectState(room_kind="living_room", budget=BUDGET, pieces=("sofa",))

    question = next_question(room, LIVING, _stock(), 2, picked_seats=5)

    assert question is not None
    assert (question.picked_seat_count, question.earlier_seat_count) == (5, None)
    with pytest.raises(ValueError, match="replace an earlier head count"):
        RoomQuestion(
            room_kind="living_room",
            kind=RoomQuestionKind.SEATS,
            picked_seat_count=5,
            earlier_seat_count=2,
        )


@pytest.mark.parametrize(
    ("category", "subcategory", "piece"),
    [
        ("seating", "sofa", "sofa"),
        ("seating", "sectional-sofa", "sofa"),
        ("tables", "center-table", "center-table"),
        ("decor", "carpet", "rug"),
        ("bedroom", "bed", None),
    ],
)
def test_a_product_maps_onto_the_room_piece_it_fills(
    category: str, subcategory: str, piece: str | None
) -> None:
    found = piece_for(LIVING, category, subcategory)

    assert (found.key if found else None) == piece


# ── building the room ───────────────────────────────────────────────────────


async def test_the_picks_are_locked_into_the_room_when_it_is_built() -> None:
    optimizer = FakeOptimizer()
    coordinator, _ = _coordinator(_handoff(), optimizer=optimizer)
    room = RoomProjectState(
        room_kind="living_room",
        budget=BUDGET,
        pieces=("sofa", "center-table", "rug"),
        regular_seating_count=5,
        questions_done=True,
        anchor_product_ids=(THREE_SEATER, TABLE, TWO_SEATER),
    )

    result = await coordinator.run(_turn(_state(room=room), "leave the colours to you"))

    locked = {product.product.product_id for product in optimizer.requests[0].locked}
    assert locked == {THREE_SEATER, TABLE, TWO_SEATER}
    # Their seats already cover everyone, so no seating is planned beside them.
    assert not any(
        entry.need.commerce_category == "seating" for entry in optimizer.requests[0].discovery.needs
    )
    assert result.state.room_project is not None
    assert result.state.room_project.anchor_product_ids == ()


async def test_seats_beyond_the_picks_are_planned_for_the_remainder_only() -> None:
    """7 people, the picked sofas seat 5: the planner is asked for 2."""
    from tests.unit.test_room_questions_turn import ArrangingPlanner

    planner = ArrangingPlanner()
    coordinator, _ = _coordinator(_handoff(), seating_planner=planner)
    room = RoomProjectState(
        room_kind="living_room",
        budget=BUDGET,
        pieces=("sofa", "rug"),
        regular_seating_count=7,
        questions_done=True,
        anchor_product_ids=(THREE_SEATER, TWO_SEATER),
    )

    await coordinator.run(_turn(_state(room=room), "go ahead"))

    assert planner.asked[0]["target_seats"] == 2


def test_a_locked_sofa_never_fills_the_seats_planned_beyond_it() -> None:
    """The picked 2-seater's seats were already counted: the "2 more seats"
    need is bought new, never filled by the same locked sofa again."""
    loveseat = _item(201, "seating", "sofa", 2, "990")
    need = DesignCategoryNeed(
        commerce_category="seating",
        commerce_subcategory="sofa",
        priority=DesignPriority.REQUIRED,
        seating_capacity=SeatingCapacityConstraint(min_capacity=2, max_capacity=2),
    )
    discovery = DesignDiscoveryResult(
        needs=(
            DesignNeedCandidates(
                need_index=0,
                need=need,
                pool=CandidatePoolResult(
                    candidates=(RankedProductCandidate(product=loveseat, relaxation_depth=0),),
                    eligible_count=1,
                    was_relaxed=False,
                    stop_reason=StopReason.EXACT_SUFFICIENT,
                    semantic_used=False,
                ),
            ),
        )
    )
    locked = (
        LockedBundleProduct(
            product=CATALOG[TWO_SEATER], quantity=1, acquisition=BundleAcquisition.TO_BUY
        ),
    )

    counted_twice = BundleOptimizer().optimize(
        BundleOptimizationRequest(discovery=discovery, budget=BUDGET, locked=locked)
    )
    fresh = BundleOptimizer().optimize(
        BundleOptimizationRequest(
            discovery=discovery, budget=BUDGET, locked=locked, fresh_needs=frozenset({0})
        )
    )

    bought = lambda outcome: {  # noqa: E731
        line.product.product_id for line in outcome.lines if not line.locked
    }
    assert bought(counted_twice) == set()
    assert bought(fresh) == {201}
    assert {line.product.product_id for line in fresh.lines if line.locked} == {TWO_SEATER}


def _stock() -> RetailerCatalogCapabilities:
    return RetailerCatalogCapabilities(
        capabilities=tuple(
            RetailerCatalogCapability(
                commerce_category=category, commerce_subcategory=subcategory, active_product_count=5
            )
            for category, subcategory in LIVING_STOCK
        )
    )


# ── the edges ───────────────────────────────────────────────────────────────

NIGHTSTAND, UNKNOWN_SOFA, ARMCHAIR = 105, 106, 107
CATALOG[NIGHTSTAND] = _item(NIGHTSTAND, "tables", "nightstand", None, "560")
CATALOG[UNKNOWN_SOFA] = _item(UNKNOWN_SOFA, "seating", "sofa", None, "1800")
CATALOG[ARMCHAIR] = _item(ARMCHAIR, "seating", "chair", None, "420")


def _built(**room: Any) -> RoomProjectState:
    """A living room with every question answered, ready to build."""
    fields: dict[str, Any] = {
        "room_kind": "living_room",
        "budget": BUDGET,
        "pieces": ("sofa", "center-table", "rug"),
        "regular_seating_count": 5,
        "questions_done": True,
    }
    fields.update(room)
    return RoomProjectState(**fields)


async def test_picks_that_break_the_budget_are_kept_and_the_room_says_so() -> None:
    """8,030 of picks against 8,000: the optimiser's infeasible path - the picks
    stay, nothing is dropped to make the numbers work, the reply owns it."""
    from app.schemas.bundle import BundleStatus
    from app.services.response_view import route_response

    coordinator, _ = _coordinator(_handoff(), optimizer=BundleOptimizer())
    room = _built(
        budget=PriceConstraint.at_most(Decimal("8000"), "SAR"),
        anchor_product_ids=(THREE_SEATER, TABLE, TWO_SEATER),
    )

    result = await coordinator.run(_turn(_state(room=room), "go ahead"))

    assert result.bundle_outcome is not None
    assert result.bundle_outcome.status is BundleStatus.INFEASIBLE
    kept = {line.product.product_id for line in result.bundle_outcome.lines if line.locked}
    assert kept == {THREE_SEATER, TABLE, TWO_SEATER}
    view = route_response(result).primary
    assert view.bundle is not None and view.bundle.within_budget is False  # type: ignore[union-attr]


async def test_one_named_piece_survives_the_room_questions() -> None:
    """ "Design the room around the second sofa", asked before the budget: the
    named sofa is saved like a pick, not lost to the budget question."""
    from app.schemas.agent_decision import DesignAnchorIntent
    from app.schemas.product_reference import PresentedOrdinal
    from app.schemas.resolution import ResolvedProductReference

    from tests.unit.test_turn_coordinator import FakeReferences

    coordinator, _ = _coordinator(
        _handoff(
            design_anchor=DesignAnchorIntent(reference=PresentedOrdinal(position=2)),
            proposal={"room_kind": "living_room"},
        ),
        references=FakeReferences(default=ResolvedProductReference(product_id=THREE_SEATER)),
    )

    result = await coordinator.run(_turn(_state(room=None), "design the room around the second"))

    assert result.room_question is not None
    assert result.room_question.kind is RoomQuestionKind.BUDGET
    assert result.state.room_project is not None
    assert result.state.room_project.anchor_product_ids == (THREE_SEATER,)


async def test_a_bedroom_keeps_its_own_picks_and_never_asks_seats() -> None:
    """A bed and a nightstand are bedroom pieces; a sofa is not."""
    coordinator, _ = _coordinator(
        _handoff(anchor_picks=True, proposal={"room_kind": "bedroom"}),
        capabilities=FakeCapabilities(
            pairs=(("bedroom", "bed"), ("tables", "nightstand"), ("decor", "carpet"))
        ),
    )
    state = _with_picks(_state(room=None), BED, NIGHTSTAND, THREE_SEATER)

    first = await coordinator.run(_turn(state, "design my bedroom around these"))

    room = first.state.room_project
    assert room is not None and room.anchor_product_ids == (BED, NIGHTSTAND)
    later = room.model_copy(update={"budget": BUDGET, "pieces": ("bed", "nightstands", "rug")})
    coordinator, _ = _coordinator(
        _handoff(),
        capabilities=FakeCapabilities(
            pairs=(("bedroom", "bed"), ("tables", "nightstand"), ("decor", "carpet"))
        ),
    )
    asked = await coordinator.run(_turn(_state(room=later), "choose for me"))
    assert asked.room_question is not None
    assert asked.room_question.kind is RoomQuestionKind.COLOUR


@pytest.mark.parametrize(
    ("anchors", "seats"),
    [
        # An armchair seats one by review; a sofa whose count nobody recorded
        # adds nothing - the question never claims seats that are not known.
        ((THREE_SEATER, ARMCHAIR), 4),
        ((UNKNOWN_SOFA, ARMCHAIR), 1),
        ((UNKNOWN_SOFA,), None),
    ],
)
async def test_only_known_seats_are_counted(anchors: tuple[int, ...], seats: int | None) -> None:
    coordinator, _ = _coordinator(_handoff())
    room = RoomProjectState(
        room_kind="living_room", budget=BUDGET, pieces=("sofa",), anchor_product_ids=anchors
    )

    result = await coordinator.run(_turn(_state(room=room), "choose for me"))

    assert result.room_question is not None
    assert result.room_question.kind is RoomQuestionKind.SEATS
    assert result.room_question.picked_seat_count == seats


async def test_unticking_the_sofa_keeps_the_picked_sofas_but_plans_no_seating() -> None:
    """They took "Sofa · your pick" off the list: the sofas they picked still
    stay in the room - a pick is never dropped - but no more seating is bought."""
    optimizer = FakeOptimizer()
    coordinator, _ = _coordinator(_handoff(), optimizer=optimizer)
    room = _built(
        pieces=("center-table", "rug"),
        regular_seating_count=7,
        anchor_product_ids=(THREE_SEATER, TABLE, TWO_SEATER),
    )

    await coordinator.run(_turn(_state(room=room), "go ahead"))

    request = optimizer.requests[0]
    assert {lock.product.product_id for lock in request.locked} == {
        THREE_SEATER,
        TABLE,
        TWO_SEATER,
    }
    assert not any(e.need.commerce_category == "seating" for e in request.discovery.needs)


async def test_a_room_already_built_is_not_rebuilt_around_new_picks() -> None:
    """Once a room is planned, "around these" is a change to that room, which
    has its own path; nothing is saved as if the room were new."""
    from app.schemas.agent_state import RoomDesignNeedState

    coordinator, _ = _coordinator(_handoff(anchor_picks=True))
    room = RoomProjectState(
        room_kind="living_room",
        budget=BUDGET,
        design_needs=(
            RoomDesignNeedState(
                need_id=1,
                commerce_category="decor",
                commerce_subcategory="carpet",
                priority=DesignPriority.REQUIRED,
                quantity=1,
            ),
        ),
        next_design_need_id=2,
    )
    state = _with_picks(_state(room=room), THREE_SEATER)

    result = await coordinator.run(_turn(state, "rebuild it around these"))

    assert result.state.room_project is not None
    assert result.state.room_project.anchor_product_ids == ()


async def test_around_my_picks_with_no_picks_just_starts_the_room() -> None:
    coordinator, _ = _coordinator(
        _handoff(anchor_picks=True, proposal={"room_kind": "living_room"})
    )

    result = await coordinator.run(
        _turn(_state(room=None), "design my living room around my picks")
    )

    assert result.state.room_project is not None
    assert result.state.room_project.anchor_product_ids == ()
    assert result.room_question is not None
    assert result.room_question.kind is RoomQuestionKind.BUDGET


async def test_a_pick_already_in_the_room_is_locked_where_it_is_not_added_twice() -> None:
    from app.schemas.agent_state import BundleItemState

    from tests.unit.test_room_questions_turn import ArrangingPlanner

    optimizer = FakeOptimizer()
    coordinator, _ = _coordinator(
        _handoff(), optimizer=optimizer, seating_planner=ArrangingPlanner()
    )
    room = _built(
        bundle_items=(
            BundleItemState(
                line_id=1,
                product_id=TABLE,
                quantity=1,
                acquisition=BundleAcquisition.TO_BUY,
                status=BundleItemStatus.SUGGESTED,
            ),
        ),
        next_bundle_line_id=2,
        anchor_product_ids=(TABLE, THREE_SEATER),
    )

    await coordinator.run(_turn(_state(room=room), "go ahead"))

    locked = [lock.product.product_id for lock in optimizer.requests[0].locked]
    assert sorted(locked) == sorted([TABLE, THREE_SEATER])


@pytest.mark.parametrize(
    ("picks", "kind"),
    [
        # Sofas are living-room pieces only; a bed is a bedroom piece only.
        ((THREE_SEATER, TWO_SEATER), "living_room"),
        ((BED, NIGHTSTAND), "bedroom"),
        # An armchair belongs in both rooms' lists: nothing is guessed.
        ((ARMCHAIR,), None),
    ],
)
async def test_the_room_kind_comes_from_the_picks_when_it_was_not_named(
    picks: tuple[int, ...], kind: str | None
) -> None:
    """The model is asked to set the room kind; when "around my picks" came
    without one, the picks decide it - only when they point to one room."""
    coordinator, _ = _coordinator(_handoff(anchor_picks=True))
    state = _with_picks(_state(room=None), *picks)

    result = await coordinator.run(_turn(state, "build my room around these"))

    room = result.state.room_project
    assert (room.room_kind if room else None) == kind
    if kind is not None:
        assert room is not None and set(room.anchor_product_ids) == set(picks)
