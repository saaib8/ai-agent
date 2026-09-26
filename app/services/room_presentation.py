"""A room question's pieces as chips (CLAUDE.md 10.3).

Built from the question the application decided to ask - the room registry
filtered by what the store stocks - never from a model's words.
"""

from __future__ import annotations

from app.schemas.chat import PieceChoice, PiecePicker, ReplyChoice
from app.schemas.room_opener import RoomQuestion
from app.taxonomy.rooms import PieceTier

CHOOSE_FOR_ME = ReplyChoice(label="Choose for me", value="Choose the pieces for me")


def piece_picker(question: RoomQuestion) -> PiecePicker | None:
    """The chips for a pieces question; nothing for any other question."""
    if not question.pieces:
        return None
    return PiecePicker(
        pieces=tuple(
            PieceChoice(
                label=piece.label,
                selected=piece.selected,
                essential=piece.tier is PieceTier.ESSENTIAL,
            )
            for piece in question.pieces
        ),
        submit_label="Design my room",
        choose_for_me=CHOOSE_FOR_ME,
    )
