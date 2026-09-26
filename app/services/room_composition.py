"""A room made of the pieces the customer chose (CLAUDE.md 10.3).

Deterministic, and deliberately small: which pieces the store can offer for a
room, what the opening message still has to ask, and the plan's needs for the
pieces chosen. No model, no search - the coordinator runs those.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.schemas.agent_state import RoomProjectState
from app.schemas.design import DesignCategoryNeed, DesignPriority, InteriorDesignResult
from app.schemas.retailer import RetailerCatalogCapabilities
from app.schemas.room_opener import (
    ROOM_QUESTION_ORDER,
    RoomPieceOffer,
    RoomQuestion,
    RoomQuestionKind,
)
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.rooms import PieceTier, RoomPiece, RoomTemplate

PRIORITY_FOR_TIER: dict[PieceTier, DesignPriority] = {
    PieceTier.ESSENTIAL: DesignPriority.REQUIRED,
    PieceTier.RECOMMENDED: DesignPriority.RECOMMENDED,
    PieceTier.OPTIONAL: DesignPriority.OPTIONAL,
}
"""A short budget gives up optional pieces first, then recommended ones, and an
essential piece last (CLAUDE.md 10)."""


def stocked(piece: RoomPiece, capabilities: RetailerCatalogCapabilities) -> bool:
    """Whether the store sells anything this piece can be filled with."""
    return any(capabilities.supports(piece.commerce_category, t) for t in piece.types)


def offered(template: RoomTemplate, capabilities: RetailerCatalogCapabilities) -> list[RoomPiece]:
    """The room's pieces this store can supply, in the template's order."""
    return [piece for piece in template.pieces if stocked(piece, capabilities)]


def default_pieces(template: RoomTemplate) -> tuple[str, ...]:
    """What "choose for me" means: every essential and recommended piece."""
    return tuple(piece.key for piece in template.pieces if piece.tier.starts_selected)


def chosen_keys(
    template: RoomTemplate, keys: Sequence[str] | None, *, default: bool
) -> tuple[str, ...] | None:
    """The pieces a decision named, as this room's keys - or `None` if it named
    none this template knows. A key the registry does not hold is dropped,
    never mapped onto a near one (CLAUDE.md 14.3)."""
    if default:
        return default_pieces(template)
    if keys is None:
        return None
    known = tuple(key for key in dict.fromkeys(keys) if template.piece(key) is not None)
    return known or None


def colour_known(room: RoomProjectState) -> bool:
    return any(p.family is AttributeFamily.COLOR for p in room.design_preferences)


def next_question(
    room: RoomProjectState,
    template: RoomTemplate,
    capabilities: RetailerCatalogCapabilities,
    earlier_seat_count: int | None,
) -> RoomQuestion | None:
    """The one question to ask this turn, or `None` to build the room now.

    In order - budget, pieces, head count (when the room seats people), colour
    - the first that is still unanswered and not yet asked. Each is asked once,
    and none once they said to get on with it or the room already exists.
    """
    if room.questions_done or room.design_needs or room.bundle_items:
        return None
    missing = {
        RoomQuestionKind.BUDGET: room.budget is None,
        RoomQuestionKind.PIECES: room.pieces is None,
        RoomQuestionKind.SEATS: template.asks_seats and room.regular_seating_count is None,
        RoomQuestionKind.COLOUR: not colour_known(room),
    }
    for kind in ROOM_QUESTION_ORDER:
        if not missing[kind] or kind in room.questions_asked:
            continue
        if kind is RoomQuestionKind.PIECES:
            pieces = _offers(template, capabilities)
            if not pieces:
                continue
            return RoomQuestion(room_kind=template.kind, kind=kind, pieces=pieces)
        return RoomQuestion(
            room_kind=template.kind,
            kind=kind,
            earlier_seat_count=earlier_seat_count if kind is RoomQuestionKind.SEATS else None,
        )
    return None


def _offers(
    template: RoomTemplate, capabilities: RetailerCatalogCapabilities
) -> tuple[RoomPieceOffer, ...]:
    return tuple(
        RoomPieceOffer(
            key=piece.key,
            label=piece.label,
            tier=piece.tier,
            selected=piece.tier.starts_selected,
        )
        for piece in offered(template, capabilities)
    )


def composed_needs(
    template: RoomTemplate,
    keys: Sequence[str],
    capabilities: RetailerCatalogCapabilities,
    designed: InteriorDesignResult,
) -> list[DesignCategoryNeed]:
    """One need per chosen piece the store stocks - seating aside, which is
    sized to the head count separately.

    The composition is the customer's; the specialist contributes only how each
    piece should feel, taken from its need of the same type when it proposed
    one (CLAUDE.md 17.2).
    """
    intent = {
        (need.commerce_category, need.commerce_subcategory): need.semantic_intent
        for need in designed.needs
    }
    needs: list[DesignCategoryNeed] = []
    for piece in template.pieces:
        if piece.key not in keys or piece.is_seating or not stocked(piece, capabilities):
            continue
        needs.append(
            DesignCategoryNeed(
                commerce_category=piece.commerce_category,
                commerce_subcategory=piece.commerce_subcategory,
                priority=PRIORITY_FOR_TIER[piece.tier],
                quantity=piece.quantity,
                semantic_intent=intent.get((piece.commerce_category, piece.commerce_subcategory)),
            )
        )
    return needs


def seating_piece(
    template: RoomTemplate, keys: Sequence[str], capabilities: RetailerCatalogCapabilities
) -> RoomPiece | None:
    """The room's seating, when it was chosen and the store can supply it."""
    piece = template.seating
    if piece is None or piece.key not in keys or not stocked(piece, capabilities):
        return None
    return piece
