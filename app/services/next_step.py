"""The next step every reply offers, so a conversation never dead-ends.

A reply that answers and stops leaves the customer to work out what to do next -
"that sofa's saved too", "you're very welcome" - and in a showroom that is the
moment a salesperson would point somewhere. This decides, from what the turn
produced and what the customer has picked, the one next step worth offering:
a question the reply ends on, and the chips that answer it (CLAUDE.md 10.2).

A pick is a moment to cross-sell, never to compare: a second sofa beside the
first is offered what goes with it, like any pick (10.4). Comparing is theirs
to ask for.

Deterministic, and it defers: a turn that already asks something - a room
question, a card of questions, a seating shape, the companions of a pick, a
clarification - keeps its own question and gets no second one.

Chips carry the action they perform where one exists (what goes with a pick),
so a tap runs exactly what the screen buttons run; the rest are words the
decision model reads like any typed message.
"""

from __future__ import annotations

from app.schemas.agent_turn import CustomerTurnResult
from app.schemas.bundle import RoomBundle
from app.schemas.language import ReplyLanguage
from app.schemas.next_step import NextStep, NextStepKind
from app.schemas.picks import PickView
from app.schemas.product_action import GoesWithPickAction
from app.schemas.reply_choice import ReplyChoice
from app.services.chip_wording import Chip, chip
from app.taxonomy.complements import Complements
from app.taxonomy.words import customer_words


def next_step(result: CustomerTurnResult, complements: Complements | None) -> NextStep | None:
    """The next step this turn should offer, or None when it already asks one.

    Its chips are worded in the turn's language; which chips, and what they
    do, never depend on it.
    """
    if already_asks(result):
        return None
    grounding = result.grounding
    picks = result.picks or ()
    language = result.reply_language or ReplyLanguage.EN

    if grounding.comparison is not None:
        return NextStep(
            kind=NextStepKind.AFTER_COMPARISON,
            chips=_chips(language, Chip.TAKE_FIRST, Chip.TAKE_SECOND, Chip.SHOW_SIMILAR),
        )
    if grounding.product_detail is not None:
        return NextStep(
            kind=NextStepKind.AFTER_DETAIL,
            chips=_chips(language, Chip.ADD_TO_PICKS, Chip.WHAT_GOES_WITH_IT, Chip.SHOW_SIMILAR),
        )
    if isinstance(result.bundle_outcome, RoomBundle):
        return NextStep(
            kind=NextStepKind.AFTER_ROOM,
            chips=_chips(language, Chip.SWAP_A_PIECE, Chip.FINISHING_TOUCH),
        )
    if picks:
        goes_with = _goes_with_pick(picks, complements)
        kind = NextStepKind.AFTER_PICKS if goes_with else NextStepKind.ROOM_AROUND_PICKS
        return NextStep(kind=kind, chips=_picks_chips(goes_with, language))
    if grounding.search is not None and grounding.search.products:
        return NextStep(
            kind=NextStepKind.KEEP_BROWSING,
            chips=_chips(language, Chip.SHOW_MORE, Chip.NARROW_DOWN),
        )
    return NextStep(
        kind=NextStepKind.START, chips=_chips(language, Chip.FIND_A_PIECE, Chip.DESIGN_A_ROOM)
    )


def already_asks(result: CustomerTurnResult) -> bool:
    """A turn with its own question keeps it: never a second one beside it."""
    grounding = result.grounding
    return bool(
        result.room_question is not None
        or result.product_brief is not None
        or result.swap_offer is not None
        or result.seating_solution is not None
        or result.companions
        or grounding.clarification is not None
        or grounding.deterministic_clarification is not None
    )


def _goes_with_pick(
    picks: tuple[PickView, ...], complements: Complements | None
) -> PickView | None:
    """The newest pick something reviewed goes with - never a type with no pairings,
    whose "what goes with it" would come back empty."""
    if complements is None:
        return None
    # Picks carry the type in customer words ("sofa set"), so match them against
    # the registry's own types worded the same way - never a key rebuilt by hand.
    paired = {customer_words(anchor) for anchor in complements.anchors}
    return next(
        (pick for pick in reversed(picks) if pick.kind is not None and pick.kind in paired),
        None,
    )


def _picks_chips(pick: PickView | None, language: ReplyLanguage) -> tuple[ReplyChoice, ...]:
    chips = []
    if pick is not None:
        chips.append(
            chip(
                Chip.WHAT_GOES_WITH_PICK,
                language,
                product_action=GoesWithPickAction(pick=pick.pick),
                kind=pick.kind,
            )
        )
    chips.append(chip(Chip.ROOM_AROUND_PICKS, language))
    chips.append(chip(Chip.KEEP_BROWSING, language))
    return tuple(chips)


def _chips(language: ReplyLanguage, *keys: Chip) -> tuple[ReplyChoice, ...]:
    return tuple(chip(key, language) for key in keys)
