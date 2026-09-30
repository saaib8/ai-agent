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
from app.schemas.next_step import NextStep, NextStepKind
from app.schemas.picks import PickView
from app.schemas.product_action import GoesWithPickAction
from app.schemas.reply_choice import ReplyChoice
from app.taxonomy.complements import Complements
from app.taxonomy.words import customer_words


def next_step(result: CustomerTurnResult, complements: Complements | None) -> NextStep | None:
    """The next step this turn should offer, or None when it already asks one."""
    if already_asks(result):
        return None
    grounding = result.grounding
    picks = result.picks or ()

    if grounding.comparison is not None:
        return NextStep(kind=NextStepKind.AFTER_COMPARISON, chips=_comparison_chips())
    if grounding.product_detail is not None:
        return NextStep(kind=NextStepKind.AFTER_DETAIL, chips=_detail_chips(picks, complements))
    if isinstance(result.bundle_outcome, RoomBundle):
        return NextStep(kind=NextStepKind.AFTER_ROOM, chips=_room_chips())
    if picks:
        goes_with = _goes_with_pick(picks, complements)
        kind = NextStepKind.AFTER_PICKS if goes_with else NextStepKind.ROOM_AROUND_PICKS
        return NextStep(kind=kind, chips=_picks_chips(goes_with))
    if grounding.search is not None and grounding.search.products:
        return NextStep(kind=NextStepKind.KEEP_BROWSING, chips=_browsing_chips())
    return NextStep(kind=NextStepKind.START, chips=_start_chips())


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


def _picks_chips(pick: PickView | None) -> tuple[ReplyChoice, ...]:
    chips = []
    if pick is not None:
        chips.append(
            ReplyChoice(
                label=f"What goes with the {pick.kind}",
                value=f"What goes with the {pick.kind}?",
                product_action=GoesWithPickAction(pick=pick.pick),
            )
        )
    chips.append(
        ReplyChoice(label="Design a room around my picks", value="Design a room around my picks")
    )
    chips.append(ReplyChoice(label="Keep browsing", value="Show me something else"))
    return tuple(chips)


def _detail_chips(
    picks: tuple[PickView, ...], complements: Complements | None
) -> tuple[ReplyChoice, ...]:
    return (
        ReplyChoice(label="Add it to my picks", value="Add this one to my picks"),
        ReplyChoice(label="What goes with it", value="What goes with this one?"),
        ReplyChoice(label="Show similar ones", value="Show me similar ones"),
    )


def _comparison_chips() -> tuple[ReplyChoice, ...]:
    return (
        ReplyChoice(label="Take the first", value="I'll take the first one"),
        ReplyChoice(label="Take the second", value="I'll take the second one"),
        ReplyChoice(label="Show similar ones", value="Show me similar ones"),
    )


def _room_chips() -> tuple[ReplyChoice, ...]:
    return (
        ReplyChoice(label="Swap a piece", value="I'd like to swap one of the pieces"),
        ReplyChoice(label="Add a finishing touch", value="What would finish the room?"),
    )


def _browsing_chips() -> tuple[ReplyChoice, ...]:
    return (
        ReplyChoice(label="Show me more", value="Show me more options"),
        ReplyChoice(label="Narrow them down", value="Help me narrow these down"),
    )


def _start_chips() -> tuple[ReplyChoice, ...]:
    return (
        ReplyChoice(label="Find a piece", value="I'm looking for a piece of furniture"),
        ReplyChoice(label="Design a room", value="I'd like to design a room"),
    )
