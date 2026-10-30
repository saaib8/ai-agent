"""A guest's chat, handed to the account they signed in to.

The platform names the shopper at the front of every conversation id
(`<16 hex>.<the widget's id>`, CLAUDE.md 36). Signing in changes the shopper,
so the widget's chat would be looked for under a new name and found empty: the
room they built, the photos they sent and the cart they filled would stay with
the guest. Once the platform has checked the guest really was that browser, it
asks here for the chat to move under the account's name.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.furniture_finder import SESSION_ID_PATTERN


class ConversationAdoptionRequest(BaseModel):
    """Which chat moves: the guest's name for it, and the account's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    store_id: int = Field(ge=1)
    from_session_id: str = Field(min_length=1, max_length=128, pattern=SESSION_ID_PATTERN)
    to_session_id: str = Field(min_length=1, max_length=128, pattern=SESSION_ID_PATTERN)


class AdoptionOutcome(StrEnum):
    MOVED = "moved"
    """The chat, its room photo and its finder photos now live under the
    account's name, and no longer under the guest's."""

    ALREADY_MOVED = "already_moved"
    """Asked again: the account holds the chat and the guest no longer does."""

    NOTHING_TO_MOVE = "nothing_to_move"
    """The guest has no such chat - it never started, or it expired."""

    TARGET_IN_USE = "target_in_use"
    """The account already has a chat under that name; neither is touched."""


class ConversationAdoption(BaseModel):
    """What the move did. The cart is merged whatever the chat's outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: AdoptionOutcome
    cart_lines_added: int = Field(ge=0)
    """Products from the guest's cart the account's cart did not hold yet."""
