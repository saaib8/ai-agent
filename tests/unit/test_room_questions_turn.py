"""A living room or a bedroom, asked about one question per turn, then built
from the pieces the customer chose (CLAUDE.md 10.1, 10.3, 27.1).

Turn-level: the coordinator asks what is still missing before anything is
designed, records each question as asked, turns a model-written room question
into its own, and builds the room from exactly the chosen pieces - its seating
sized to the head count.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CustomerAgentDecision,
    CustomerStateProposal,
    FollowUpPolicy,
    PriceProposal,
)
from app.schemas.agent_state import ActiveSearchState, RoomProjectState
from app.schemas.design import DesignPriority
from app.schemas.design_discovery import DesignDiscoveryResult, DesignNeedCandidates
from app.schemas.discovery import PriceConstraint, ProductSearchRequest, SeatingCapacityConstraint
from app.schemas.query import ConstraintSemantics, ConstraintStrength, SemanticPreference
from app.schemas.relaxation import StopReason
from app.schemas.resolution import CandidatePoolResult, RankedProductCandidate
from app.schemas.room_opener import RoomQuestionKind
from app.schemas.seating_solution import SeatingArrangement, SeatingArrangementLine
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics

from tests.unit.test_turn_coordinator import (
    FakeCapabilities,
    FakeDesign,
    FakeDesignDiscovery,
    FakeOptimizer,
    FakePipeline,
    FakeSeatingPlanner,
    _coordinator,
    _product,
    _state,
    _turn,
)

TAXONOMY = load_taxonomy()
ROOMS = load_room_pieces(taxonomy=TAXONOMY, seating=load_seating_semantics(taxonomy=TAXONOMY))

LIVING_STOCK = (
    ("seating", "sofa"),
    ("seating", "sofa-set"),
    ("tables", "center-table"),
    ("decor", "carpet"),
)
BUDGET = PriceConstraint.at_most(Decimal("12000"), "SAR")
BEIGE = SemanticPreference(
    family=AttributeFamily.COLOR,
    raw_value="beige",
    canonical_value="Beige",
    strength=ConstraintStrength.PREFERRED,
)


def _handoff(**proposal: Any) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF,
        state_proposal=CustomerStateProposal(**proposal) if proposal else None,
    )


def _room(**fields: Any) -> RoomProjectState:
    return RoomProjectState(room_kind="living_room", **fields)


class ArrangingPlanner(FakeSeatingPlanner):
    """Seats the room with the arrangements it is given, recording the ask."""

    def __init__(self, arrangements: tuple[SeatingArrangement, ...] = ()) -> None:
        super().__init__()
        self.arrangements_given = arrangements
        self.asked: list[dict[str, Any]] = []

    async def arrangements(self, **kwargs: Any) -> tuple[SeatingArrangement, ...]:
        self.asked.append(kwargs)
        return self.arrangements_given


class ForcingPipeline(FakePipeline):
    async def execute_forced_pool(self, product_id: int, context: Any) -> CandidatePoolResult:
        return CandidatePoolResult(
            candidates=(RankedProductCandidate(product=_product(product_id), relaxation_depth=0),),
            eligible_count=1,
            was_relaxed=False,
            stop_reason=StopReason.EXACT_SUFFICIENT,
            semantic_used=False,
        )


def _parts(**kwargs: Any) -> tuple[Any, dict[str, Any]]:
    kwargs.setdefault("capabilities", FakeCapabilities(pairs=LIVING_STOCK))
    return _coordinator(kwargs.pop("decision"), rooms=ROOMS, **kwargs)


# ── the questions ───────────────────────────────────────────────────────────


async def test_a_new_living_room_asks_for_the_budget_first_and_designs_nothing() -> None:
    coordinator, parts = _parts(decision=_handoff(room_kind="living_room"))

    result = await coordinator.run(_turn(_state(room=None), "design my living room"))

    assert result.room_question is not None
    assert result.room_question.kind is RoomQuestionKind.BUDGET
    assert result.state.room_project is not None
    assert result.state.room_project.room_kind == "living_room"
    assert result.state.room_project.questions_asked == (RoomQuestionKind.BUDGET,)
    assert parts["design"].requests == []
    assert parts["optimizer"].requests == []


async def test_an_answer_is_recorded_and_the_next_question_follows() -> None:
    coordinator, _ = _parts(
        decision=_handoff(room_budget=PriceProposal(max_amount="12000", currency="SAR"))
    )
    state = _state(room=_room(questions_asked=(RoomQuestionKind.BUDGET,)))

    result = await coordinator.run(_turn(state, "under 12000 SAR"))

    room = result.state.room_project
    assert room is not None and room.budget is not None
    assert result.room_question is not None
    assert result.room_question.kind is RoomQuestionKind.PIECES
    assert [p.key for p in result.room_question.pieces] == ["sofa", "center-table", "rug"]
    assert room.questions_asked == (RoomQuestionKind.BUDGET, RoomQuestionKind.PIECES)


async def test_a_model_written_room_question_becomes_the_applications_own() -> None:
    decision = CustomerAgentDecision(
        action=AgentAction.CLARIFY,
        clarification=BlockingClarification(
            reason=BlockingClarificationReason.MISSING_ROOM_REQUIREMENTS,
            question="What's your budget, how big is the room, and what style?",
        ),
        state_proposal=CustomerStateProposal(room_kind="living_room"),
        follow_up_policy=FollowUpPolicy.NONE,
    )
    coordinator, _ = _parts(decision=decision)

    result = await coordinator.run(_turn(_state(room=None), "design my living room"))

    assert result.room_question is not None
    assert result.room_question.kind is RoomQuestionKind.BUDGET


async def test_choose_for_me_records_the_usual_pieces() -> None:
    coordinator, _ = _parts(decision=_handoff(room_pieces_default=True))
    state = _state(room=_room(budget=BUDGET, questions_asked=(RoomQuestionKind.BUDGET,)))

    result = await coordinator.run(_turn(state, "choose for me"))

    room = result.state.room_project
    assert room is not None and room.pieces is not None
    assert {"sofa", "center-table", "rug", "wall-art"} <= set(room.pieces)
    assert "chandelier" not in room.pieces


async def test_an_unknown_piece_key_is_dropped() -> None:
    coordinator, _ = _parts(decision=_handoff(room_pieces=("sofa", "throne", "rug")))
    state = _state(room=_room(budget=BUDGET))

    result = await coordinator.run(_turn(state, "a sofa, a throne and a rug"))

    assert result.state.room_project is not None
    assert result.state.room_project.pieces == ("sofa", "rug")


async def test_a_head_count_from_an_earlier_sofa_search_is_offered_to_confirm() -> None:
    search = ActiveSearchState(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sofa",
            seating_capacity=SeatingCapacityConstraint(min_capacity=9),
        ),
        semantics=ConstraintSemantics(),
        revision=1,
    )
    state = _state(room=_room(budget=BUDGET, pieces=("sofa",))).model_copy(
        update={"active_search": search}
    )
    coordinator, _ = _parts(decision=_handoff())

    result = await coordinator.run(_turn(state, "let's do the room"))

    assert result.room_question is not None
    assert result.room_question.kind is RoomQuestionKind.SEATS
    assert result.room_question.earlier_seat_count == 9
    assert result.state.room_project is not None
    assert result.state.room_project.regular_seating_count is None


async def test_just_design_it_builds_at_once() -> None:
    coordinator, parts = _parts(decision=_handoff(room_skip_questions=True))

    result = await coordinator.run(_turn(_state(room=_room()), "just design it"))

    assert result.room_question is None
    assert len(parts["design"].requests) == 1


async def test_a_room_the_registry_does_not_know_is_planned_as_before() -> None:
    coordinator, parts = _parts(decision=_handoff(room_kind="home_office"))

    result = await coordinator.run(_turn(_state(room=None), "design my office"))

    assert result.room_question is None
    assert len(parts["design"].requests) == 1


# ── the room they chose ─────────────────────────────────────────────────────


def _room_discovery(*subcategories: str) -> DesignDiscoveryResult:
    from app.schemas.design import DesignCategoryNeed

    return DesignDiscoveryResult(
        needs=tuple(
            DesignNeedCandidates(
                need_index=position,
                need=DesignCategoryNeed(
                    commerce_category=category,
                    commerce_subcategory=subcategory,
                    priority=DesignPriority.REQUIRED,
                ),
                pool=CandidatePoolResult(
                    candidates=(),
                    eligible_count=0,
                    was_relaxed=False,
                    stop_reason=StopReason.EXACT_SUFFICIENT,
                    semantic_used=False,
                ),
            )
            for position, (category, subcategory) in enumerate(
                (("tables", s) if s == "center-table" else ("decor", s)) for s in subcategories
            )
        )
    )


async def test_the_room_holds_exactly_the_chosen_pieces_with_seating_for_everyone() -> None:
    arrangement = SeatingArrangement(
        lines=(
            SeatingArrangementLine(
                product_id=41, commerce_subcategory="sofa-set", quantity=1, seats_each=6
            ),
            SeatingArrangementLine(
                product_id=42, commerce_subcategory="sofa", quantity=1, seats_each=3
            ),
        ),
        total_price=Decimal("8600"),
    )
    planner = ArrangingPlanner((arrangement,))
    discovery = FakeDesignDiscovery(_room_discovery("center-table", "carpet"))
    optimizer = FakeOptimizer()
    coordinator, _ = _parts(
        decision=_handoff(),
        seating_planner=planner,
        design_discovery=discovery,
        optimizer=optimizer,
        pipeline=ForcingPipeline(),
        design=FakeDesign(),
    )
    room = _room(
        budget=BUDGET,
        pieces=("sofa", "center-table", "rug"),
        regular_seating_count=9,
        design_preferences=(BEIGE,),
    )

    await coordinator.run(_turn(_state(room=room), "beige please"))

    # The designer is not asked what to include: the searched plan is the chosen
    # pieces less the seating, which is sized separately.
    _, searched, _ = discovery.calls[0]
    assert [n.commerce_subcategory for n in searched.needs] == ["center-table", "carpet"]
    assert planner.asked[0]["target_seats"] == 9
    assert planner.asked[0]["budget_amount"] == Decimal("12000")
    assert planner.asked[0]["types"] == frozenset(
        ROOMS.template("living_room").seating.seating_types
    )  # type: ignore[union-attr]
    assert planner.asked[0]["requirements"].wished_colors == ("Beige",)

    optimised = optimizer.requests[0].discovery.needs
    assert [(e.need.commerce_subcategory, e.need.quantity) for e in optimised] == [
        ("sofa-set", 1),
        ("sofa", 1),
        ("center-table", 1),
        ("carpet", 1),
    ]
    # Each seat need keeps its piece's seat count, so a swap brings back one
    # that seats the same and the head count holds.
    capacities = [e.need.seating_capacity for e in optimised[:2]]
    assert [(c.min_capacity, c.max_capacity) for c in capacities if c] == [(6, 6), (3, 3)]
    assert [e.need_index for e in optimised] == [0, 1, 2, 3]


async def test_without_a_head_count_the_room_gets_one_sofa() -> None:
    planner = ArrangingPlanner()
    discovery = FakeDesignDiscovery(_room_discovery("carpet"))
    coordinator, _ = _parts(
        decision=_handoff(),
        seating_planner=planner,
        design_discovery=discovery,
        design=FakeDesign(),
    )
    room = _room(
        budget=BUDGET,
        pieces=("sofa", "rug"),
        questions_asked=tuple(RoomQuestionKind),
    )

    await coordinator.run(_turn(_state(room=room), "go ahead"))

    assert planner.asked == []
    _, seat_plan, _ = discovery.calls[1]
    assert [(n.commerce_subcategory, n.seating_capacity) for n in seat_plan.needs] == [
        ("sofa", None)
    ]


async def test_the_rooms_real_seats_are_counted_from_its_pieces() -> None:
    """A chair records no capacity but seats one by review; a sofa its own."""
    from app.schemas.acquisition import BundleAcquisition
    from app.schemas.bundle import BundleLine, BundleStatus, RoomBundle
    from app.schemas.product import CommerceClassification

    sofa = _product(51).model_copy(
        update={
            "commerce": CommerceClassification(
                category="seating", subcategory="sofa", seating_capacity=3
            )
        }
    )
    chair = _product(52, subcategory="single-seater-sofa")
    outcome = RoomBundle(
        lines=tuple(
            BundleLine(
                need_index=None,
                product=product,
                quantity=quantity,
                locked=False,
                acquisition=BundleAcquisition.TO_BUY,
                relaxation_depth=0,
            )
            for product, quantity in ((sofa, 2), (chair, 2))
        ),
        status=BundleStatus.COMPLETE,
        new_spend_total=Decimal("1"),
        currency="SAR",
    )
    coordinator, _ = _parts(
        decision=_handoff(room_skip_questions=True),
        optimizer=FakeOptimizer(outcome),
        seating=load_seating_semantics(taxonomy=TAXONOMY),
    )
    room = _room(budget=BUDGET, pieces=("rug",), regular_seating_count=8)

    result = await coordinator.run(_turn(_state(room=room), "go"))

    assert result.room_seats == 8
