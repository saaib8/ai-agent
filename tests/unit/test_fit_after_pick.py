"""A bed picked is checked against the room; a mattress comes in its size.

What these tests hold the feature to: a bed in focus with the room's size
unknown closes the turn on its length and width - once a session, never beside
another question, never for a piece the reviewed data does not check; typing
the size answers it, and the designer then judges that bed, not the cards on
screen; a mattress after a bed is ordered by the size the designer read off the
bed; and a fit between two pieces never asks for the room. Switched off,
nothing changes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from app.prompts.customer_commerce.v1 import build_instructions
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarificationReason,
    CustomerAgentDecision,
    CustomerStateProposal,
    DesignScope,
    FollowUpPolicy,
    RoomGeometryProposal,
    RoomMeasurementProposal,
    with_fit_after_pick,
)
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    InsideSize,
    ProductInteractionState,
    RoomCheck,
    RoomProjectState,
)
from app.schemas.agent_turn import CustomerTurnResult, TurnGrounding
from app.schemas.design import (
    DesignDirection,
    DesignGuidance,
    GuidanceTopic,
    InteriorDesignResult,
)
from app.schemas.geometry import RoomGeometry, RoomMeasurement, RoomMeasurementRole
from app.schemas.next_step import NextStepKind
from app.schemas.product import EligibleProduct
from app.schemas.query import RankingLean, ResolvedSearch
from app.schemas.relaxation import RelaxedCandidate
from app.schemas.retailer import RetailerContext
from app.services.grounding_builder import to_grounded_product
from app.services.semantic_ranking import SemanticRankingService
from app.taxonomy.briefs import load_briefs
from app.taxonomy.registry import load_taxonomy

from tests.unit.test_agent_state import _line
from tests.unit.test_product_brief import _builder
from tests.unit.test_turn_coordinator import (
    SOFAS,
    FakeDesign,
    FakeHydration,
    _coordinator,
    _product,
)
from tests.unit.test_turn_coordinator import _turn as turn_of

BED = 30
TAXONOMY = load_taxonomy()

ROOM = RoomGeometry(
    measurements=(
        RoomMeasurement(role=RoomMeasurementRole.ROOM_LENGTH, centimetres=Decimal("500")),
        RoomMeasurement(role=RoomMeasurementRole.ROOM_WIDTH, centimetres=Decimal("500")),
    )
)


def _engine(decision: CustomerAgentDecision | None = None, **kwargs: Any) -> Any:
    design = FakeDesign(
        InteriorDesignResult(
            guidance=(
                DesignGuidance(
                    topic=GuidanceTopic.SPACING,
                    summary="It leaves comfortable walking space on both sides.",
                ),
            )
        )
    )
    coordinator, parts = _coordinator(
        decision or CustomerAgentDecision(action=AgentAction.ANSWER),
        design=design,
        hydration=FakeHydration(available=(BED, 20, 21)),
        designer_fit=True,
        fit_after_pick=kwargs.pop("on", True),
        **kwargs,
    )
    coordinator._briefs = _builder()[0]
    return coordinator, parts


def _picked(kind: str = "bed", **state: Any) -> CustomerTurnResult:
    focus = to_grounded_product(
        _product(BED, subcategory=kind), grounding_ref=1, presented_ordinal=1, relaxation_depth=0
    )
    return CustomerTurnResult(
        state=AgentStateV1(
            product_interaction=ProductInteractionState(
                selected_product_ids=(BED,), focused_product_id=BED
            ),
            **state,
        ),
        decision=CustomerAgentDecision(action=AgentAction.ANSWER),
        grounding=TurnGrounding(),
        focus=focus,
    )


# ── the room's size, asked at the pick ──────────────────────────────────────


def test_beds_are_checked_against_the_room_when_picked() -> None:
    briefs = load_briefs(taxonomy=TAXONOMY)

    assert briefs.checks_room_on_pick("bed")
    assert not briefs.checks_room_on_pick("sofa")


def test_a_bed_picked_closes_on_the_rooms_size_once() -> None:
    coordinator, _ = _engine()

    result = coordinator._with_room_check(_picked())

    assert result.next_step is not None and result.next_step.kind is NextStepKind.ROOM_SIZE
    assert result.next_step.chips == ()
    assert result.state.room_check == RoomCheck(product_id=BED, kind="bed")
    # Asked once a session, whatever is picked next.
    again = coordinator._with_room_check(_picked(room_check=result.state.room_check))
    assert again.next_step is None


@pytest.mark.parametrize(
    "picked",
    [
        _picked(room_project=RoomProjectState(geometry=ROOM)),
        _picked(kind="sofa"),
        _picked(
            room_project=RoomProjectState(
                bundle_items=(_line(1, product_id=BED),), next_bundle_line_id=2
            )
        ),
    ],
    ids=["room-size-known", "not-checked-against-the-room", "a-piece-of-the-room"],
)
def test_the_rooms_size_is_never_asked_when_known_or_not_needed(
    picked: CustomerTurnResult,
) -> None:
    coordinator, _ = _engine()

    assert coordinator._with_room_check(picked).next_step is None


def test_switched_off_a_pick_asks_nothing() -> None:
    coordinator, _ = _engine(on=False)

    assert coordinator._with_room_check(_picked()).next_step is None


# ── its typed answer ────────────────────────────────────────────────────────


def _says_room(length: str = "5", width: str = "5") -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.ANSWER,
        state_proposal=CustomerStateProposal(
            room_geometry=RoomGeometryProposal(
                measurements=(
                    RoomMeasurementProposal(
                        role=RoomMeasurementRole.ROOM_LENGTH, value=length, unit="m"
                    ),
                    RoomMeasurementProposal(
                        role=RoomMeasurementRole.ROOM_WIDTH, value=width, unit="m"
                    ),
                )
            )
        ),
    )


async def test_the_size_typed_after_the_question_checks_that_bed() -> None:
    """Whatever the decision took "5x5 m" for, the designer judges the bed,
    in that room, and the check is done."""
    coordinator, parts = _engine(_says_room())
    state = AgentStateV1(
        active_search=ActiveSearchState(request=ProductSearchRequestFor("mattresses"), revision=1),
        product_interaction=ProductInteractionState(
            presented_product_ids=(20, 21),
            presented_search_revision=1,
            selected_product_ids=(BED,),
        ),
        room_check=RoomCheck(product_id=BED, kind="bed", list_revision=1),
    )

    result = await coordinator.run(turn_of(state, "my room is 5x5 m"))

    (request,) = parts["design"].requests
    assert request.geometry is not None
    assert len(request.anchors) == 1
    assert "bed" in (request.question or "")
    assert result.state.room_check is not None and result.state.room_check.answered
    assert result.grounding.design_guidance


def test_a_room_size_with_nothing_waiting_is_left_to_the_decision() -> None:
    coordinator, _ = _engine()
    decision = _says_room()

    assert coordinator._answers_room_check(decision, AgentStateV1()) is decision


# ── a piece in a piece ──────────────────────────────────────────────────────


async def test_a_mattress_in_a_bed_never_asks_the_rooms_size() -> None:
    fit = CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF,
        design_scope=DesignScope.ADVICE,
        fit_question=True,
        fit_with_piece=True,
        design_question="Will this mattress fit the bed I picked?",
    )
    coordinator, parts = _engine(fit)
    state = AgentStateV1(
        active_search=ActiveSearchState(request=ProductSearchRequestFor("mattresses"), revision=1),
        product_interaction=ProductInteractionState(
            presented_product_ids=(20, 21),
            presented_search_revision=1,
            selected_product_ids=(BED,),
        ),
    )

    result = await coordinator.run(turn_of(state, "will this mattress fit my bed?"))

    clarification = result.grounding.deterministic_clarification
    assert clarification is None or (
        clarification.reason is not BlockingClarificationReason.MISSING_ROOM_SIZE
    )
    (request,) = parts["design"].requests
    # What is on screen, and the bed they picked.
    assert len(request.anchors) == 3
    assert request.fit_checks == ()


def test_the_decision_sees_fit_with_piece_only_when_it_is_on() -> None:
    from app.schemas.agent_decision import CustomerAgentDecision as Plain

    assert "fit_with_piece" not in Plain.model_json_schema()["properties"]
    assert "fit_with_piece" in with_fit_after_pick(Plain).model_json_schema()["properties"]
    assert "fit_with_piece" not in build_instructions()
    assert "fit_with_piece" in build_instructions(fit_after_pick=True)


# ── a mattress in the bed's size ────────────────────────────────────────────


def _mattress(product_id: int, width: str) -> RelaxedCandidate:
    return RelaxedCandidate(
        product=EligibleProduct(
            product_id=product_id,
            subcategory="mattresses",
            price_amount=Decimal("900"),
            long_side_cm=Decimal("200"),
            short_side_cm=Decimal(width),
        ),
        relaxation_depth=0,
    )


async def test_the_mattress_in_the_beds_size_comes_first() -> None:
    mattresses = ProductSearchRequestFor("mattresses")
    resolved = ResolvedSearch(request=mattresses, lean=RankingLean(fit_side_cm=Decimal("180")))
    cards = [_mattress(1, "120"), _mattress(2, "160"), _mattress(3, "180"), _mattress(4, "200")]

    ranked = await SemanticRankingService(None, None).rank(
        resolved, cards, RetailerContext(store_id=50), namespace="store-50"
    )

    assert ranked.product_ids[0] == 3


def ProductSearchRequestFor(subcategory: str) -> Any:  # noqa: N802
    return SOFAS.model_copy(
        update={"commerce_category": "bedroom", "commerce_subcategory": subcategory}
    )


def test_the_designers_size_for_the_pick_orders_only_when_on() -> None:
    from app.services.turn_coordinator import _directed, _DirectionContext

    direction = DesignDirection(fits_inside_cm=180)
    resolved = ResolvedSearch(request=ProductSearchRequestFor("mattresses"))

    on = _directed(
        resolved, direction, _DirectionContext(None, frozenset(), fits_inside=True)
    )
    off = _directed(
        resolved, direction, _DirectionContext(None, frozenset(), fits_inside=False)
    )

    assert on.lean is not None and on.lean.fit_side_cm == Decimal("180")
    assert off.lean is None or off.lean.fit_side_cm is None


@pytest.mark.parametrize("language", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("fit", [False, True])
def test_the_decision_schema_name_stays_within_the_providers_limit(
    language: bool, mixed: bool, fit: bool
) -> None:
    """Stacked, the transport layers once named it 75 characters; the
    provider refuses more than 64 and every turn failed."""
    from app.services.customer_decision import MAX_SCHEMA_NAME, CustomerAgentDecisionService
    from app.taxonomy.attributes import load_catalog_attributes

    service = CustomerAgentDecisionService(
        cast(Any, None),
        load_catalog_attributes(),
        reply_language=language,
        mixed_types=mixed,
        fit_after_pick=fit,
    )

    assert len(service._schema.__name__) <= MAX_SCHEMA_NAME


def test_the_rooms_size_takes_the_place_of_the_kinds_beside_the_pick() -> None:
    """ "Also goes well with it" chips are not a question: the room's size is
    asked, alone, so its answer is typed and nothing else is tapped for it."""
    from app.schemas.product_action import CompanionOffer

    coordinator, _ = _engine()
    rugs = CompanionOffer(category="decor", subcategory="carpet", label="Rugs")
    offered = _picked().model_copy(update={"companions": (rugs,)})

    result = coordinator._with_room_check(offered)

    assert result.next_step is not None and result.next_step.kind is NextStepKind.ROOM_SIZE
    assert result.companions == ()


def test_the_size_read_off_the_pick_survives_the_stocked_values_check() -> None:
    from app.schemas.design import DesignCategoryNeed, DesignPriority, StockedLook
    from app.services.interior_design import _with_stocked_direction

    need = DesignCategoryNeed(
        commerce_category="bedroom",
        commerce_subcategory="mattresses",
        priority=DesignPriority.RECOMMENDED,
        direction=DesignDirection(colours=("White", "Teal"), fits_inside_cm=180),
    )

    looks = (StockedLook(commerce_subcategory="mattresses", colours=("White",)),)
    kept = _with_stocked_direction(need, looks, fits_inside=True)
    off = _with_stocked_direction(need, looks)

    assert kept.direction is not None
    assert kept.direction.colours == ("White",)
    assert kept.direction.fits_inside_cm == 180
    assert off.direction is not None and off.direction.fits_inside_cm is None


def test_switched_off_the_designer_is_told_nothing_of_fit_after_pick() -> None:
    from app.prompts.interior_design.v1 import build_instructions as design_instructions

    assert "fits_inside_cm" not in design_instructions(TAXONOMY)
    assert "fits_inside_cm" in design_instructions(TAXONOMY, fit_after_pick=True)


# ── only its own answer answers it ──────────────────────────────────────────


def _waiting(revision: int = 1, **interaction: Any) -> AgentStateV1:
    """The room's size asked beside list 1, with list `revision` on screen."""
    shown: dict[str, Any] = {
        "presented_product_ids": (20, 21),
        "presented_search_revision": revision,
        "selected_product_ids": (BED,),
    }
    return AgentStateV1(
        active_search=ActiveSearchState(
            request=ProductSearchRequestFor("mattresses"), revision=revision
        ),
        product_interaction=ProductInteractionState(**(shown | interaction)),
        room_check=RoomCheck(product_id=BED, kind="bed", list_revision=1),
    )


def test_the_question_is_asked_beside_the_list_on_screen() -> None:
    coordinator, _ = _engine()
    shown = _picked().state.product_interaction.model_copy(update={"presented_search_revision": 4})
    picked = _picked().model_copy(
        update={
            "state": _picked().state.model_copy(
                update={
                    "product_interaction": shown,
                    "active_search": ActiveSearchState(
                        request=ProductSearchRequestFor("mattresses"), revision=4
                    ),
                }
            )
        }
    )

    result = coordinator._with_room_check(picked)

    assert result.state.room_check is not None and result.state.room_check.list_revision == 4
    # The reply's one question: none of its own beside it.
    assert result.grounding.follow_up_policy is FollowUpPolicy.NONE


@pytest.mark.parametrize(
    "reply",
    [
        CustomerAgentDecision(
            action=AgentAction.DESIGN_HANDOFF,
            design_scope=DesignScope.WHOLE_ROOM,
            state_proposal=_says_room().state_proposal,
        ),
        CustomerAgentDecision(
            action=AgentAction.SEARCH, state_proposal=_says_room().state_proposal
        ),
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            state_proposal=CustomerStateProposal(
                room_geometry=RoomGeometryProposal(
                    measurements=(
                        RoomMeasurementProposal(role=RoomMeasurementRole.ROOM_LENGTH, value="5"),
                        RoomMeasurementProposal(role=RoomMeasurementRole.ROOM_WIDTH, value="4"),
                    )
                )
            ),
        ),
    ],
    ids=["a-room-to-design", "a-new-search", "a-size-without-its-unit"],
)
def test_only_a_reply_that_gives_the_size_alone_answers_it(
    reply: CustomerAgentDecision,
) -> None:
    coordinator, _ = _engine()

    assert coordinator._answers_room_check(reply, _waiting()) is reply


@pytest.mark.parametrize(
    "moved_on",
    [{"revision": 2}, {"selected_product_ids": ()}],
    ids=["another-list-replaced-it", "the-bed-was-unticked"],
)
def test_the_question_lapses_once_they_move_on(moved_on: dict[str, Any]) -> None:
    from app.services.fit import awaiting_room_check

    assert awaiting_room_check(_waiting()) is not None
    assert awaiting_room_check(_waiting(**moved_on)) is None


def test_the_reply_says_the_bed_size_comes_first_only_when_it_does() -> None:
    from app.core.config import SizeSettings
    from app.schemas.design import DesignCategoryNeed, DesignPriority
    from app.schemas.response import DirectionView

    need = DesignCategoryNeed(
        commerce_category="bedroom",
        commerce_subcategory="mattresses",
        priority=DesignPriority.RECOMMENDED,
        direction=DesignDirection(fits_inside_cm=90),
    )
    lean = RankingLean(fit_side_cm=Decimal("90"))

    assert DirectionView.of(need, lean, SizeSettings(), first_fits=True).sized_for_the_pick
    assert not DirectionView.of(need, lean, SizeSettings()).sized_for_the_pick


# ── what goes inside a pick, searched for later ─────────────────────────────


def _inside(width: int = 180) -> InsideSize:
    return InsideSize(product_id=BED, pick_kind="bed", kind="mattresses", width_cm=width)


def test_the_designers_reading_is_kept_at_the_pick() -> None:
    from app.schemas.design import DesignCategoryNeed, DesignPriority
    from app.services.turn_coordinator import _remembering_inside

    def need(subcategory: str, direction: DesignDirection) -> DesignCategoryNeed:
        return DesignCategoryNeed(
            commerce_category="bedding",
            commerce_subcategory=subcategory,
            priority=DesignPriority.RECOMMENDED,
            direction=direction,
        )

    bed = _product(BED, subcategory="bed")
    kept = _remembering_inside(
        AgentStateV1(),
        bed,
        (
            need("mattresses", DesignDirection(fits_inside_cm=180)),
            need("carpet", DesignDirection(colours=("Beige",))),
        ),
    )
    again = _remembering_inside(
        kept, bed, (need("mattresses", DesignDirection(fits_inside_cm=160)),)
    )

    assert kept.inside_sizes == (_inside(),)
    # A new reading of the same pick replaces the old one.
    assert again.inside_sizes == (_inside(160),)


def test_their_own_search_for_it_comes_in_that_size_while_the_pick_is_theirs() -> None:
    from app.schemas.composition import ComposedSearch
    from app.services.turn_coordinator import _sized_for

    coordinator, _ = _engine()
    off, _ = _engine(on=False)
    picked = AgentStateV1(
        product_interaction=ProductInteractionState(selected_product_ids=(BED,)),
        inside_sizes=(_inside(),),
    )
    mattresses = ProductSearchRequestFor("mattresses")
    composed = ComposedSearch(
        candidate=ActiveSearchState(request=mattresses, revision=1),
        resolved=ResolvedSearch(request=mattresses),
    )

    sized = _sized_for(composed, coordinator._inside_their_pick(picked, "mattresses"))

    assert sized.candidate.lean is not None and sized.resolved.lean is not None
    assert sized.candidate.lean.fit_side_cm == sized.resolved.lean.fit_side_cm == Decimal("180")
    unticked = picked.model_copy(update={"product_interaction": ProductInteractionState()})
    assert coordinator._inside_their_pick(unticked, "mattresses") is None
    assert coordinator._inside_their_pick(picked, "carpet") is None
    assert off._inside_their_pick(picked, "mattresses") is None


@pytest.mark.parametrize(("first_width", "said"), [("180", True), ("160", False)])
def test_the_reply_is_told_only_when_the_first_card_is_that_size(
    first_width: str, said: bool
) -> None:
    from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
    from app.schemas.grounding import SearchExecutionGrounding, SearchOutcome
    from app.schemas.relaxation import StopReason
    from app.services.turn_coordinator import _Primary

    coordinator, _ = _engine()
    mattress = _product(20, subcategory="mattresses").model_copy(
        update={
            "dimensions": NormalisedDimensions(
                status=DimensionStatus.NORMALISED,
                length_cm=Decimal("200"),
                width_cm=Decimal(first_width),
            )
        }
    )
    shown = _Primary(
        state=AgentStateV1(),
        search=SearchExecutionGrounding(
            outcome=SearchOutcome.RESULTS,
            products=(
                to_grounded_product(
                    mattress, grounding_ref=1, presented_ordinal=1, relaxation_depth=0
                ),
            ),
            eligible_count=1,
            ranked_count=1,
            selected_count=1,
            presented_count=1,
            exact_candidate_count=1,
            stop_reason=StopReason.EXACT_SUFFICIENT,
        ),
    )

    told = coordinator._said_sized(shown, _inside())

    assert told.search is not None
    assert (told.search.sized_for_pick == "bed") is said
