"""A button the customer tapped, as a typed event the team reads.

A tap is not turned into words someone wrote for it, and it belongs to no
agent and no tool in advance: the supervisor reads it, with who offered the
button, and sends it to whoever should handle it; that agent decides what to
do, with the same tools and checks a typed message gets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from app.schemas.bundle_action import BundleActionRequest
from app.schemas.card_comparison import CardRef
from app.schemas.chat import ChatRequest
from app.schemas.product_action import ProductActionRequest
from app.schemas.search_action import SearchActionRequest


class TapEvent(BaseModel):
    """What was tapped, by the places the customer sees - never an id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    button: str
    room_piece: int | None = None
    option: int | None = None
    card: int | None = None
    cards: tuple[int, ...] | None = None
    pick: int | None = None
    picks: tuple[int, ...] | None = None
    liked: int | None = None
    requirement: str | None = None
    seating_option: int | None = None
    kind: str | None = None
    answers: dict[str, str | list[str]] | None = None

    def described(self) -> str:
        return json.dumps(self.model_dump(mode="json", exclude_none=True), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class Tap:
    event: TapEvent
    cards: tuple[CardRef, ...] = ()
    """The cards the button named, with the list each was on."""

    @property
    def said(self) -> str:
        """What the customer did, for the agents."""
        return f"[tapped a button] {self.event.described()}"

    @property
    def recorded(self) -> str:
        """The tap in history when the client sent no words for it: which
        button, with no figure a later reply could pass off as theirs."""
        return f"[tapped: {self.event.button.replace('_', ' ')}]"


def tap_of(request: ChatRequest) -> Tap | None:
    """The tap this request carries, or None for a typed message."""
    if request.bundle_action is not None:
        return Tap(_bundle(request.bundle_action))
    if request.search_action is not None:
        return Tap(_search(request.search_action))
    if request.product_action is not None:
        return _product(request.product_action)
    if request.support_action is not None:
        return Tap(TapEvent(button="contact_store"))
    return None


def _bundle(action: BundleActionRequest) -> TapEvent:
    match action.kind:
        case "list_alternatives":
            return TapEvent(button="room_piece_options", room_piece=action.bundle_ordinal)
        case "swap":
            return TapEvent(
                button="swap_room_piece",
                room_piece=action.bundle_ordinal,
                option=action.alternative_ordinal,
            )
        case "remove":
            return TapEvent(button="remove_room_piece", room_piece=action.bundle_ordinal)
        case "swap_confirm":
            return TapEvent(button="accept_over_budget_offer")
        case "swap_decline" | "swap_keep_original" | "swap_dismiss":
            return TapEvent(button="decline_over_budget_offer")
        case "swap_alternatives":
            return TapEvent(button="cheaper_options_within_budget")
        case "add_piece":
            return TapEvent(button="add_piece_to_room", kind=action.piece)
        case "finishing_touches":
            return TapEvent(button="finishing_touches")
        case "add_to_room":
            return TapEvent(button="add_card_to_room", card=action.ordinal)
        case "room_done":
            return TapEvent(button="done_with_room")
        case "add_to_cart":
            return TapEvent(button="add_room_to_cart")
    raise AssertionError(f"unhandled room action {action.kind}")


def _search(action: SearchActionRequest) -> TapEvent:
    match action.kind:
        case "more_options":
            return TapEvent(button="more_results")
        case "exclude":
            return TapEvent(button="not_this_card", card=action.ordinal)
        case "drop":
            return TapEvent(button="drop_requirement", requirement=str(action.facet))
        case "combination":
            if action.op == "choose" and action.position:
                return TapEvent(button="choose_seating_option", seating_option=action.position)
            if action.op == "dismiss" and action.position:
                return TapEvent(button="not_this_seating_option", seating_option=action.position)
            return TapEvent(button="more_seating_options")
        case "brief" | "taste":
            answers = _answers(action)
            if not answers:
                return TapEvent(button="skip_questions")
            return TapEvent(button="answers", answers=answers)
    raise AssertionError(f"unhandled search action {action.kind}")


def _answers(action: SearchActionRequest) -> dict[str, str | list[str]]:
    said: dict[str, str | list[str]] = {
        name: str(v)
        for name in ("piece", "room", "people", "budget", "space", "feel", "answer")
        if (v := getattr(action, name, None))
    }
    for name in ("colours", "styles", "feels"):
        if values := getattr(action, name, ()):
            said[name] = [str(v) for v in values]
    return said


def _product(action: ProductActionRequest) -> Tap:
    match action.kind:
        case "goes_with":
            return Tap(TapEvent(button="what_goes_with_pick", pick=action.pick))
        case "compare":
            return Tap(TapEvent(button="compare_picks", picks=tuple(action.picks)))
        case "compare_cards":
            return Tap(
                TapEvent(button="compare_cards", cards=tuple(c.ordinal for c in action.cards)),
                tuple(action.cards),
            )
        case "companion":
            return Tap(TapEvent(button="show_what_goes_with_it", kind=action.subcategory))
        case "more_like_this":
            if action.liked is not None:
                return Tap(TapEvent(button="more_like_this", liked=action.liked))
            if action.card is None:
                return Tap(TapEvent(button="more_like_this"))
            return Tap(TapEvent(button="more_like_this", card=action.card.ordinal), (action.card,))
        case "add_to_cart":
            if action.card is None:
                return Tap(TapEvent(button="add_to_cart"))
            return Tap(
                TapEvent(button="add_card_to_cart", card=action.card.ordinal), (action.card,)
            )
    raise AssertionError(f"unhandled product action {action.kind}")
