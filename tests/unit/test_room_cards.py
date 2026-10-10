""" "Which piece?" beside a finished room (CLAUDE.md 10.2).

"I'd like to swap one of the pieces", "I'd like to fine-tune another piece":
the decision model asks which, and gives no answers of its own - so the reply
used to stop on the question with nothing to tap. The room's pieces are the
answers: each becomes a chip doing exactly what that piece's Swap button does,
numbered as the room's cards are.
"""

from __future__ import annotations

from typing import Any

from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CommercialReason,
    CustomerAgentDecision,
    FollowUpPolicy,
)
from app.schemas.agent_state import BundleItemState, BundleItemStatus, RoomProjectState
from app.schemas.agent_turn import CustomerTurnResult
from app.schemas.bundle_action import BundleAlternativesAction
from app.schemas.language import ReplyLanguage
from app.schemas.room_opener import RoomCardChoice
from app.schemas.text_choice import TextReplyChoice
from app.services.chat_runtime import ChatRuntime
from app.services.room_presentation import room_card_choices

from tests.unit.test_finishing_touch import ROOMS, RoomHydration
from tests.unit.test_turn_coordinator import _coordinator, _state, _turn

SOFA, TABLE, RUG = 501, 502, 503
"""Kinds as `RoomHydration` reads them: a sofa, a centre table, a rug."""


def _line(line_id: int, product_id: int, *, locked: bool = False) -> BundleItemState:
    return BundleItemState(
        line_id=line_id,
        product_id=product_id,
        quantity=1,
        acquisition=BundleAcquisition.TO_BUY,
        status=BundleItemStatus.LOCKED if locked else BundleItemStatus.SUGGESTED,
    )


def _room(*lines: BundleItemState) -> RoomProjectState:
    return RoomProjectState(
        room_kind="living_room",
        questions_done=True,
        bundle_items=lines or (_line(1, SOFA, locked=True), _line(2, TABLE), _line(3, RUG)),
        next_bundle_line_id=10,
    )


def _which_piece(
    *choices: str,
    reason: BlockingClarificationReason = BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.CLARIFY,
        commercial_reason=CommercialReason.CUSTOMER_REQUEST,
        follow_up_policy=FollowUpPolicy.NONE,
        clarification=BlockingClarification(
            reason=reason,
            question="Which piece would you like to swap?",
            choices=tuple(TextReplyChoice(label=c, value=c) for c in choices),
        ),
    )


async def _run(decision: CustomerAgentDecision, room: RoomProjectState | None) -> Any:
    coordinator, _ = _coordinator(
        decision,
        rooms=ROOMS,
        hydration=RoomHydration(),  # type: ignore[arg-type]
    )
    result: CustomerTurnResult = await coordinator.run(
        _turn(_state(room=room, selected=(), presented=()), "I'd like to swap one of the pieces")
    )
    return result


async def test_which_piece_offers_each_swappable_piece_numbered_as_its_card() -> None:
    result = await _run(_which_piece(), _room())

    # The locked sofa has no Swap button, so no chip; the others keep their
    # card numbers.
    assert result.room_cards == (
        RoomCardChoice(ordinal=2, label="Center table", label_ar=result.room_cards[0].label_ar),
        RoomCardChoice(ordinal=3, label="Rug", label_ar=result.room_cards[1].label_ar),
    )
    assert all(card.label_ar for card in result.room_cards)


async def test_each_chip_is_that_pieces_swap() -> None:
    result = await _run(_which_piece(), _room())

    presentation = ChatRuntime.presentation(result)

    assert presentation is not None
    assert [c.label for c in presentation.choices] == ["Center table", "Rug"]
    assert [c.bundle_action for c in presentation.choices] == [
        BundleAlternativesAction(bundle_ordinal=2),
        BundleAlternativesAction(bundle_ordinal=3),
    ]
    assert presentation.choices[1].value == "Show me other rug options"


async def test_the_models_own_answers_are_kept() -> None:
    result = await _run(_which_piece("The rug", "The table"), _room())

    assert result.room_cards == ()


async def test_without_a_room_nothing_is_offered() -> None:
    result = await _run(_which_piece(), None)

    assert result.room_cards == ()


async def test_another_question_is_not_answered_with_pieces() -> None:
    result = await _run(
        _which_piece(reason=BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE), _room()
    )

    assert result.room_cards == ()


async def test_a_room_with_nothing_to_swap_offers_nothing() -> None:
    result = await _run(_which_piece(), _room(_line(1, SOFA, locked=True)))

    assert result.room_cards == ()


async def test_two_pieces_of_one_kind_stay_tellable_apart() -> None:
    result = await _run(_which_piece(), _room(_line(1, RUG), _line(2, TABLE), _line(3, 504)))

    labels = [card.label for card in result.room_cards]
    assert labels[1] == "Center table"
    # 504 reads back as a sofa: seating is named by its type, never "Sofa" the
    # room's whole seating.
    assert labels == ["Rug", "Center table", "Sofa"]


def test_in_arabic_the_chips_name_the_pieces_in_arabic() -> None:
    cards = (RoomCardChoice(ordinal=1, label="Rug", label_ar="سجادة"),)

    (chip,) = room_card_choices(cards, ReplyLanguage.AR)

    assert chip.label == "سجادة"
    assert chip.bundle_action == BundleAlternativesAction(bundle_ordinal=1)


def test_duplicate_kinds_are_numbered() -> None:
    from app.services.room_presentation import room_card_choices as chips

    cards = (
        RoomCardChoice(ordinal=1, label="Rug"),
        RoomCardChoice(ordinal=2, label="Rug 2"),
    )

    assert [c.label for c in chips(cards)] == ["Rug", "Rug 2"]
