"""Which assistant a reply is shown as coming from.

A label, not an agent: one assistant answers every turn, with one set of
prompts and services behind it (CLAUDE.md 1, 17). The client shows the reply
under the name this carries.
"""

from __future__ import annotations

from enum import StrEnum


class AgentPersona(StrEnum):
    SARAH = "sarah"
    """Shopping: the session's own voice - product discovery, Furniture Finder,
    room visualisation (every render), picks, comparisons, everything that is
    not sales or design."""

    SALMAN = "salman"
    """Sales: cross-sell (what goes with a pick), up-sell, and the money side
    of a room - a budget stretch, cheaper pieces, finishing-touch add-ons."""

    NORA = "nora"
    """Interior design: the greeting (the reply introduces itself as Nora),
    design answers, and the room being designed - its questions and package."""
