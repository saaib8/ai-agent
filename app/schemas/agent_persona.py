"""Which assistant a reply is shown as coming from.

A label, not an agent: one assistant answers every turn, with one set of
prompts and services behind it (CLAUDE.md 1, 17). The client shows the reply
under the name this carries.
"""

from __future__ import annotations

from enum import StrEnum


class AgentPersona(StrEnum):
    SARA = "sara"
    """Shopping: the session's own voice - the greeting (the first reply
    introduces itself as Sara), product discovery, Furniture Finder, room
    visualisation (every render), the cart, comparisons, everything that is not
    sales or design."""

    SALMAN = "salman"
    """Sales: cross-sell (what goes with a pick), up-sell, and the money side
    of a room - a budget stretch, cheaper pieces, finishing-touch add-ons."""

    NORA = "nora"
    """Interior design: design answers, and the room being designed - its
    questions and package."""
