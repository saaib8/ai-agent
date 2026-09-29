"""A room question's answers as chips (CLAUDE.md 10.3).

Built from the question the application decided to ask - the room registry and
the store's real catalogue - never guessed from the reply's words. Every kind of
room question carries its own real answers: the pieces the store stocks, the seat
counts, the budget bands, and the store's own colours. The one thing the customer
taps is therefore always an answer to the question they were actually asked.
"""

from __future__ import annotations

from app.schemas.chat import PieceChoice, PiecePicker, ReplyChoice
from app.schemas.room_opener import RoomQuestion, RoomQuestionKind
from app.taxonomy.rooms import PieceTier

CHOOSE_FOR_ME = ReplyChoice(label="Choose for me", value="Choose the pieces for me")

_SEAT_COUNTS = (2, 3, 4, 5)
"""The seat counts offered as chips; larger households type the number. The
'six or more' chip caps the row, and any earlier head count leads it."""

_ROOM_BUDGET_BANDS: tuple[ReplyChoice, ...] = (
    ReplyChoice(label="Under 10,000 SAR", value="A budget under 10000 SAR"),
    ReplyChoice(label="10,000-25,000 SAR", value="A budget between 10000 and 25000 SAR"),
    ReplyChoice(label="25,000-50,000 SAR", value="A budget between 25000 and 50000 SAR"),
    ReplyChoice(label="No strict limit", value="No strict budget"),
)
"""Coarse whole-room ranges, plus the way out the question offers. Deliberately
few: a room's total has no single catalogue basis, so inventing fine thresholds
would be a guess of its own - these are round anchors a person recognises."""

LEAVE_THE_PALETTE = ReplyChoice(label="Leave it to you", value="Leave the palette to you")
"""The 'or would you rather leave it to me?' the colour question always offers,
so a tap-only customer can take the path the words promise."""


def room_answer_choices(question: RoomQuestion) -> tuple[ReplyChoice, ...]:
    """The tappable answers to this room question, built from real options.

    Empty for a pieces question - `piece_picker` draws those as its own control -
    and empty for a colour question at a store that records no colour, where the
    reply simply invites them to say. Every other case carries real answers, so
    the chip beside the reply can only ever be an answer to the question asked.
    """
    match question.kind:
        case RoomQuestionKind.PIECES:
            return ()
        case RoomQuestionKind.SEATS:
            return _seat_choices(question.earlier_seat_count)
        case RoomQuestionKind.BUDGET:
            return _ROOM_BUDGET_BANDS
        case RoomQuestionKind.COLOUR:
            return _colour_choices(question.colours)


def _seat_choices(earlier: int | None) -> tuple[ReplyChoice, ...]:
    chips: list[ReplyChoice] = []
    if earlier is not None:
        # The question confirms an earlier head count ("is it for the nine you
        # mentioned?"), so leading with it lets a tap say yes.
        chips.append(ReplyChoice(label=f"Yes, {earlier}", value=f"Seating for {earlier} people"))
    for count in _SEAT_COUNTS:
        if count != earlier:
            chips.append(ReplyChoice(label=f"{count} people", value=f"Seating for {count} people"))
    chips.append(ReplyChoice(label="6+ people", value="Seating for 6 or more people"))
    return tuple(chips)


def _colour_choices(colours: tuple[str, ...]) -> tuple[ReplyChoice, ...]:
    if not colours:
        # Nothing real to offer, so offer nothing rather than a guessed palette;
        # the reply still invites them to say a colour in words.
        return ()
    return (
        *(
            ReplyChoice(label=colour, value=f"I'd like {colour.lower()} tones")
            for colour in colours
        ),
        LEAVE_THE_PALETTE,
    )


def piece_picker(question: RoomQuestion) -> PiecePicker | None:
    """The chips for a pieces question; nothing for any other question."""
    if not question.pieces:
        return None
    return PiecePicker(
        pieces=tuple(
            PieceChoice(
                label=piece.label,
                selected=piece.selected,
                essential=piece.tier is PieceTier.ESSENTIAL,
            )
            for piece in question.pieces
        ),
        submit_label="Design my room",
        choose_for_me=CHOOSE_FOR_ME,
    )
