"""A room question's chips are its real answers, keyed off the question's kind.

The chip the customer taps can only ever be an answer to the question they were
asked - the reply's wording never decides the chips (the bug this replaced).
"""

from __future__ import annotations

from app.schemas.room_opener import RoomPieceOffer, RoomQuestion, RoomQuestionKind
from app.services.room_presentation import room_answer_choices
from app.taxonomy.rooms import PieceTier


def _q(kind: RoomQuestionKind, **fields: object) -> RoomQuestion:
    return RoomQuestion(room_kind="living_room", kind=kind, **fields)


def test_seats_offers_counts() -> None:
    chips = room_answer_choices(_q(RoomQuestionKind.SEATS))

    assert [c.label for c in chips] == ["2 people", "3 people", "4 people", "5 people", "6+ people"]
    assert all("people" in c.value for c in chips)


def test_seats_leads_with_an_earlier_head_count() -> None:
    """The question confirms a count from before ("is it for the 9?"), so a tap
    can say yes."""
    chips = room_answer_choices(_q(RoomQuestionKind.SEATS, earlier_seat_count=9))

    assert chips[0].label == "Yes, 9"
    assert "9 people" in chips[0].value
    assert "9 people" not in [c.label for c in chips[1:]]  # not offered twice


def test_budget_offers_bands_and_the_way_out() -> None:
    chips = room_answer_choices(_q(RoomQuestionKind.BUDGET))

    assert chips[-1].label == "No strict limit"
    assert chips[-1].value == "No strict budget"
    assert len(chips) >= 2


def test_colour_offers_the_stores_colours_then_a_skip() -> None:
    chips = room_answer_choices(
        _q(RoomQuestionKind.COLOUR, colours=("Beige", "Grey", "Denim Blue"))
    )

    assert [c.label for c in chips] == ["Beige", "Grey", "Denim Blue", "Leave it to you"]
    assert "beige" in chips[0].value.lower()
    assert chips[-1].value == "Leave the palette to you"


def test_colour_with_no_stocked_colour_offers_nothing() -> None:
    """Rather than a guessed palette: the reply still invites them to say one."""
    assert room_answer_choices(_q(RoomQuestionKind.COLOUR)) == ()


def test_pieces_leaves_the_chips_to_the_picker() -> None:
    picker_question = _q(
        RoomQuestionKind.PIECES,
        pieces=(RoomPieceOffer(key="sofa", label="Sofa", tier=PieceTier.ESSENTIAL, selected=True),),
    )

    assert room_answer_choices(picker_question) == ()
