"""What the customer is told about a room of chosen pieces (CLAUDE.md 10.3).

A missing piece is named, never counted - "the rug didn't fit the budget", not
"1 needed piece couldn't be included" - with the lowest real price that would
fill it when the budget was the reason. The head count the seating was built
for is said back. A room question is its own reply, with its chips.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.agent_state import AgentStateV1, RoomProjectState
from app.schemas.agent_turn import CustomerTurnResult, TurnGrounding
from app.schemas.bundle import BundleOptimizationRequest, BundleStatus, UnmetReason
from app.schemas.design import DesignCategoryNeed, DesignPriority
from app.schemas.design_discovery import DesignDiscoveryResult, DesignNeedCandidates
from app.schemas.discovery import PriceConstraint
from app.schemas.relaxation import StopReason
from app.schemas.resolution import CandidatePoolResult, RankedProductCandidate
from app.schemas.response import ResponseGroundingView, ResponseOutcomeKind
from app.schemas.room_opener import RoomPieceOffer, RoomQuestion, RoomQuestionKind
from app.services.bundle_optimizer import BundleOptimizer
from app.services.response_view import route_response
from app.services.response_wording import fallback_for
from app.services.room_presentation import piece_picker
from app.taxonomy.rooms import PieceTier

from tests.unit.test_bundle_response import bundle, line, product

BUDGET = PriceConstraint.at_most(Decimal("2000"), "SAR")


def _pool(*prices: str) -> CandidatePoolResult:
    return CandidatePoolResult(
        candidates=tuple(
            RankedProductCandidate(product=product(100 + n, price=price), relaxation_depth=0)
            for n, price in enumerate(prices)
        ),
        eligible_count=len(prices),
        was_relaxed=False,
        stop_reason=StopReason.EXACT_SUFFICIENT,
        semantic_used=False,
    )


def _need(category: str, subcategory: str, priority: DesignPriority) -> DesignCategoryNeed:
    return DesignCategoryNeed(
        commerce_category=category, commerce_subcategory=subcategory, priority=priority
    )


def _result(
    outcome: object, room: RoomProjectState, seats: int | None = None
) -> CustomerTurnResult:
    return CustomerTurnResult(
        state=AgentStateV1(room_project=room),
        decision=CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        grounding=TurnGrounding(design_handoff_requested=True),
        bundle_outcome=outcome,  # type: ignore[arg-type]
        room_seats=seats,
    )


def _view(result: CustomerTurnResult) -> ResponseGroundingView:
    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    return route.primary


# ── naming what is missing ──────────────────────────────────────────────────


def test_a_piece_the_budget_could_not_reach_is_named_with_its_lowest_price() -> None:
    outcome = BundleOptimizer().optimize(
        BundleOptimizationRequest(
            discovery=DesignDiscoveryResult(
                needs=(
                    DesignNeedCandidates(
                        need_index=0,
                        need=_need("seating", "sofa", DesignPriority.REQUIRED),
                        pool=_pool("1800"),
                    ),
                    DesignNeedCandidates(
                        need_index=1,
                        need=_need("decor", "carpet", DesignPriority.RECOMMENDED),
                        pool=_pool("390", "700"),
                    ),
                    DesignNeedCandidates(
                        need_index=2,
                        need=_need("lighting", "floor-lamp", DesignPriority.OPTIONAL),
                        pool=_pool(),
                    ),
                )
            ),
            budget=BUDGET,
        )
    )

    missing = _view(_result(outcome, RoomProjectState(budget=BUDGET))).bundle
    assert missing is not None
    named = {(p.piece, p.reason, p.cheapest_price) for p in missing.missing_pieces}
    assert named == {
        ("carpet", UnmetReason.BUDGET_EXHAUSTED, Decimal("390")),
        ("floor lamp", UnmetReason.NO_CANDIDATES, None),
    }


def test_a_missing_piece_reaches_the_reply_as_words_never_a_key() -> None:
    from app.schemas.bundle import UnmetNeed

    outcome = bundle(
        line(),
        status=BundleStatus.PARTIAL,
        unmet_needs=(
            UnmetNeed(
                need_index=1,
                priority=DesignPriority.REQUIRED,
                shortfall=1,
                reason=UnmetReason.NO_CANDIDATES,
                commerce_category="tables",
                commerce_subcategory="center-table",
            ),
        ),
    )

    view = _view(_result(outcome, RoomProjectState()))

    assert view.bundle is not None
    assert [p.piece for p in view.bundle.missing_pieces] == ["center table"]


# ── the head count ──────────────────────────────────────────────────────────


def test_the_head_count_is_said_back_when_the_seats_add_up() -> None:
    room = RoomProjectState(room_kind="living_room", regular_seating_count=9)

    view = _view(_result(bundle(line()), room, seats=9))

    assert view.bundle is not None
    assert (view.bundle.seating_for, view.bundle.seats_short_of) == (9, None)


def test_a_room_that_seats_fewer_says_so_and_claims_nothing_more() -> None:
    """A swap put a two-seater where the three-seater was: eight seats for
    nine people is never "seating for all nine"."""
    room = RoomProjectState(room_kind="living_room", regular_seating_count=9)

    view = _view(_result(bundle(line()), room, seats=8))

    assert view.bundle is not None
    assert (view.bundle.seating_for, view.bundle.seats_short_of) == (None, 9)


@pytest.mark.parametrize(
    ("room", "seats"),
    [
        # A room planned the older way sized nothing to a head count.
        (RoomProjectState(regular_seating_count=9), 9),
        # They never said how many.
        (RoomProjectState(room_kind="living_room"), 3),
        # Seats nobody could count.
        (RoomProjectState(room_kind="living_room", regular_seating_count=9), None),
    ],
)
def test_no_head_count_is_claimed_that_the_room_was_not_built_for(
    room: RoomProjectState, seats: int | None
) -> None:
    view = _view(_result(bundle(line()), room, seats=seats))

    assert view.bundle is not None
    assert view.bundle.seating_for is None and view.bundle.seats_short_of is None


# ── the question ────────────────────────────────────────────────────────────

PIECES = RoomQuestion(
    room_kind="living_room",
    kind=RoomQuestionKind.PIECES,
    pieces=(
        RoomPieceOffer(key="sofa", label="Sofa", tier=PieceTier.ESSENTIAL, selected=True),
        RoomPieceOffer(key="vase", label="Vase", tier=PieceTier.OPTIONAL, selected=False),
    ),
)


def test_a_room_question_is_the_whole_reply_even_over_a_model_question() -> None:
    result = CustomerTurnResult(
        state=AgentStateV1(),
        decision=CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        grounding=TurnGrounding(design_handoff_requested=True),
        room_question=PIECES,
    )

    view = _view(result)

    assert view.kind is ResponseOutcomeKind.ROOM_QUESTION
    assert view.room_question is not None
    assert view.room_question.room_kind == "living room"
    assert (view.room_question.pieces_offered, view.room_question.pieces_preselected) == (2, 1)


def test_the_pieces_become_chips_with_the_usual_ones_ticked() -> None:
    picker = piece_picker(PIECES)

    assert picker is not None
    assert [(c.label, c.selected, c.essential) for c in picker.pieces] == [
        ("Sofa", True, True),
        ("Vase", False, False),
    ]
    assert piece_picker(RoomQuestion(room_kind="living_room", kind=RoomQuestionKind.BUDGET)) is None


@pytest.mark.parametrize("kind", list(RoomQuestionKind))
def test_every_room_question_has_fallback_wording(kind: RoomQuestionKind) -> None:
    wording = fallback_for(ResponseOutcomeKind.ROOM_QUESTION, room_question=kind)

    assert wording.endswith("?") or "choose" in wording
    assert not any(ch.isdigit() for ch in wording)
