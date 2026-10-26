"""What a client may be told while a turn is still working.

Which agent is working, and in a word what it is doing - never a tool's
arguments, a product, a figure or anything a model wrote. The final reply is
sent only once the turn is committed.
"""

from __future__ import annotations

from enum import StrEnum

from app.agents.trace import TraceEvent


class Phase(StrEnum):
    ROUTING = "routing"
    WORKING = "working"
    SEARCHING = "searching"
    DESIGNING = "designing"
    UPDATING_CART = "updating_cart"
    CHECKING_STORE = "checking_store"
    WRITING = "writing"


_TOOL_PHASES = {
    "search_products": Phase.SEARCHING,
    "more_results": Phase.SEARCHING,
    "leave_out_product": Phase.SEARCHING,
    "more_like_this": Phase.SEARCHING,
    "what_goes_with": Phase.SEARCHING,
    "compare_products": Phase.SEARCHING,
    "product_details": Phase.SEARCHING,
    "store_shelf": Phase.DESIGNING,
    "measure_fit": Phase.DESIGNING,
    "record_design_notes": Phase.DESIGNING,
    "design_room": Phase.DESIGNING,
    "revise_room_piece": Phase.DESIGNING,
    "room_piece_options": Phase.DESIGNING,
    "swap_room_piece": Phase.DESIGNING,
    "remove_room_piece": Phase.DESIGNING,
    "add_to_room": Phase.DESIGNING,
    "add_to_cart": Phase.UPDATING_CART,
    "remove_from_cart": Phase.UPDATING_CART,
    "add_room_to_cart": Phase.UPDATING_CART,
    "store_policy": Phase.CHECKING_STORE,
    "contact_store": Phase.CHECKING_STORE,
}


def progress_of(event: TraceEvent) -> dict[str, str] | None:
    """The public form of one trace event, or None when it is not news."""
    if event.kind == "handoff":
        return {"agent": event.role, "phase": Phase.WORKING.value}
    if event.kind == "model" and event.role == "supervisor":
        return {"agent": event.role, "phase": Phase.ROUTING.value}
    if event.kind == "tool":
        return {"agent": event.role, "phase": _TOOL_PHASES.get(event.name, Phase.WORKING).value}
    return None
