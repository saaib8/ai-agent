"""The add-ons offered after a room is built (CLAUDE.md 10.2, 27).

A good salesperson, having put a room together, does not offer to swap the bed
they just chose for another bed. They point at what would finish the room -
"a full-length mirror would pull this together" - and if the customer takes
it, suggest one more. This module holds the deterministic half of that: which
pieces are worth suggesting, which product to show, and which stored facts
make it right for *this* customer. Searching the catalog and costing the room
are the coordinator's, through Product Discovery and the optimiser, so there
is no second search implementation and no second pricing (CLAUDE.md 27).

**Only what the room does not hold.** The pieces come from the reviewed
`add_ons` list of the room (`room_pieces_v1.yaml`), in its order, less any type
the package already has and any the store does not stock.

**Within the budget first, and past it only out loud.** A product that keeps
the room inside their budget is preferred; failing that, one within the
configured stretch, and then the offer carries exactly how far over it goes.
Nothing is added until they say yes (CLAUDE.md 10.2).

**Never a claim about quality.** The only reasons given are stored facts: it
comes in a colour, or a style, they asked for.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.schemas.agent_state import UpgradeReason
from app.schemas.product import ProductCandidate
from app.schemas.query import SemanticPreference
from app.schemas.retailer import RetailerCatalogCapabilities
from app.services.room_composition import stocked
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.rooms import RoomPiece, RoomTemplate

MAX_ADD_ON_ROUNDS = 2
"""One add-on, and one more if they take it. A third is a salesperson who
does not know when to stop."""


@dataclass(frozen=True, slots=True)
class RoomUpgradePolicy:
    """How far past their budget an add-on may take the room, as a fraction."""

    max_over_budget: Decimal = Decimal(0)


def add_on_pieces(
    template: RoomTemplate,
    present: Collection[str],
    capabilities: RetailerCatalogCapabilities,
) -> list[RoomPiece]:
    """The pieces worth suggesting, most worth it first: the room's reviewed
    add-ons that it does not already hold and the store actually sells."""
    pieces = (template.piece(key) for key in template.add_ons)
    return [
        piece
        for piece in pieces
        if piece is not None
        and piece.commerce_subcategory not in present
        and stocked(piece, capabilities)
    ]


def choose_add_on(
    candidates: Sequence[ProductCandidate],
    quantity: int,
    spare: Decimal | None,
    headroom: Decimal | None,
) -> ProductCandidate | None:
    """The product to suggest: the best-ranked one that keeps the room within
    their budget, else the best-ranked one within the stretch past it.

    `spare` is what the budget has left, `headroom` that plus the stretch;
    None for either means no ceiling was given. Candidates arrive in ranked
    order - their colours and styles first - and that order is kept."""

    def fits(product: ProductCandidate, limit: Decimal | None) -> bool:
        return limit is None or product.price_amount * quantity <= limit

    return next((p for p in candidates if fits(p, spare)), None) or next(
        (p for p in candidates if fits(p, headroom)), None
    )


def add_on_reasons(
    product: ProductCandidate, preferences: Sequence[SemanticPreference]
) -> tuple[UpgradeReason, ...]:
    """Which stored facts make it right for them: their colour, their style."""
    reasons: list[UpgradeReason] = []
    colours = _wanted(preferences, AttributeFamily.COLOR)
    if product.main_color is not None and product.main_color.casefold() in colours:
        reasons.append(UpgradeReason.THEIR_COLOUR)
    styles = _wanted(preferences, AttributeFamily.STYLE)
    if styles & {style.casefold() for style in product.styles}:
        reasons.append(UpgradeReason.THEIR_STYLE)
    return tuple(reasons)


def _wanted(preferences: Sequence[SemanticPreference], family: AttributeFamily) -> set[str]:
    return {
        preference.canonical_value.casefold()
        for preference in preferences
        if preference.family is family and preference.canonical_value
    }
