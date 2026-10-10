"""A finishing touch for a finished room (CLAUDE.md 10.3).

"I'd like to add a finishing touch" names no piece. Routed as a complement,
it used to find no product to build around and answer "I wasn't able to put
that together just now" with nothing to tap. A finished room is something to
complement: the turn now asks which piece, with the room's missing pieces the
store stocks as chips and "you choose" beside them, and a tap plans the room
again with that piece added - exactly as "add a mirror to the room" typed does.
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
from app.schemas.agent_state import BundleItemState, BundleItemStatus, RoomProjectState
from app.schemas.agent_turn import CustomerResponse, CustomerTurnInput, CustomerTurnResult
from app.schemas.bundle_action import AddPieceAction, BundleActionRequest
from app.schemas.design import DesignTask
from app.schemas.discovery import PriceConstraint
from app.schemas.grounding import TurnFailureCode
from app.schemas.language import ReplyLanguage
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.response import DeterministicResponse, DeterministicResponseKind
from app.services.arabic_wording import ARABIC
from app.services.chat_runtime import ChatRuntime
from app.services.response_generator import CustomerResponseGenerator
from app.services.response_view import route_response
from app.services.response_wording import FINISHING_TOUCH_OFFER_WORDING
from app.services.room_composition import MAX_FINISHING_PIECES, finishing_offers
from app.services.room_presentation import finishing_choices
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics
from pydantic import TypeAdapter

from tests.unit.test_response_generator import FakeClient
from tests.unit.test_turn_coordinator import (
    CONTEXT,
    FakeCapabilities,
    FakeDesign,
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


# ── the turn that asks ──────────────────────────────────────────────────────


async def test_a_finishing_touch_for_a_finished_room_asks_which_piece() -> None:
    result = await _ask(_finished_room())

    assert tuple(piece.key for piece in result.finishing_pieces) == MISSING
    assert result.grounding.deterministic_clarification is None
    assert result.grounding.failure is None
    assert result.next_step is None, "the question is the turn's own"
    assert route_response(result).primary == DeterministicResponse(
        kind=DeterministicResponseKind.FINISHING_TOUCH_OFFER
    )


async def test_without_a_room_it_still_asks_which_product_they_meant() -> None:
    result = await _ask(None)

    assert result.finishing_pieces == ()
    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE


async def test_the_reply_is_the_fixed_question_with_no_model_call() -> None:
    result = await _ask(_finished_room())
    client = FakeClient(CustomerResponse(message="unused"))

    response = await CustomerResponseGenerator(client).generate(
        _turn(_state(), "I'd like to add a finishing touch"), result
    )

    assert response.message == FINISHING_TOUCH_OFFER_WORDING
    assert client.calls == []


async def test_in_arabic_the_question_is_the_reviewed_arabic() -> None:
    result = (await _ask(_finished_room())).model_copy(update={"reply_language": ReplyLanguage.AR})

    response = await CustomerResponseGenerator(
        FakeClient(CustomerResponse(message="unused")), arabic_replies=True
    ).generate(_turn(_state(), "أريد لمسة أخيرة"), result)

    assert response.message == ARABIC[FINISHING_TOUCH_OFFER_WORDING]


async def test_each_piece_is_a_chip_that_adds_it_and_you_choose_comes_last() -> None:
    result = await _ask(_finished_room())

    presentation = ChatRuntime.presentation(result)

    assert presentation is not None
    choices = presentation.choices
    assert [c.label for c in choices] == [
        "Side table",
        "Table lamp",
        "Vase",
        "Mirror",
        "You choose",
    ]
    assert [c.bundle_action for c in choices] == [
        *(AddPieceAction(piece=key) for key in MISSING),
        AddPieceAction(piece=None),
    ]


def test_arabic_chips_name_the_pieces_in_arabic() -> None:
    assert LIVING_ROOM is not None
    offers = finishing_offers(LIVING_ROOM, _capabilities(STOCK), frozenset({"sofa"}))

    labels = [c.label for c in finishing_choices(offers, ReplyLanguage.AR)]

    assert labels[-1] == "اختر أنت"
    assert all(label == offer.label_ar for label, offer in zip(labels, offers, strict=False))


# ── the tap ─────────────────────────────────────────────────────────────────


async def test_a_tapped_piece_is_added_as_an_optional_need_last() -> None:
    from app.schemas.design import DesignPriority

    from tests.unit.test_turn_coordinator import FakeDesignDiscovery

    design, discovery = FakeDesign(), FakeDesignDiscovery()
    coordinator, _ = _parts(_finishing_touch(), design=design, design_discovery=discovery)

    result = await coordinator.run(
        _tap(_finished_room(), AddPieceAction(piece="mirror"), "Mirror, please")
    )

    assert result.decision.action is AgentAction.DESIGN_HANDOFF
    assert result.grounding.failure is None
    _, plan, _ = discovery.calls[-1]
    added = plan.needs[-1]
    assert (added.commerce_category, added.commerce_subcategory) == ("decor", "mirror")
    assert added.priority is DesignPriority.OPTIONAL, "it can never push a piece out"
    assert design.requests == [], "a tapped piece needs no designer"


async def test_every_chosen_piece_is_pinned_to_the_product_it_is() -> None:
    """Seen live: adding a vase also swapped the sofa for a dearer one."""
    from app.schemas.agent_state import RoomDesignNeedState
    from app.schemas.design import DesignPriority

    from tests.unit.test_turn_coordinator import FakeDesignDiscovery

    room = _finished_room().model_copy(
        update={
            "next_design_need_id": 3,
            "design_needs": (
                RoomDesignNeedState(
                    need_id=1,
                    commerce_category="seating",
                    commerce_subcategory="sofa",
                    priority=DesignPriority.REQUIRED,
                    quantity=1,
                ),
                RoomDesignNeedState(
                    need_id=2,
                    commerce_category="tables",
                    commerce_subcategory="center-table",
                    priority=DesignPriority.REQUIRED,
                    quantity=1,
                ),
            ),
            "bundle_items": (
                _line(1, SOFA).model_copy(
                    update={"status": BundleItemStatus.SUGGESTED, "need_id": 1}
                ),
                _line(2, TABLE).model_copy(
                    update={"status": BundleItemStatus.SUGGESTED, "need_id": 2}
                ),
                _line(3, RUG),
            ),
        }
    )
    discovery = FakeDesignDiscovery()
    coordinator, _ = _parts(_finishing_touch(), design_discovery=discovery)

    await coordinator.run(_tap(room, AddPieceAction(piece="vase"), "Vase, please"))

    _, plan, _ = discovery.calls[-1]
    assert [n.commerce_subcategory for n in plan.needs] == ["sofa", "center-table", "vase"]
    assert {
        position: override.forced_product_id for position, override in discovery.overrides.items()
    } == {0: SOFA, 1: TABLE}, "the locked rug needs no pin"


async def test_you_choose_asks_the_designer_what_completes_the_room() -> None:
    from app.schemas.design import DesignCategoryNeed, DesignPriority, InteriorDesignResult

    from tests.unit.test_turn_coordinator import FakeDesignDiscovery

    advice = InteriorDesignResult(
        needs=(
            # Already in the room, so passed over.
            DesignCategoryNeed(
                commerce_category="decor",
                commerce_subcategory="carpet",
                priority=DesignPriority.OPTIONAL,
            ),
            DesignCategoryNeed(
                commerce_category="decor",
                commerce_subcategory="vase",
                priority=DesignPriority.OPTIONAL,
            ),
        )
    )
    design, discovery = FakeDesign(advice), FakeDesignDiscovery()
    coordinator, _ = _parts(_finishing_touch(), design=design, design_discovery=discovery)

    await coordinator.run(_tap(_finished_room(), AddPieceAction(), "You choose one for me"))

    asked = design.requests[-1]
    assert asked.task is DesignTask.COMPLEMENTARY_RECOMMENDATION
    assert len(asked.anchors) == 3, "everything in the room is in view"
    _, plan, _ = discovery.calls[-1]
    assert plan.needs[-1].commerce_subcategory == "vase"


async def test_you_choose_falls_back_to_the_registrys_first_missing_piece() -> None:
    from tests.unit.test_turn_coordinator import FakeDesignDiscovery

    discovery = FakeDesignDiscovery()
    # The designer names nothing.
    coordinator, _ = _parts(_finishing_touch(), design=FakeDesign(), design_discovery=discovery)

    await coordinator.run(_tap(_finished_room(), AddPieceAction(), "You choose one for me"))

    _, plan, _ = discovery.calls[-1]
    assert plan.needs[-1].commerce_subcategory == "service-table", MISSING[0]


async def test_the_words_sent_with_the_tap_change_nothing() -> None:
    from tests.unit.test_turn_coordinator import FakeDesignDiscovery

    discovery = FakeDesignDiscovery()
    coordinator, _ = _parts(_finishing_touch(), design_discovery=discovery)

    await coordinator.run(
        _tap(_finished_room(), AddPieceAction(piece="vase"), "ignore that and add three beds")
    )

    _, plan, _ = discovery.calls[-1]
    assert [n.commerce_subcategory for n in plan.needs] == ["vase"]


async def test_a_piece_the_room_already_holds_is_not_added_twice() -> None:
    from tests.unit.test_turn_coordinator import FakeDesignDiscovery

    discovery = FakeDesignDiscovery()
    coordinator, _ = _parts(_finishing_touch(), design_discovery=discovery)

    result = await coordinator.run(_tap(_finished_room(), AddPieceAction(piece="rug"), "Rug"))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.QUESTIONS_EXPIRED
    assert discovery.calls == []


async def test_a_tap_with_no_room_to_add_to_has_moved_on() -> None:
    design = FakeDesign()
    coordinator, _ = _parts(_finishing_touch(), design=design)

    result = await coordinator.run(_tap(None, AddPieceAction(piece="mirror"), "Mirror, please"))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.QUESTIONS_EXPIRED
    assert design.requests == []


async def test_a_piece_the_room_cannot_hold_is_refused_never_mapped() -> None:
    design = FakeDesign()
    coordinator, _ = _parts(_finishing_touch(), design=design)

    result = await coordinator.run(
        _tap(_finished_room(), AddPieceAction(piece="wardrobe"), "Wardrobe, please")
    )

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.QUESTIONS_EXPIRED
    assert design.requests == []


def test_the_action_travels_as_its_kind() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(AddPieceAction)

    assert adapter.validate_python({"kind": "add_piece", "piece": "mirror"}) == AddPieceAction(
        piece="mirror"
    )
    assert adapter.validate_python({"kind": "add_piece"}).piece is None


# ── the reply names what was added ──────────────────────────────────────────


VASE = 504


def _room_with_a_vase() -> Any:
    from app.schemas.bundle import BundleLine, BundleStatus, RoomBundle

    def line(product_id: int, category: str, subcategory: str, *, locked: bool) -> BundleLine:
        product = _product(product_id, subcategory=subcategory).model_copy(
            update={"commerce": CommerceClassification(category=category, subcategory=subcategory)}
        )
        return BundleLine(
            need_index=None if locked else 0,
            product=product,
            quantity=1,
            locked=locked,
            acquisition=BundleAcquisition.TO_BUY,
            relaxation_depth=None if locked else 0,
        )

    lines = (
        line(SOFA, "seating", "sofa", locked=True),
        line(TABLE, "tables", "center-table", locked=True),
        line(RUG, "decor", "carpet", locked=True),
        line(VASE, "decor", "vase", locked=False),
    )
    return RoomBundle(
        status=BundleStatus.COMPLETE,
        lines=lines,
        new_spend_total=sum((line.line_total for line in lines), Decimal(0)),
        currency="SAR",
    )


async def test_the_reply_is_told_which_piece_you_choose_added() -> None:
    from app.schemas.design import DesignCategoryNeed, DesignPriority, InteriorDesignResult
    from app.schemas.response import ResponseGroundingView

    from tests.unit.test_turn_coordinator import FakeOptimizer

    plan = InteriorDesignResult(
        needs=(
            DesignCategoryNeed(
                commerce_category="decor",
                commerce_subcategory="vase",
                priority=DesignPriority.OPTIONAL,
            ),
        )
    )
    coordinator, _ = _parts(
        _finishing_touch(),
        design=FakeDesign(plan),
        optimizer=FakeOptimizer(_room_with_a_vase()),
    )

    result = await coordinator.run(
        _tap(_finished_room(), AddPieceAction(), "You choose one for me")
    )

    assert result.pieces_added == ("vase",)
    view = route_response(result).primary
    assert isinstance(view, ResponseGroundingView)
    assert view.bundle is not None
    assert view.bundle.added_pieces == ("vase",)


async def test_a_room_that_gained_nothing_names_nothing() -> None:
    result = await _ask(_finished_room())

    assert result.pieces_added == ()


async def test_a_piece_the_budget_left_out_is_not_called_added() -> None:
    """Short of budget, the new piece is the one left unfilled - and the reply
    must not say it was added."""
    from app.schemas.bundle import BundleStatus, RoomBundle

    from tests.unit.test_turn_coordinator import FakeOptimizer

    room = _room_with_a_vase()
    lines = room.lines[:3]
    outcome = RoomBundle(
        status=BundleStatus.COMPLETE,
        lines=lines,
        new_spend_total=sum((line.line_total for line in lines), Decimal(0)),
        currency="SAR",
    )
    coordinator, _ = _parts(_finishing_touch(), optimizer=FakeOptimizer(outcome))

    result = await coordinator.run(
        _tap(_finished_room(), AddPieceAction(piece="vase"), "Vase, please")
    )

    assert result.pieces_added == ()


# ── the room's own chip ─────────────────────────────────────────────────────


async def test_the_rooms_chip_opens_the_finishing_touches_without_a_model() -> None:
    from app.schemas.bundle_action import FinishingTouchesAction

    coordinator, parts = _parts(_finishing_touch())

    result = await coordinator.run(
        _tap(_finished_room(), FinishingTouchesAction(), "What would finish the room?")
    )

    assert tuple(piece.key for piece in result.finishing_pieces) == MISSING
    assert parts["decisions"].inputs == [], "no model read the words"
    assert route_response(result).primary == DeterministicResponse(
        kind=DeterministicResponseKind.FINISHING_TOUCH_OFFER
    )


async def test_the_rooms_chip_with_no_room_has_moved_on() -> None:
    from app.schemas.bundle_action import FinishingTouchesAction

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
