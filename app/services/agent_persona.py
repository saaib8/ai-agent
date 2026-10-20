"""Which assistant a chat turn is shown as: decided in code from what the turn
did, never by a model, so the same turn always carries the same name.

Sales first, then design, then shopping: a room's over-budget swap is a room
turn and a sales turn, and the money is what it is about. Where the support
agent is on, a turn about the store itself is Faysal's before any of them.
"""

from __future__ import annotations

from app.schemas.agent_decision import CommercialReason
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_turn import CustomerTurnInput, CustomerTurnResult
from app.schemas.bundle import RoomBundle

_SALES_ROOM_ACTIONS = frozenset(
    {
        # The over-budget question's yes/no and its cheaper options - for a
        # swap or a finishing touch alike: the room's money.
        "swap_confirm",
        "swap_decline",
        "swap_keep_original",
        "swap_alternatives",
        "swap_dismiss",
        "room_done",
    }
)
_DESIGN_ROOM_ACTIONS = frozenset(
    {"list_alternatives", "swap", "finishing_touches", "add_piece", "add_to_room"}
)
"""A piece of the room swapped for another of its kind, or a finishing touch
shown and added: the room being designed, not money (CLAUDE.md 10.3)."""

_SALES_PRODUCT_ACTIONS = frozenset({"goes_with", "companion"})
"""What goes with a pick: the offer, and a kind tapped from it."""


def speaker(
    turn: CustomerTurnInput, result: CustomerTurnResult, *, support_agent: bool = False
) -> AgentPersona:
    """The assistant this turn's reply is shown as."""
    if support_agent and _supports(result):
        # A question about the store is Faysal's even as their first message:
        # the reply then greets them as Faysal.
        return AgentPersona.FAYSAL
    if not turn.conversation.messages:
        # Every other first reply greets them and introduces itself as Sara.
        return AgentPersona.SARA
    if result.room_addition is not None or (
        (result.finishing_pieces or result.pieces_added)
        and result.swap_offer is None
        and result.budget_stretched is None
    ):
        # Finishing the room being designed - a touch shown, offered or added
        # within budget - is Nora's; the budget question is Salman's.
        return AgentPersona.NORA
    if _sells(turn, result):
        return AgentPersona.SALMAN
    if _designs(turn, result):
        return AgentPersona.NORA
    return AgentPersona.SARA


def _supports(result: CustomerTurnResult) -> bool:
    """A turn about the store itself: its documents, or its support team."""
    grounding = result.grounding
    return grounding.store_knowledge is not None or grounding.support is not None


def _sells(turn: CustomerTurnInput, result: CustomerTurnResult) -> bool:
    if turn.bundle_action is not None and turn.bundle_action.kind in _SALES_ROOM_ACTIONS:
        return True
    if turn.product_action is not None and turn.product_action.kind in _SALES_PRODUCT_ACTIONS:
        return True
    if result.decision.commercial_reason is CommercialReason.UPSELL:
        return True
    # The room in their cart, from "Add all to cart" on: what they add for it,
    # and closing it when they are done (CLAUDE.md 36).
    if result.room_in_cart or result.picks_for_room or result.room_finished:
        return True
    # Cross-sell: a pick shown with what goes with it, or the kinds offered.
    if result.companions or result.focus is not None:
        return True
    # The room's money side: a stretch held for their yes, or one confirmed.
    if result.swap_offer is not None or result.budget_stretched is not None:
        return True
    # A page of what goes with their pick, ordered by it.
    search = result.grounding.search
    active = result.state.active_search
    return bool(
        search is not None and search.products and active is not None and active.ordered_by_pick
    )


def _designs(turn: CustomerTurnInput, result: CustomerTurnResult) -> bool:
    if turn.bundle_action is not None and turn.bundle_action.kind in _DESIGN_ROOM_ACTIONS:
        return True
    grounding = result.grounding
    return bool(
        grounding.design_guidance
        or grounding.design_handoff_requested
        or result.room_question is not None
        or result.room_cards
        or isinstance(result.bundle_outcome, RoomBundle)
    )
