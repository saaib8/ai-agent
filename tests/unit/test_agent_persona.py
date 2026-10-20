"""Which assistant a reply is shown as (`ChatResponse.agent`).

A label decided in code from what the turn did: Sara greets and shops
through everything that is not sales or design, Salman sells - cross-sell,
up-sell, a room's money side - and Nora designs. One assistant answers every turn behind them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.schemas.agent_decision import AgentAction, CommercialReason, CustomerAgentDecision
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1, SwapBudgetOfferStage
from app.schemas.agent_turn import (
    CustomerTurnInput,
    CustomerTurnResult,
    SwapBudgetOffer,
    TurnGrounding,
)
from app.schemas.bundle import BundleStatus, RoomBundle
from app.schemas.bundle_action import (
    AddPieceAction,
    AddToRoomAction,
    BundleAlternativesAction,
    FinishingTouchesAction,
    SwapConfirmAction,
)
from app.schemas.conversation import ConversationContext, ConversationMessage, ConversationRole
from app.schemas.design import DesignGuidance, GuidanceTopic
from app.schemas.product_action import GoesWithPickAction
from app.schemas.room_opener import RoomPieceOffer
from app.schemas.store_knowledge import StoreKnowledgeGrounding
from app.schemas.support import SupportGrounding
from app.services.agent_persona import speaker
from app.taxonomy.rooms import PieceTier

from tests.unit.test_turn_coordinator import CONTEXT

NORA, SALMAN, SARA = AgentPersona.NORA, AgentPersona.SALMAN, AgentPersona.SARA
FAYSAL = AgentPersona.FAYSAL

HISTORY = ConversationContext(
    messages=(
        ConversationMessage(role=ConversationRole.USER, content="hi"),
        ConversationMessage(role=ConversationRole.ASSISTANT, content="Hello, I'm Sara."),
    )
)


def _turn(*, first: bool = False, **action: Any) -> CustomerTurnInput:
    return CustomerTurnInput(
        message="something",
        state=AgentStateV1(),
        context=CONTEXT,
        conversation=ConversationContext() if first else HISTORY,
        **action,
    )


def _result(
    reason: CommercialReason = CommercialReason.CUSTOMER_REQUEST,
    grounding: TurnGrounding | None = None,
    **fields: Any,
) -> CustomerTurnResult:
    return CustomerTurnResult(
        state=AgentStateV1(),
        decision=CustomerAgentDecision(action=AgentAction.ANSWER, commercial_reason=reason),
        grounding=grounding or TurnGrounding(),
        **fields,
    )


def _room() -> RoomBundle:
    return RoomBundle(status=BundleStatus.COMPLETE, new_spend_total=Decimal(0), currency="SAR")


def test_the_first_reply_greets_as_sara() -> None:
    assert speaker(_turn(first=True), _result()) is SARA


def test_sara_shops_through_everything_else() -> None:
    assert speaker(_turn(), _result()) is SARA


def test_an_upsell_is_salman() -> None:
    assert speaker(_turn(), _result(CommercialReason.UPSELL)) is SALMAN


def test_what_goes_with_a_pick_is_salman() -> None:
    assert speaker(_turn(product_action=GoesWithPickAction(pick=1)), _result()) is SALMAN


def test_finishing_touches_are_nora() -> None:
    offer = RoomPieceOffer(key="vase", label="Vase", tier=PieceTier.OPTIONAL, selected=False)

    assert speaker(_turn(), _result(finishing_pieces=(offer,))) is NORA
    assert speaker(_turn(bundle_action=FinishingTouchesAction()), _result()) is NORA
    assert speaker(_turn(), _result(room_addition="vase")) is NORA
    tapped = _turn(bundle_action=AddPieceAction(piece="vase"))
    assert speaker(tapped, _result(bundle_outcome=_room())) is NORA
    added = _turn(bundle_action=AddToRoomAction(ordinal=1))
    assert speaker(added, _result(bundle_outcome=_room(), pieces_added=("vase",))) is NORA


def test_an_addition_over_budget_is_salmans_question() -> None:
    offer = SwapBudgetOffer(
        stage=SwapBudgetOfferStage.STRETCH,
        new_spend_total=Decimal(4000),
        budget_max=Decimal(3500),
        overage=Decimal(500),
        currency="SAR",
        added_piece="vase",
    )
    added = _turn(bundle_action=AddToRoomAction(ordinal=1))

    assert speaker(added, _result(bundle_outcome=_room(), swap_offer=offer)) is SALMAN


def test_a_rooms_money_side_outranks_the_room() -> None:
    """The over-budget swap's yes lands on a room, and is about the money."""
    turn = _turn(bundle_action=SwapConfirmAction())

    assert speaker(turn, _result(bundle_outcome=_room())) is SALMAN


def test_the_room_and_design_answers_are_nora() -> None:
    guidance = DesignGuidance(topic=GuidanceTopic.COLOR, summary="Keep it light.")

    assert speaker(_turn(), _result(bundle_outcome=_room())) is NORA
    assert speaker(_turn(), _result(grounding=TurnGrounding(design_guidance=(guidance,)))) is NORA
    swap_a_piece = _turn(bundle_action=BundleAlternativesAction(bundle_ordinal=2))
    assert speaker(swap_a_piece, _result()) is NORA


def _store_answer() -> TurnGrounding:
    return TurnGrounding(
        store_knowledge=StoreKnowledgeGrounding(question="How long is delivery to Jeddah?")
    )


def test_a_turn_about_the_store_is_faysal_where_the_support_agent_is_on() -> None:
    assert speaker(_turn(), _result(grounding=_store_answer()), support_agent=True) is FAYSAL
    supported = TurnGrounding(support=SupportGrounding(requested=True))
    assert speaker(_turn(), _result(grounding=supported), support_agent=True) is FAYSAL


def test_faysal_outranks_sales_on_a_turn_about_the_store() -> None:
    result = _result(CommercialReason.UPSELL, grounding=_store_answer())

    assert speaker(_turn(), result, support_agent=True) is FAYSAL


def test_without_the_support_agent_a_turn_about_the_store_is_as_before() -> None:
    assert speaker(_turn(), _result(grounding=_store_answer())) is SARA


def test_a_first_message_about_the_store_is_faysals() -> None:
    result = _result(grounding=_store_answer())

    assert speaker(_turn(first=True), result, support_agent=True) is FAYSAL


def test_without_the_support_agent_the_first_reply_is_still_sara() -> None:
    result = _result(grounding=_store_answer())

    assert speaker(_turn(first=True), result) is SARA


def test_the_next_shopping_turn_is_sara_again() -> None:
    assert speaker(_turn(), _result(), support_agent=True) is SARA


def test_the_reply_is_told_it_speaks_as_faysal() -> None:
    """The label and the reply's own name come from the same call."""
    result = _result(grounding=_store_answer())

    assert speaker(_turn(), result, support_agent=True) is FAYSAL
    assert speaker(_turn(), result) is SARA
