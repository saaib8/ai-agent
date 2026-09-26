"""The questions asked before a room is designed (CLAUDE.md 10.1).

Before a living room or a bedroom is built, the customer is asked what is still
missing - one question per turn, each at most once: the budget, which pieces
they want (chips of what the store really stocks), how many people will sit
(living room) and the colours they like. Built by the application from the room
registry, the session and the live catalog; the reply only words it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.design import MAX_REGULAR_SEATING_COUNT
from app.taxonomy.rooms import PieceTier


class RoomQuestionKind(StrEnum):
    """What a room question asks, in the order they are asked."""

    BUDGET = "budget"
    PIECES = "pieces"
    SEATS = "seats"
    COLOUR = "colour"


ROOM_QUESTION_ORDER: tuple[RoomQuestionKind, ...] = tuple(RoomQuestionKind)
"""Budget first: every other choice depends on it (CLAUDE.md 10.1)."""


class RoomPieceOffer(BaseModel):
    """One chip: a piece the store stocks, and whether it starts selected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=40)
    tier: PieceTier
    selected: bool


class RoomQuestion(BaseModel):
    """This turn's one question about the room, and the chips for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_kind: str = Field(min_length=1)
    kind: RoomQuestionKind

    earlier_seat_count: int | None = Field(default=None, ge=1, le=MAX_REGULAR_SEATING_COUNT)
    """A head count they gave for a seating search earlier in the chat. Never
    used for the room unasked: the question confirms it ("is it for the nine
    you mentioned?")."""

    pieces: tuple[RoomPieceOffer, ...] = ()

    @model_validator(mode="after")
    def _fits_the_question(self) -> Self:
        if (self.kind is RoomQuestionKind.PIECES) != bool(self.pieces):
            raise ValueError("pieces are offered exactly when they are asked for")
        if self.earlier_seat_count is not None and self.kind is not RoomQuestionKind.SEATS:
            raise ValueError("an earlier head count is confirmed only when seats are asked")
        return self
