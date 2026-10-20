"""A finishing touch for a finished room (CLAUDE.md 10.3).

"Add a finishing touch" - tapped, or typed - is shown, never asked about: Nora
chooses the piece the room lacks that would best finish it, and a page of it
is shown with the other pieces as chips. Each card's "Add to room" adds it to
the room package, never the cart; over budget the customer is asked, as for a
dearer swap, whether to increase the budget or keep the plan.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarificationReason,
    CommercialReason,
    CustomerAgentDecision,
    DesignScope,
)
from app.schemas.agent_state import (
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    RoomProjectState,
    SwapBudgetOfferStage,
    SwapBudgetOfferState,
)
from app.schemas.agent_turn import CustomerResponse, CustomerTurnInput, CustomerTurnResult
from app.schemas.bundle import BundleLine, BundleStatus, RoomBundle
from app.schemas.bundle_action import (
    AddPieceAction,
    AddToRoomAction,
    BundleActionRequest,
    FinishingTouchesAction,
    SwapAlternativesAction,
    SwapConfirmAction,
    SwapDeclineAction,
    SwapKeepOriginalAction,
)
from app.schemas.design import DesignCategoryNeed, DesignPriority, DesignTask, InteriorDesignResult
from app.schemas.discovery import PriceConstraint
from app.schemas.grounding import TurnFailureCode
from app.schemas.language import ReplyLanguage
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.relaxation import StopReason
from app.schemas.resolution import (
    CandidatePoolResult,
    RankedProductCandidate,
    ResolvedProductReference,
)
from app.services.chat_runtime import ChatRuntime
from app.services.design_discovery import DesignDiscoveryService
from app.services.room_composition import MAX_FINISHING_PIECES, finishing_offers
from app.services.room_presentation import finishing_choices
from app.services.turn_coordinator import _bundle_action_decision
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics
from pydantic import TypeAdapter

from tests.unit.test_turn_coordinator import (
    CONTEXT,
    FakeCapabilities,
    FakeDesign,
    FakePipeline,
    FakeReferences,
    _coordinator,
    _product,
    _state,
    _turn,
)

TAXONOMY = load_taxonomy()
ROOMS = load_room_pieces(taxonomy=TAXONOMY, seating=load_seating_semantics(taxonomy=TAXONOMY))
LIVING_ROOM = ROOMS.template("living_room")

SOFA, TABLE, RUG = 501, 502, 503
KINDS = {
    SOFA: ("seating", "sofa"),
    TABLE: ("tables", "center-table"),
    RUG: ("decor", "carpet"),
    520: ("decor", "vase"),
    521: ("decor", "vase"),
    522: ("decor", "vase"),
}
STOCK = (
    ("seating", "sofa"),
    ("tables", "center-table"),
    ("decor", "carpet"),
    ("tables", "service-table"),
    ("lighting", "table-lamp"),
    ("decor", "vase"),
    ("decor", "mirror"),
)
MISSING = ("side-table", "table-lamp", "vase", "mirror")
"""The living room's pieces in STOCK that the room below does not hold, in the
registry's order."""


class RoomHydration:
    """Reads each product back as the kind of piece it is."""

    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[ProductCandidate, ...]:
        self.calls.append(list(product_ids))
        products = []
        for product_id in product_ids:
            category, subcategory = KINDS.get(product_id, ("seating", "sofa"))
            products.append(
                _product(product_id, subcategory=subcategory).model_copy(
                    update={
                        "commerce": CommerceClassification(
                            category=category, subcategory=subcategory
                        )
                    }
                )
            )
        return tuple(products)


def _line(line_id: int, product_id: int) -> BundleItemState:
    return BundleItemState(
        line_id=line_id,
        product_id=product_id,
        quantity=1,
        acquisition=BundleAcquisition.TO_BUY,
        status=BundleItemStatus.LOCKED,
    )


def _finished_room() -> RoomProjectState:
    return RoomProjectState(
        room_kind="living_room",
        budget=PriceConstraint.at_most(Decimal("12000"), "SAR"),
        questions_done=True,
        bundle_items=(_line(1, SOFA), _line(2, TABLE), _line(3, RUG)),
        next_bundle_line_id=4,
    )


def _finishing_touch() -> CustomerAgentDecision:
    """What the decision model made of "I'd like to add a finishing touch"."""
    return CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF,
        design_scope=DesignScope.COMPLEMENT,
        commercial_reason=CommercialReason.CUSTOMER_REQUEST,
    )


def _parts(decision: CustomerAgentDecision, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
    kwargs.setdefault("capabilities", FakeCapabilities(pairs=STOCK))
    kwargs.setdefault("hydration", RoomHydration())
    return _coordinator(decision, rooms=ROOMS, **kwargs)


async def _ask(room: RoomProjectState | None) -> CustomerTurnResult:
    coordinator, _ = _parts(_finishing_touch())
    # Nothing picked: there is no single product to build around.
    state = _state(room=room, selected=())
    result: CustomerTurnResult = await coordinator.run(
        _turn(state, "I'd like to add a finishing touch")
    )
    return result


def _tap(room: RoomProjectState | None, action: BundleActionRequest, message: str) -> Any:
    return CustomerTurnInput(
        message=message,
        state=_state(room=room, selected=()),
        context=CONTEXT,
        bundle_action=action,
    )


# ── which pieces ────────────────────────────────────────────────────────────


def test_the_offer_is_what_the_room_lacks_and_the_store_stocks_in_registry_order() -> None:
    assert LIVING_ROOM is not None
    capabilities = _capabilities(STOCK)

    offers = finishing_offers(LIVING_ROOM, capabilities, frozenset({"sofa", "center-table", "rug"}))

    assert tuple(offer.key for offer in offers) == MISSING
    assert not any(offer.selected for offer in offers)


def test_the_seating_is_never_a_finishing_touch() -> None:
    assert LIVING_ROOM is not None
    offers = finishing_offers(LIVING_ROOM, _capabilities(STOCK), frozenset())

    assert "sofa" not in {offer.key for offer in offers}


def test_the_offer_is_a_row_of_chips_not_the_registry() -> None:
    assert LIVING_ROOM is not None
    everything = tuple(
        dict.fromkeys(
            (piece.commerce_category, t) for piece in LIVING_ROOM.pieces for t in piece.types
        )
    )

    offers = finishing_offers(LIVING_ROOM, _capabilities(everything), frozenset())

    assert len(offers) == MAX_FINISHING_PIECES


def _capabilities(pairs: tuple[tuple[str, str], ...]) -> Any:
    from app.schemas.retailer import RetailerCatalogCapabilities, RetailerCatalogCapability

    return RetailerCatalogCapabilities(
        capabilities=tuple(
            RetailerCatalogCapability(
                commerce_category=category,
                commerce_subcategory=subcategory,
                active_product_count=3,
            )
            for category, subcategory in pairs
        )
    )


# ── the page Nora chooses ───────────────────────────────────────────────────


PAGE = (520, 521, 522)
"""A page of the finishing touch Nora chose."""

VASES = DesignCategoryNeed(
    commerce_category="decor", commerce_subcategory="vase", priority=DesignPriority.OPTIONAL
)


class RoomPipeline(FakePipeline):
    """The page search, and the one-product pools an addition is costed with."""

    def __init__(self, ids: tuple[int, ...] = PAGE) -> None:
        super().__init__(ids=ids)
        self.hydration = RoomHydration()

    async def execute_forced_pool(self, product_id: int, context: Any) -> CandidatePoolResult:
        products = await self.hydration.hydrate_ids((product_id,), context)
        return CandidatePoolResult(
            candidates=tuple(
                RankedProductCandidate(product=p, relaxation_depth=0) for p in products
            ),
            eligible_count=len(products),
            was_relaxed=False,
            stop_reason=StopReason.EXACT_SUFFICIENT,
            semantic_used=False,
        )


class CostingOptimizer:
    """Keeps every lock and takes each pool's one product, priced as it is."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def optimize(self, request: Any) -> RoomBundle:
        self.requests.append(request)
        lines = [
            BundleLine(
                product=lock.product,
                quantity=lock.quantity,
                locked=True,
                acquisition=lock.acquisition,
            )
            for lock in request.locked
        ]
        for entry in request.discovery.needs:
            if entry.pool is not None and entry.pool.candidates:
                lines.append(
                    BundleLine(
                        need_index=entry.need_index,
                        product=entry.pool.candidates[0].product,
                        quantity=entry.need.quantity,
                        locked=False,
                        acquisition=BundleAcquisition.TO_BUY,
                        relaxation_depth=0,
                    )
                )
        return RoomBundle(
            status=BundleStatus.COMPLETE,
            lines=tuple(lines),
            new_spend_total=sum((line.line_total for line in lines), Decimal(0)),
            currency="SAR",
        )


def _engine(
    *, design: Any = None, pipeline: RoomPipeline | None = None, chosen: int = PAGE[0]
) -> tuple[Any, dict[str, Any]]:
    pipeline = pipeline or RoomPipeline()
    return _parts(
        _finishing_touch(),
        design=design or FakeDesign(InteriorDesignResult(needs=(VASES,))),
        pipeline=pipeline,
        optimizer=CostingOptimizer(),
        design_discovery=DesignDiscoveryService(pipeline, TAXONOMY),  # type: ignore[arg-type]
        references=FakeReferences(
            {"PresentedOrdinal": ResolvedProductReference(product_id=chosen)}
        ),
    )


def _room(budget: str = "12000") -> RoomProjectState:
    """Three pieces at 1,000 each: 3,000 spent."""
    return _finished_room().model_copy(
        update={"budget": PriceConstraint.at_most(Decimal(budget), "SAR")}
    )


def _on_screen(room: RoomProjectState) -> AgentStateV1:
    return _state(room=room, selected=(), presented=PAGE)


def _input(state: AgentStateV1, action: BundleActionRequest, message: str) -> CustomerTurnInput:
    return CustomerTurnInput(message=message, state=state, context=CONTEXT, bundle_action=action)


async def test_the_finishing_touch_is_shown_not_asked_about() -> None:
    coordinator, parts = _engine()

    result = await coordinator.run(
        _tap(_finished_room(), FinishingTouchesAction(), "Add a finishing touch")
    )

    asked = parts["design"].requests[0]
    assert asked.task is DesignTask.COMPLEMENTARY_RECOMMENDATION
    assert len(asked.anchors) == 3, "Nora is told everything in the room"
    (search,) = parts["pipeline"].calls
    assert search.request.commerce_subcategory == "vase"
    assert result.room_addition == "vase"
    assert result.state.product_interaction.presented_product_ids == PAGE
    assert tuple(piece.key for piece in result.finishing_pieces) == tuple(
        key for key in MISSING if key != "vase"
    ), "the other pieces the room could take are chips"
    assert parts["decisions"].inputs == [], "no model read the words"


async def test_typed_add_a_finishing_touch_is_the_tap() -> None:
    coordinator, parts = _engine()

    result = await coordinator.run(
        _turn(_state(room=_finished_room(), selected=()), "I'd like to add a finishing touch")
    )

    assert result.room_addition == "vase"
    assert [c.request.commerce_subcategory for c in parts["pipeline"].calls] == ["vase"]


async def test_a_pieces_chip_shows_that_piece_without_asking_nora() -> None:
    coordinator, parts = _engine()

    result = await coordinator.run(
        _tap(_finished_room(), AddPieceAction(piece="mirror"), "Show me mirrors")
    )

    assert parts["design"].requests == []
    assert [c.request.commerce_subcategory for c in parts["pipeline"].calls] == ["mirror"]
    assert result.room_addition == "mirror"
    assert "mirror" not in {piece.key for piece in result.finishing_pieces}


async def test_the_chips_beside_the_page_offer_no_you_choose() -> None:
    coordinator, _ = _engine()
    result = await coordinator.run(
        _tap(_finished_room(), FinishingTouchesAction(), "Add a finishing touch")
    )

    presentation = ChatRuntime.presentation(result)

    assert presentation is not None
    assert presentation.room_addition == "vase"
    assert [c.bundle_action for c in presentation.choices] == [
        AddPieceAction(piece=key) for key in MISSING if key != "vase"
    ]


async def test_with_nothing_in_stock_the_pieces_stay_chips() -> None:
    coordinator, _ = _engine(pipeline=RoomPipeline(ids=()))

    result = await coordinator.run(
        _tap(_finished_room(), FinishingTouchesAction(), "Add a finishing touch")
    )

    assert result.room_addition is None
    assert tuple(piece.key for piece in result.finishing_pieces) == (
        "vase",
        *(key for key in MISSING if key != "vase"),
    ), "in Nora's order"


MIRRORS = DesignCategoryNeed(
    commerce_category="decor", commerce_subcategory="mirror", priority=DesignPriority.OPTIONAL
)


async def test_nora_ranks_the_pieces_and_the_chips_follow_her_order() -> None:
    coordinator, parts = _engine(design=FakeDesign(InteriorDesignResult(needs=(MIRRORS, VASES))))

    result = await coordinator.run(
        _tap(_finished_room(), FinishingTouchesAction(), "Add a finishing touch")
    )

    (asked,) = parts["design"].requests
    told = {
        (c.commerce_category, c.commerce_subcategory)
        for c in asked.catalog_capabilities.capabilities
    }
    assert {sub for _, sub in told} == {"service-table", "table-lamp", "vase", "mirror"}, (
        "she ranks only what the room could still take"
    )
    assert result.room_addition == "mirror"
    assert [piece.key for piece in result.finishing_pieces] == ["vase", "side-table", "table-lamp"]
    room = result.state.room_project
    assert room is not None
    assert room.finishing_order == ("mirror", "vase", "side-table", "table-lamp")


async def test_a_tapped_chip_keeps_noras_order_without_asking_her_again() -> None:
    room = _finished_room().model_copy(
        update={"finishing_order": ("mirror", "vase", "side-table", "table-lamp")}
    )
    coordinator, parts = _engine()

    result = await coordinator.run(_tap(room, AddPieceAction(piece="vase"), "Show me vases"))

    assert parts["design"].requests == []
    assert [piece.key for piece in result.finishing_pieces] == [
        "mirror",
        "side-table",
        "table-lamp",
    ]


async def test_without_a_room_it_still_asks_which_product_they_meant() -> None:
    result = await _ask(None)

    assert result.finishing_pieces == ()
    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE


def test_arabic_chips_name_the_pieces_in_arabic() -> None:
    assert LIVING_ROOM is not None
    offers = finishing_offers(LIVING_ROOM, _capabilities(STOCK), frozenset({"sofa"}))

    labels = [c.label for c in finishing_choices(offers, ReplyLanguage.AR)]

    assert labels[-1] == "اختر أنت"
    assert all(label == offer.label_ar for label, offer in zip(labels, offers, strict=False))
    beside_a_page = finishing_choices(offers, ReplyLanguage.AR, you_choose=False)
    assert [c.label for c in beside_a_page] == labels[:-1]


# ── add to room ─────────────────────────────────────────────────────────────


async def test_add_to_room_within_budget_joins_the_room_package() -> None:
    coordinator, _ = _engine()

    result = await coordinator.run(
        _input(_on_screen(_room()), AddToRoomAction(ordinal=1), "Add this vase to my room")
    )

    room = result.state.room_project
    assert room is not None
    assert PAGE[0] in {line.product_id for line in room.bundle_items}
    assert result.pieces_added == ("vase",)
    assert result.swap_offer is None
    assert result.state.product_interaction.selected_product_ids == (), "never the cart"


async def test_a_card_the_room_already_holds_a_piece_of_is_refused() -> None:
    coordinator, _ = _engine(chosen=RUG)

    result = await coordinator.run(
        _input(_on_screen(_room()), AddToRoomAction(ordinal=1), "Add this to my room")
    )

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.QUESTIONS_EXPIRED


async def _over_budget() -> tuple[Any, CustomerTurnResult]:
    coordinator, _ = _engine()
    result = await coordinator.run(
        _input(_on_screen(_room("3500")), AddToRoomAction(ordinal=1), "Add this vase to my room")
    )
    return coordinator, result


async def test_over_budget_asks_whether_to_increase_it_or_keep_the_plan() -> None:
    _, result = await _over_budget()

    room = result.state.room_project
    assert room is not None
    assert PAGE[0] not in {line.product_id for line in room.bundle_items}, "nothing changes yet"
    assert result.swap_offer is not None
    assert result.swap_offer.stage is SwapBudgetOfferStage.STRETCH
    assert result.swap_offer.added_piece == "vase"
    assert (result.swap_offer.new_spend_total, result.swap_offer.overage) == (
        Decimal(4000),
        Decimal(500),
    )
    held = result.state.swap_budget_offer
    assert held is not None and held.added_piece == "vase" and held.chosen_product_id == PAGE[0]


async def test_yes_adds_it_and_raises_the_budget() -> None:
    coordinator, held = await _over_budget()

    result = await coordinator.run(_input(held.state, SwapConfirmAction(), "Yes, increase it"))

    room = result.state.room_project
    assert room is not None
    assert PAGE[0] in {line.product_id for line in room.bundle_items}
    assert room.budget is not None and room.budget.max_amount == Decimal(4000)
    assert result.state.swap_budget_offer is None
    assert result.pieces_added == ("vase",)
    assert result.budget_stretched is not None


async def test_no_offers_keeping_the_plan_or_cheaper_options() -> None:
    coordinator, held = await _over_budget()

    result = await coordinator.run(_input(held.state, SwapDeclineAction(), "No"))

    assert result.swap_offer is not None
    assert result.swap_offer.stage is SwapBudgetOfferStage.ALTERNATIVES
    assert result.swap_offer.added_piece == "vase"


async def _declined() -> tuple[Any, CustomerTurnResult]:
    coordinator, held = await _over_budget()
    return coordinator, await coordinator.run(_input(held.state, SwapDeclineAction(), "No"))


async def test_keeping_the_plan_leaves_the_room_as_it_was() -> None:
    coordinator, held = await _declined()

    result = await coordinator.run(
        _input(held.state, SwapKeepOriginalAction(), "Keep my original room")
    )

    room = result.state.room_project
    assert room is not None
    assert {line.product_id for line in room.bundle_items} == {SOFA, TABLE, RUG}
    assert result.state.swap_budget_offer is None
    assert room.budget is not None and room.budget.max_amount == Decimal(3500)


async def test_cheaper_options_are_the_same_piece_within_what_is_left() -> None:
    coordinator, held = await _declined()

    result = await coordinator.run(
        _input(held.state, SwapAlternativesAction(), "Show cheaper options")
    )

    search = coordinator._pipeline.calls[-1].request
    assert search.commerce_subcategory == "vase"
    assert search.price is not None and search.price.max_amount == Decimal(500)
    assert result.room_addition == "vase"
    assert result.state.swap_budget_offer is None


def test_the_addition_travels_as_its_kind() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(BundleActionRequest)

    assert adapter.validate_python({"kind": "add_to_room", "ordinal": 2}) == AddToRoomAction(
        ordinal=2
    )
    assert _bundle_action_decision(AddToRoomAction(ordinal=1)).action is AgentAction.DESIGN_HANDOFF


def test_the_held_offer_is_a_swap_or_an_addition_never_both() -> None:
    from pydantic import ValidationError

    SwapBudgetOfferState(added_piece="vase", chosen_product_id=1, bundle_revision=0)
    SwapBudgetOfferState(bundle_ordinal=1, chosen_product_id=1, bundle_revision=0)
    for wrong in ({}, {"bundle_ordinal": 1, "added_piece": "vase"}):
        try:
            SwapBudgetOfferState(chosen_product_id=1, bundle_revision=0, **wrong)
        except ValidationError:
            continue
        raise AssertionError(f"accepted {wrong}")


# ── the room's own chip ─────────────────────────────────────────────────────


async def test_the_rooms_chip_with_no_room_has_moved_on() -> None:
    coordinator, _ = _parts(_finishing_touch())

    result = await coordinator.run(_tap(None, FinishingTouchesAction(), "What would finish it?"))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.QUESTIONS_EXPIRED


def test_after_a_room_the_finishing_touch_chip_carries_its_action() -> None:
    from app.schemas.bundle import BundleStatus, RoomBundle
    from app.schemas.bundle_action import FinishingTouchesAction
    from app.schemas.next_step import NextStepKind
    from app.services.next_step import next_step

    result = CustomerTurnResult(
        state=_state(room=None),
        decision=_finishing_touch(),
        grounding=_grounding(),
        bundle_outcome=RoomBundle(
            status=BundleStatus.COMPLETE, new_spend_total=Decimal(0), currency="SAR"
        ),
    )

    step = next_step(result, None)

    assert step is not None and step.kind is NextStepKind.AFTER_ROOM
    assert step.chips[1].bundle_action == FinishingTouchesAction()


def test_after_a_room_the_reply_cannot_swap_the_chips_for_words() -> None:
    from app.schemas.bundle import BundleStatus, RoomBundle
    from app.schemas.bundle_action import FinishingTouchesAction
    from app.schemas.text_choice import TextReplyChoice
    from app.services.next_step import next_step

    result = CustomerTurnResult(
        state=_state(room=None),
        decision=_finishing_touch(),
        grounding=_grounding(),
        bundle_outcome=RoomBundle(
            status=BundleStatus.COMPLETE, new_spend_total=Decimal(0), currency="SAR"
        ),
    )
    result = result.model_copy(update={"next_step": next_step(result, None)})
    reply = CustomerResponse(
        message="Would you like to keep it, or add a finishing touch?",
        choices=(
            TextReplyChoice(label="Keep it", value="Keep it"),
            TextReplyChoice(label="Add one", value="I'd like to add a finishing touch"),
        ),
    )

    presentation = ChatRuntime.presentation(result, reply)

    assert presentation is not None
    assert FinishingTouchesAction() in [c.bundle_action for c in presentation.choices]


def test_the_decision_is_told_a_finishing_touch_is_a_complement() -> None:
    from app.prompts.customer_commerce.v1 import INSTRUCTIONS

    assert "A finishing touch for a room already put together is complement" in INSTRUCTIONS


def _grounding() -> Any:
    from app.schemas.agent_turn import TurnGrounding

    return TurnGrounding()
