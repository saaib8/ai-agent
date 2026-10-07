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
The product-type clarification also gets stocked suggestions answering it.

Chips carry the action they perform where one exists (what goes with a pick),
so a tap runs exactly what the screen buttons run; the rest are words the
decision model reads like any typed message.
"""

from __future__ import annotations

from app.schemas.agent_decision import BlockingClarificationReason, FollowUpPolicy
from app.schemas.agent_turn import CustomerTurnResult
from app.schemas.bundle import RoomBundle
from app.schemas.language import ReplyLanguage
from app.schemas.next_step import NextStep, NextStepKind
from app.schemas.picks import PickView
from app.schemas.product_action import GoesWithPickAction
from app.schemas.reply_choice import ReplyChoice
from app.schemas.retailer import RetailerCatalogCapabilities
from app.services.chip_wording import Chip, chip
from app.taxonomy.complements import Complements
from app.taxonomy.registry import CommerceTaxonomy
from app.taxonomy.rooms import RoomPieces
from app.taxonomy.words import customer_words

MAX_PIECE_CHOICES = 6
"""A compact row of suggestions; customers can still type any other type."""


def asks_for_product_type(result: CustomerTurnResult) -> bool:
    clarification = result.grounding.clarification or result.grounding.deterministic_clarification
    return (
        clarification is not None
        and clarification.reason is BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE
    )


def next_step(
    result: CustomerTurnResult,
    complements: Complements | None,
    *,
    capabilities: RetailerCatalogCapabilities | None = None,
    taxonomy: CommerceTaxonomy | None = None,
    rooms: RoomPieces | None = None,
) -> NextStep | None:
    """Offer a next step, or chips answering the existing product-type question."""
    language = result.reply_language or ReplyLanguage.EN
    if asks_for_product_type(result) and capabilities is not None:
        chips = _piece_chips(capabilities, language, taxonomy)
        if chips:
            return NextStep(kind=NextStepKind.CHOOSE_PIECE, chips=chips)
    clarification = result.grounding.clarification or result.grounding.deterministic_clarification
    room = result.state.room_project
    if (
        clarification is not None
        and clarification.reason is BlockingClarificationReason.MISSING_ROOM_REQUIREMENTS
        and rooms is not None
        and (room is None or rooms.template(room.room_kind) is None)
    ):
        choices = _room_type_chips(rooms, language)
        if choices:
            return NextStep(kind=NextStepKind.CHOOSE_ROOM, chips=choices)
    if already_asks(result):
        return None
    grounding = result.grounding
    if (
        grounding.follow_up_policy is FollowUpPolicy.OPTIONAL
        and result.decision.follow_up_goal is not None
    ):
        # A specific preference question owns its answers; the generic next
        # step is only for turns that have no question of their own.
        return None
    picks = result.picks or ()

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
    if (
        grounding.search is not None and grounding.search.products
    ) or result.state.product_interaction.presented_product_ids:
        return NextStep(
            kind=NextStepKind.KEEP_BROWSING,
            chips=_chips(language, Chip.SHOW_MORE, Chip.NARROW_DOWN),
        )
    return NextStep(
        kind=NextStepKind.START, chips=_chips(language, Chip.FIND_A_PIECE, Chip.DESIGN_A_ROOM)
    )


def _piece_chips(
    capabilities: RetailerCatalogCapabilities,
    language: ReplyLanguage,
    taxonomy: CommerceTaxonomy | None,
) -> tuple[ReplyChoice, ...]:
    # Offer breadth first: a deep sofa shelf should not crowd out beds and tables.
    stocked = sorted(
        capabilities.capabilities,
        key=lambda item: (
            -item.active_product_count,
            item.commerce_category,
            item.commerce_subcategory or "",
        ),
    )
    categories: set[str] = set()
    first, remaining = [], []
    for item in stocked:
        if item.commerce_category in categories:
            remaining.append(item)
        else:
            first.append(item)
            categories.add(item.commerce_category)
    choices = []
    for item in first + remaining:
        key = item.commerce_subcategory or item.commerce_category
        name = taxonomy.arabic(key) if language is ReplyLanguage.AR and taxonomy else None
        if language is ReplyLanguage.EN:
            name = customer_words(key)
        if name:
            choices.append(chip(Chip.PRODUCT_TYPE, language, label=name.capitalize(), kind=name))
        if len(choices) == MAX_PIECE_CHOICES:
            break
    return tuple(choices)


def _room_type_chips(rooms: RoomPieces, language: ReplyLanguage) -> tuple[ReplyChoice, ...]:
    choices = []
    for kind in rooms.kinds:
        template = rooms.template(kind)
        if language is ReplyLanguage.AR:
            name = template.label_ar if template is not None else None
        else:
            name = kind.replace("_", " ").capitalize()
        if name:
            choices.append(chip(Chip.ROOM_TYPE, language, room=name))
    return tuple(choices)


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
