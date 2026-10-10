"""A room question's answers as chips (CLAUDE.md 10.3).

Built from the question the application decided to ask - the room registry and
the store's real catalogue - never guessed from the reply's words. Every kind of
room question carries its own real answers: the pieces the store stocks, the seat
counts, the budget bands, and the store's own colours. The one thing the customer
taps is therefore always an answer to the question they were actually asked.
"""

from __future__ import annotations

from app.schemas.agent_state import SwapBudgetOfferStage
from app.schemas.agent_turn import SwapBudgetOffer
from app.schemas.bundle_action import (
    AddPieceAction,
    BundleAlternativesAction,
    SwapAlternativesAction,
    SwapConfirmAction,
    SwapDeclineAction,
    SwapKeepOriginalAction,
)
from app.schemas.chat import PieceChoice, PiecePicker
from app.schemas.language import ReplyLanguage
from app.schemas.reply_choice import ReplyChoice
from app.schemas.room_opener import (
    RoomCardChoice,
    RoomPieceOffer,
    RoomQuestion,
    RoomQuestionKind,
)
from app.services.chip_wording import DESIGN_MY_ROOM, PIECE_PICKED, Chip, chip
from app.taxonomy.rooms import PieceTier

EN = ReplyLanguage.EN


def swap_offer_choices(
    offer: SwapBudgetOffer, language: ReplyLanguage = EN
) -> tuple[ReplyChoice, ...]:
    """The yes/no chips for a held over-budget swap, keyed off which question it
    is on - so a tap always answers the question actually asked (CLAUDE.md 27).
    Each carries its room edit, so the words can be in any language."""
    match offer.stage:
        case SwapBudgetOfferStage.STRETCH:
            return (
                chip(Chip.STRETCH_YES, language, bundle_action=SwapConfirmAction()),
                chip(Chip.STRETCH_NO, language, bundle_action=SwapDeclineAction()),
            )
        case SwapBudgetOfferStage.ALTERNATIVES:
            # The two ways to stay in budget: keep the room they already had, or
            # look for a cheaper version of just the piece they were swapping.
            return (
                chip(Chip.KEEP_ORIGINAL_ROOM, language, bundle_action=SwapKeepOriginalAction()),
                chip(Chip.CHEAPER_PIECE, language, bundle_action=SwapAlternativesAction()),
            )


_SEAT_COUNTS = (2, 3, 4, 5)
"""The seat counts offered as chips; larger households type the number. The
'six or more' chip caps the row, and any earlier head count leads it."""

_ROOM_BUDGET_EDGES: tuple[int, ...] = (10_000, 25_000, 50_000)
"""Coarse whole-room ranges, plus the way out the question offers. Deliberately
few: a room's total has no single catalogue basis, so inventing fine thresholds
would be a guess of its own - these are round anchors a person recognises."""


def room_answer_choices(
    question: RoomQuestion, language: ReplyLanguage = EN
) -> tuple[ReplyChoice, ...]:
    """The tappable answers to this room question, built from real options.

    Empty for a pieces question - `piece_picker` draws those as its own control -
    and empty for a colour question at a store that records no colour, where the
    reply simply invites them to say. Every other case carries real answers, so
    the chip beside the reply can only ever be an answer to the question asked.
    Worded in `language`.
    """
    match question.kind:
        case RoomQuestionKind.PIECES:
            return ()
        case RoomQuestionKind.SEATS:
            if question.picked_seat_count is not None:
                return _picked_seat_choices(question.picked_seat_count, language)
            return _seat_choices(question.earlier_seat_count, language)
        case RoomQuestionKind.BUDGET:
            return _budget_choices(language)
        case RoomQuestionKind.COLOUR:
            return _colour_choices(question, language)


def _budget_choices(language: ReplyLanguage) -> tuple[ReplyChoice, ...]:
    low, *rest = _ROOM_BUDGET_EDGES
    chips = [chip(Chip.ROOM_BUDGET_UNDER, language, high=f"{low:,}", high_plain=low)]
    for lower, upper in zip(_ROOM_BUDGET_EDGES, rest, strict=False):
        chips.append(
            chip(
                Chip.ROOM_BUDGET_BETWEEN,
                language,
                low=f"{lower:,}",
                high=f"{upper:,}",
                low_plain=lower,
                high_plain=upper,
            )
        )
    chips.append(chip(Chip.NO_STRICT_BUDGET, language))
    return tuple(chips)


def _seat_choices(earlier: int | None, language: ReplyLanguage) -> tuple[ReplyChoice, ...]:
    chips: list[ReplyChoice] = []
    if earlier is not None:
        # The question confirms an earlier head count ("is it for the nine you
        # mentioned?"), so leading with it lets a tap say yes.
        chips.append(chip(Chip.SEATS_CONFIRM, language, count=earlier))
    chips.extend(
        chip(Chip.SEATS, language, count=count) for count in _SEAT_COUNTS if count != earlier
    )
    chips.append(chip(Chip.SEATS_OR_MORE, language, count=6))
    return tuple(chips)


def _picked_seat_choices(picked: int, language: ReplyLanguage) -> tuple[ReplyChoice, ...]:
    """Their picks already seat `picked`: a tap confirms that is everyone, or
    says how many more - never fewer, since the sofas they chose stay."""
    return (
        chip(Chip.SEATS_CONFIRM, language, count=picked),
        *(chip(Chip.SEATS, language, count=count) for count in (picked + 1, picked + 2)),
        chip(Chip.SEATS_OR_MORE, language, count=picked + 3),
    )


def _colour_choices(question: RoomQuestion, language: ReplyLanguage) -> tuple[ReplyChoice, ...]:
    if not question.colours:
        # Nothing real to offer, so offer nothing rather than a guessed palette;
        # the reply still invites them to say a colour in words.
        return ()
    # An approved colour as the chip shows it: its reviewed Arabic name in an
    # Arabic session, the stored value otherwise.
    names = question.colours_ar if language is not EN and question.colours_ar else question.colours
    return (
        *(
            chip(Chip.ROOM_COLOUR, language, colour=name, colour_lower=colour.lower())
            for colour, name in zip(question.colours, names, strict=True)
        ),
        chip(Chip.LEAVE_THE_PALETTE, language),
    )


def piece_picker(question: RoomQuestion, language: ReplyLanguage = EN) -> PiecePicker | None:
    """The chips for a pieces question; nothing for any other question."""
    if not question.pieces:
        return None
    return PiecePicker(
        pieces=tuple(
            PieceChoice(
                label=_piece_label(piece.label, piece.label_ar, piece.picked, language),
                selected=piece.selected,
                essential=piece.tier is PieceTier.ESSENTIAL,
            )
            for piece in question.pieces
        ),
        submit_label=DESIGN_MY_ROOM[language],
        choose_for_me=chip(Chip.CHOOSE_FOR_ME, language),
    )


def _piece_label(label: str, label_ar: str | None, picked: bool, language: ReplyLanguage) -> str:
    """English keeps the label the registry and the room built; Arabic names
    the piece from the reviewed registry, marked as theirs when it is."""
    if language is EN or label_ar is None:
        return label
    return PIECE_PICKED[language].format(label=label_ar) if picked else label_ar


def finishing_choices(
    pieces: tuple[RoomPieceOffer, ...], language: ReplyLanguage = EN
) -> tuple[ReplyChoice, ...]:
    """A finished room's finishing touches as chips: each piece it could still
    take, then "you choose". Each carries its room edit, so a tap adds exactly
    that piece - or lets the designer pick one - whatever the words say
    (CLAUDE.md 3.6)."""
    if not pieces:
        return ()
    return (
        *(
            chip(
                Chip.ADD_PIECE,
                language,
                bundle_action=AddPieceAction(piece=piece.key),
                label=_piece_label(piece.label, piece.label_ar, False, language),
            )
            for piece in pieces
        ),
        chip(Chip.YOU_CHOOSE_PIECE, language, bundle_action=AddPieceAction()),
    )


def room_card_choices(
    cards: tuple[RoomCardChoice, ...], language: ReplyLanguage = EN
) -> tuple[ReplyChoice, ...]:
    """A room's pieces as chips answering "which piece?". Each carries the
    alternatives action its Swap button sends, so a tap shows that piece's
    options and nothing is read from the words (CLAUDE.md 3.6)."""
    return tuple(
        chip(
            Chip.ROOM_CARD,
            language,
            bundle_action=BundleAlternativesAction(bundle_ordinal=card.ordinal),
            label=card.label_ar if language is not EN and card.label_ar else card.label,
            piece=card.label.lower(),
        )
        for card in cards
    )
