"""A room made of the pieces the customer chose (CLAUDE.md 10.3).

Deterministic, and deliberately small: which pieces the store can offer for a
room, what the opening message still has to ask, and the plan's needs for the
pieces chosen. No model, no search - the coordinator runs those.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.schemas.design import DesignCategoryNeed, DesignPriority, InteriorDesignResult
from app.schemas.retailer import RetailerCatalogCapabilities
from app.schemas.room_opener import (
    RoomPieceOffer,
)
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










def piece_for(
    template: RoomTemplate, category: str | None, subcategory: str | None
) -> RoomPiece | None:
    """The room piece a product fills, when it belongs in this room at all.

    Seating first - a sofa or an armchair is the room's seating before it is an
    accent chair - then the first piece of its exact type."""
    if category is None or subcategory is None:
        return None
    seating = template.seating
    if (
        seating is not None
        and category == seating.commerce_category
        and subcategory in seating.types
    ):
        return seating
    return next(
        (
            piece
            for piece in template.pieces
            if piece.commerce_category == category and subcategory in piece.types
        ),
        None,
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


def finishing_offers(
    template: RoomTemplate,
    capabilities: RetailerCatalogCapabilities,
    in_room: frozenset[str],
) -> tuple[RoomPieceOffer, ...]:
    """The pieces a finished room could still take.

    Every piece of the room the store stocks and the room does not hold yet, in
    the registry's order. Which to suggest, and in what order, is the agent's
    choice. The room's seating is never offered: it is the room's anchor, not
    a touch.
    """
    return tuple(
        RoomPieceOffer(
            key=piece.key,
            label=piece.label,
            label_ar=piece.label_ar,
            tier=piece.tier,
            selected=False,
        )
        for piece in offered(template, capabilities)
        if piece.key not in in_room and not piece.is_seating
    )


def seating_piece(
    template: RoomTemplate, keys: Sequence[str], capabilities: RetailerCatalogCapabilities
) -> RoomPiece | None:
    """The room's seating, when it was chosen and the store can supply it."""
    piece = template.seating
    if piece is None or piece.key not in keys or not stocked(piece, capabilities):
        return None
    return piece
