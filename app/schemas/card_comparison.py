"""Comparing two cards the customer checked, in a pop-up.

Transport contracts. A card is named by the result list it is on and its
position there, never by a product id: both are resolved server-side against
the session, exactly as a tick is (CLAUDE.md 20.2). The comparison is read
only - nothing is added to the conversation or the session.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.comparison import ProductComparisonResult
from app.schemas.furniture_finder import SESSION_ID_PATTERN


class CardRef(BaseModel):
    """A card on screen: the result list it is on, and its position there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    list_revision: int = Field(ge=1)
    ordinal: int = Field(ge=1)


class CardComparisonRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, pattern=SESSION_ID_PATTERN)
    store_id: int = Field(ge=1)
    cards: tuple[CardRef, CardRef]
    """Exactly two: the product decision for comparison."""

    @model_validator(mode="after")
    def _two_different_cards(self) -> Self:
        if self.cards[0] == self.cards[1]:
            raise ValueError("a comparison needs two different cards")
        return self


class CardComparisonResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    comparison: ProductComparisonResult
    message: str
    """The assistant's short take on what differs - worded like any
    comparison reply, from the same verified table."""


class CompareGroupsResponse(BaseModel):
    """Which types compare with which, for a client to disable a checkbox
    before asking. A type absent from `groups` compares only with itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    groups: dict[str, str] = Field(default_factory=dict)
