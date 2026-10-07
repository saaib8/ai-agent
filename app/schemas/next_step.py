"""The next step a reply offers, so a conversation never dead-ends (CLAUDE.md 10.2).

Decided by the application from what the turn produced and what the customer
picked; the reply words it, and its chips answer it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.schemas.reply_choice import ReplyChoice


class NextStepKind(StrEnum):
    """Which next step a reply offers."""

    START = "start"
    """Nothing on screen and nothing picked: a piece, or a whole room?"""

    CHOOSE_PIECE = "choose_piece"
    """They want a piece: answer the product-type question with stocked types."""

    CHOOSE_ROOM = "choose_room"
    """They want a room designed: choose from the supported room templates."""

    AFTER_PICKS = "after_picks"
    """They have picks: what goes with them, or a room around them."""

    ROOM_AROUND_PICKS = "room_around_picks"
    """Picks with nothing reviewed to go with them: a room around them, or more."""

    AFTER_DETAIL = "after_detail"
    """One product shown in detail: pick it, or see what goes with it."""

    AFTER_COMPARISON = "after_comparison"
    """Two products compared: which way they lean."""

    AFTER_ROOM = "after_room"
    """A room was put together: change a piece, or add to it."""

    KEEP_BROWSING = "keep_browsing"
    """Products on screen and nothing asked about them yet."""


QUESTIONS: dict[NextStepKind, str] = {
    NextStepKind.CHOOSE_PIECE: "What type of furniture are you looking for?",
    NextStepKind.CHOOSE_ROOM: "Which room would you like to design?",
    NextStepKind.START: (
        "Are you looking for a particular piece, or would you like help designing a whole room?"
    ),
    NextStepKind.AFTER_PICKS: (
        "Shall I find what goes with your picks, or design a room around them?"
    ),
    NextStepKind.ROOM_AROUND_PICKS: (
        "Shall I design a room around your picks, or would you like to keep browsing?"
    ),
    NextStepKind.AFTER_DETAIL: "Would you like to add it to your picks, or see what goes with it?",
    NextStepKind.AFTER_COMPARISON: "Which one are you leaning towards?",
    NextStepKind.AFTER_ROOM: "Would you like to swap any piece, or add a finishing touch?",
    NextStepKind.KEEP_BROWSING: "Would you like to narrow these down, or see more options?",
}
"""The fixed question for each next step - digit-free, so the number check never
refuses it - used when a reply ends without one. The reply model words its own
version when it can; this is the floor, never the ceiling."""

ANY_NEXT_STEP = "What would you like to do next?"
"""The last resort, for a turn that asks its own question but whose reply came
back without one."""


class NextStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: NextStepKind
    chips: tuple[ReplyChoice, ...] = ()

    @property
    def question(self) -> str:
        return QUESTIONS[self.kind]
