"""Which assistant a chat turn is shown as: decided in code from what the turn
did, never by a model, so the same turn always carries the same name.

Sales first, then design, then shopping: a room's over-budget swap is a room
turn and a sales turn, and the money is what it is about.
"""

from __future__ import annotations

from app.schemas.agent_decision import CommercialReason
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_turn import CustomerTurnInput, CustomerTurnResult
from app.schemas.bundle import RoomBundle

_SALES_ROOM_ACTIONS = frozenset(
    {
        # The over-budget swap's yes/no and its cheaper options.
        "swap_confirm",
        "swap_decline",
        "swap_keep_original",
        "swap_alternatives",
        "swap_dismiss",
        # Finishing touches: the offer and the piece added.
        "finishing_touches",
        "add_piece",
    }
)
_DESIGN_ROOM_ACTIONS = frozenset({"list_alternatives", "swap"})
"""A piece of the room swapped for another of its kind: the room, not money."""

_SALES_PRODUCT_ACTIONS = frozenset({"goes_with", "companion"})
"""What goes with a pick: the offer, and a kind tapped from it."""


def speaker(turn: CustomerTurnInput, result: CustomerTurnResult) -> AgentPersona:
    """The assistant this turn's reply is shown as."""
    if not turn.conversation.messages:
        # The first reply greets them and introduces itself as Sara.
        return AgentPersona.SARA
    if _sells(turn, result):
        return AgentPersona.SALMAN
    if _designs(turn, result):
        return AgentPersona.NORA
    return AgentPersona.SARA


def _sells(turn: CustomerTurnInput, result: CustomerTurnResult) -> bool:
    if turn.bundle_action is not None and turn.bundle_action.kind in _SALES_ROOM_ACTIONS:
        return True
    if turn.product_action is not None and turn.product_action.kind in _SALES_PRODUCT_ACTIONS:
        return True
    if result.decision.commercial_reason is CommercialReason.UPSELL:
        return True
    # Cross-sell: a pick shown with what goes with it, or the kinds offered.
    if result.companions or result.focus is not None:
        return True
    # The room's money side: a stretch held for their yes, one confirmed, and
    # finishing touches offered or added.
    if result.swap_offer is not None or result.budget_stretched is not None:
        return True
    if result.finishing_pieces or result.pieces_added:
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
