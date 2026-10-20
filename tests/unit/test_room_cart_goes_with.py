"""A whole room put in the cart is answered as a pick is (CLAUDE.md 10.4, 36).

"Add all to cart" - tapped on the room package, or typed - puts every piece
in the cart, and then what would finish the room is shown: the design
specialist chooses one kind the room does not hold, told every piece in it; a
page of that kind leans to the room's taste; the other kinds are chips.
Nothing in the room is suggested again, and "design a room around my picks"
is never offered for picks that already are the room.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import (
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    FinishedRoom,
    RoomProjectState,
)
from app.schemas.agent_turn import CustomerTurnInput, CustomerTurnResult, TurnGrounding
from app.schemas.bundle_action import (
    AddPieceAction,
    AddRoomToCartAction,
    BundleAlternativesAction,
    BundleRemoveAction,
    FinishingTouchesAction,
    RoomDoneAction,
)
from app.schemas.conversation import ConversationContext, ConversationMessage, ConversationRole
from app.schemas.design import DesignCategoryNeed, DesignPriority, InteriorDesignResult
from app.schemas.grounding import TurnFailureCode
from app.schemas.language import ReplyLanguage
from app.schemas.next_step import NextStepKind
from app.schemas.picks import PickView
from app.schemas.product_action import CompanionAction
from app.services.agent_persona import speaker
from app.services.next_step import next_step
from app.services.room_cart import current_room_products, picked_for_room, room_names
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics

from tests.unit.test_cross_sell_shows_products import RUGS, WARDROBES, _engine
from tests.unit.test_product_actions import BED, OTHER_BED, WARDROBE
from tests.unit.test_turn_coordinator import CONTEXT, FakeDesign

_TAXONOMY = load_taxonomy()
ROOMS = load_room_pieces(taxonomy=_TAXONOMY, seating=load_seating_semantics(taxonomy=_TAXONOMY))


def _bedroom() -> AgentStateV1:
    def line(line_id: int, product_id: int) -> BundleItemState:
        return BundleItemState(
            line_id=line_id,
            product_id=product_id,
            quantity=1,
            acquisition=BundleAcquisition.TO_BUY,
            status=BundleItemStatus.SUGGESTED,
        )

    return AgentStateV1(
        room_project=RoomProjectState(
            room_type="bedroom",
            bundle_items=(line(1, BED), line(2, WARDROBE)),
            next_bundle_line_id=3,
        )
    )


def _add_all(state: AgentStateV1) -> CustomerTurnInput:
    return CustomerTurnInput(
        message="Add all of these to my cart",
        state=state,
        context=CONTEXT,
        bundle_action=AddRoomToCartAction(),
    )


def _chip_labels(result: Any) -> list[str]:
    return [chip.label for chip in (result.next_step.chips if result.next_step else ())]


async def test_the_whole_room_in_the_cart_shows_what_would_finish_it() -> None:
    coordinator, design, pipeline = _engine()

    result = await coordinator.run(_add_all(_bedroom()))

    assert result.state.product_interaction.selected_product_ids == (BED, WARDROBE)
    (request,) = design.requests
    assert len(request.anchors) == 2, "the specialist is told every piece of the room"
    assert [r.commerce_subcategory for r in pipeline.requests] == ["carpet"]
    assert result.room_in_cart == "bedroom"
    assert result.state.active_search is not None and result.state.active_search.ordered_by_pick
    assert result.state.product_interaction.focused_product_id == BED
    assert all(c.subcategory not in ("carpet", "bed", "wardrobe") for c in result.companions)


async def test_a_kind_the_room_holds_is_never_suggested_again() -> None:
    design = FakeDesign(InteriorDesignResult(needs=(WARDROBES, RUGS)))
    coordinator, _, pipeline = _engine(design=design)

    await coordinator.run(_add_all(_bedroom()))

    assert [r.commerce_subcategory for r in pipeline.requests] == ["carpet"]


async def test_a_room_around_their_picks_is_never_offered_when_the_picks_are_the_room() -> None:
    coordinator, _, _ = _engine()

    result = await coordinator.run(_add_all(_bedroom()))

    # The other kinds are its chips, as beside a pick: no step is added, and
    # never a room around picks that are the room.
    assert result.companions
    assert "Design a room around my picks" not in _chip_labels(result)


async def test_typed_add_everything_is_the_tap() -> None:
    decision = CustomerAgentDecision(action=AgentAction.ANSWER, add_room_to_cart=True)
    coordinator, design, pipeline = _engine(decision=decision)

    result = await coordinator.run(
        CustomerTurnInput(message="add everything to my cart", state=_bedroom(), context=CONTEXT)
    )

    assert result.state.product_interaction.selected_product_ids == (BED, WARDROBE)
    assert design.requests and [r.commerce_subcategory for r in pipeline.requests] == ["carpet"]
    assert result.room_in_cart == "bedroom"


async def test_with_nothing_to_show_the_room_offers_its_own_next_step() -> None:
    design = FakeDesign(InteriorDesignResult(needs=(WARDROBES,)))
    coordinator, _, pipeline = _engine(design=design, complements=None)

    result = await coordinator.run(_add_all(_bedroom()))

    assert pipeline.requests == []
    assert result.room_in_cart is None
    assert result.next_step is not None and result.next_step.kind is NextStepKind.ROOM_IN_CART
    assert "Design a room around my picks" not in _chip_labels(result)


async def test_switched_off_the_room_goes_in_as_before() -> None:
    coordinator, design, _ = _engine(shows_products=False)

    result = await coordinator.run(_add_all(_bedroom()))

    assert result.state.product_interaction.selected_product_ids == (BED, WARDROBE)
    assert design.requests == [] and result.room_in_cart is None


# ── phase 2: the cart is the room ───────────────────────────────────────────

LAMP = 40
"""A pick made after the room went into the cart."""


def _named_bedroom() -> AgentStateV1:
    state = _bedroom()
    assert state.room_project is not None
    return state.model_copy(
        update={"room_project": state.room_project.model_copy(update={"room_kind": "bedroom"})}
    )


async def _in_cart(**engine: Any) -> tuple[Any, AgentStateV1]:
    coordinator, _, _ = _engine(rooms=ROOMS, **engine)
    result = await coordinator.run(_add_all(_named_bedroom()))
    return coordinator, result.state


def _tap(state: AgentStateV1, action: Any, message: str) -> CustomerTurnInput:
    return CustomerTurnInput(message=message, state=state, context=CONTEXT, bundle_action=action)


def _picked(state: AgentStateV1, *product_ids: int) -> AgentStateV1:
    interaction = state.product_interaction
    return state.model_copy(
        update={
            "product_interaction": interaction.model_copy(
                update={"selected_product_ids": (*interaction.selected_product_ids, *product_ids)}
            )
        }
    )


async def test_add_all_to_cart_makes_the_cart_the_room() -> None:
    before = _picked(_named_bedroom(), OTHER_BED)
    coordinator, _, _ = _engine(rooms=ROOMS)

    result = await coordinator.run(_add_all(before))

    room = result.state.room_project
    assert room is not None and room.in_cart
    assert room.picks_before_cart == (OTHER_BED,), "a pick from before is not the room's"


async def test_the_package_is_a_record_once_the_room_is_in_the_cart() -> None:
    coordinator, state = await _in_cart()

    for action in (
        BundleAlternativesAction(bundle_ordinal=1),
        BundleRemoveAction(bundle_ordinal=1),
        FinishingTouchesAction(),
        AddPieceAction(piece="rug"),
    ):
        result = await coordinator.run(_tap(state, action, "change the room"))

        assert result.grounding.failure is not None
        assert result.grounding.failure.code is TurnFailureCode.QUESTIONS_EXPIRED
        assert result.state.room_project == state.room_project


async def test_a_later_pick_is_for_the_room_and_never_a_room_around_it() -> None:
    _, state = await _in_cart()
    after = _picked(state, LAMP)

    assert picked_for_room(state, after) == "bedroom"
    assert picked_for_room(after, after) is None, "nothing picked this turn"
    assert LAMP in current_room_products(after)

    result = CustomerTurnResult(
        state=after,
        decision=CustomerAgentDecision(action=AgentAction.ANSWER),
        grounding=TurnGrounding(),
        picks=(_pick(1, BED), _pick(2, WARDROBE), _pick(3, LAMP)),
    )
    step = next_step(result, None)

    assert step is not None and step.kind is NextStepKind.ROOM_IN_CART
    labels = [c.label for c in step.chips]
    assert "Design a room around my picks" not in labels
    assert step.chips[-2].bundle_action == RoomDoneAction()


def test_cart_lines_are_named_by_their_room() -> None:
    state = _picked(_named_bedroom(), OTHER_BED)
    room = state.room_project
    assert room is not None
    state = _picked(state, BED, WARDROBE).model_copy(
        update={
            "room_project": room.model_copy(
                update={"in_cart": True, "picks_before_cart": (OTHER_BED,)}
            )
        }
    )
    state = _picked(state, LAMP)

    names = room_names(state, ReplyLanguage.EN)

    assert names == {BED: "Bedroom", WARDROBE: "Bedroom", LAMP: "Bedroom"}
    assert room_names(state, ReplyLanguage.AR)[LAMP] == "غرفة النوم"


async def test_done_with_the_room_asks_which_room_next() -> None:
    coordinator, state = await _in_cart()
    state = _picked(state, LAMP)

    result = await coordinator.run(_tap(state, RoomDoneAction(), "I'm done with this room"))

    assert result.state.room_project is None
    (done,) = result.state.finished_rooms
    assert done.room_kind == "bedroom"
    assert set(done.product_ids) == {BED, WARDROBE, LAMP}
    assert result.room_finished == "bedroom"
    assert result.next_step is not None and result.next_step.kind is NextStepKind.NEXT_ROOM
    assert [c.label for c in result.next_step.chips] == ["Living room"], "never the bedroom"
    assert result.state.product_interaction.selected_product_ids == (BED, WARDROBE, LAMP)


async def test_typed_done_is_the_tap() -> None:
    _, state = await _in_cart()
    decision = CustomerAgentDecision(action=AgentAction.ANSWER, room_done=True)
    coordinator, _, _ = _engine(rooms=ROOMS, decision=decision)

    result = await coordinator.run(
        CustomerTurnInput(message="that's the bedroom sorted", state=state, context=CONTEXT)
    )

    assert result.room_finished == "bedroom"
    assert result.state.room_project is None


async def test_a_room_still_being_designed_is_never_closed() -> None:
    coordinator, _, _ = _engine(rooms=ROOMS)

    result = await coordinator.run(_tap(_named_bedroom(), RoomDoneAction(), "I'm done"))

    assert result.state.room_project == _named_bedroom().room_project
    assert result.room_finished is None


def test_the_room_in_the_cart_is_salmans() -> None:
    turn = _tap(_named_bedroom(), RoomDoneAction(), "I'm done with this room")
    turn = turn.model_copy(
        update={
            "conversation": ConversationContext(
                messages=(ConversationMessage(role=ConversationRole.USER, content="hi"),)
            )
        }
    )

    for fields in ({"room_finished": "bedroom"}, {"picks_for_room": "bedroom"}):
        result = CustomerTurnResult(
            state=_named_bedroom(),
            decision=CustomerAgentDecision(action=AgentAction.ANSWER),
            grounding=TurnGrounding(),
            **fields,
        )
        assert speaker(turn, result) is AgentPersona.SALMAN


def _pick(position: int, product_id: int) -> PickView:
    return PickView(
        pick=position,
        name_english=f"Piece {product_id}",
        image_url="https://example.test/p.jpg",
        price_amount=Decimal("1500"),
        price_unit="SAR",
    )


async def test_every_reply_after_says_the_room_is_in_the_cart() -> None:
    from app.services.chat_runtime import ChatRuntime

    coordinator, _, _ = _engine(rooms=ROOMS)
    result = await coordinator.run(_add_all(_named_bedroom()))

    presentation = ChatRuntime.presentation(result)

    assert presentation is not None and presentation.room_in_cart


async def test_the_room_and_finished_rooms_survive_any_state_update() -> None:
    from app.schemas.agent_updates import AgentStateUpdate, RoomProjectUpdate
    from app.services.agent_state import apply_update

    _, state = await _in_cart()
    state = state.model_copy(
        update={"finished_rooms": (FinishedRoom(room_words="living room", product_ids=(1,)),)}
    )

    after = apply_update(state, AgentStateUpdate(room_project=RoomProjectUpdate(budget=None)))

    assert after.finished_rooms == state.finished_rooms
    assert after.room_project is not None and after.room_project.in_cart


# ── Nora's priority, after "Add all to cart" ────────────────────────────────

BEDROOM_MORE = (
    ("tables", "nightstand"),
    ("bedroom", "wardrobe"),
    ("decor", "carpet"),
    ("lighting", "table-lamp"),
    ("decor", "mirror"),
)
MIRRORS = DesignCategoryNeed(
    commerce_category="decor", commerce_subcategory="mirror", priority=DesignPriority.OPTIONAL
)


async def test_after_add_all_nora_ranks_and_the_chips_are_the_rooms_pieces() -> None:
    design = FakeDesign(InteriorDesignResult(needs=(MIRRORS, RUGS)))
    coordinator, _, pipeline = _engine(rooms=ROOMS, stock=BEDROOM_MORE, design=design)

    result = await coordinator.run(_add_all(_named_bedroom()))

    assert [r.commerce_subcategory for r in pipeline.requests] == ["mirror"]
    assert [c.subcategory for c in result.companions] == ["carpet", "nightstand", "table-lamp"], (
        "the room's other pieces, in Nora's order, then the room's own"
    )
    room = result.state.room_project
    assert room is not None and room.finishing_order[:2] == ("mirror", "rug")


async def test_a_room_chip_shows_that_piece_and_keeps_the_rooms_chips() -> None:
    design = FakeDesign(InteriorDesignResult(needs=(MIRRORS, RUGS)))
    coordinator, _, pipeline = _engine(rooms=ROOMS, stock=BEDROOM_MORE, design=design)
    state = (await coordinator.run(_add_all(_named_bedroom()))).state

    for kind in (("decor", "mirror"), ("tables", "nightstand")):
        result = await coordinator.run(
            CustomerTurnInput(
                message="Show me these",
                state=state,
                context=CONTEXT,
                product_action=CompanionAction(category=kind[0], subcategory=kind[1]),
            )
        )

        assert pipeline.requests[-1].commerce_subcategory == kind[1]
        shown = {c.subcategory for c in result.companions}
        assert kind[1] not in shown and "carpet" in shown, "still the room's own chips"


async def test_a_room_nora_already_ranked_keeps_her_order() -> None:
    design = FakeDesign(InteriorDesignResult(needs=(RUGS,)))
    coordinator, _, pipeline = _engine(rooms=ROOMS, stock=BEDROOM_MORE, design=design)
    state = _named_bedroom()
    assert state.room_project is not None
    state = state.model_copy(
        update={
            "room_project": state.room_project.model_copy(
                update={"finishing_order": ("table-lamps", "mirror", "rug", "nightstands")}
            )
        }
    )

    result = await coordinator.run(_add_all(state))

    assert [r.commerce_subcategory for r in pipeline.requests] == ["table-lamp"]
    assert [c.subcategory for c in result.companions] == ["mirror", "carpet", "nightstand"]
