"""What goes with a product the customer picked: the offer, and its searches.

Picking a bed is offered what goes well with it - nightstands, wardrobes,
rugs - as chips; nothing is searched until they tap one. "Nightstands to go
with that bed" is then a search for nightstands, leaning
towards the bed's own styles - built from reviewed catalog facts about the
bed, exactly as a similar-product search is (CLAUDE.md 6.1). Which
types go with which comes from the reviewed pairings, never from a model; which
products fill them is Product Discovery's, through the ordinary pipeline, so
there is no second search implementation (CLAUDE.md 27).

Style only, and as a **preference**: matching pieces rank first and nothing
is filtered out (CLAUDE.md 12.4). Not colour: pieces go with a bed by sharing
its look, not its colour - a sage bed is not asking for sage nightstands - and
leaning on it ranked same-coloured pieces first and had the reply apologise
that none was sage. No budget carries over - the customer's last
budget belonged to their last search, not to a nightstand they never asked
about.

Pure: it takes an already-hydrated, already-scoped product and returns a
search. Executing it belongs to the pipeline.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from app.core.logging import get_logger
from app.schemas.agent_state import AgentStateV1, CrossSellKindPlan, CrossSellPlan
from app.schemas.dimensions import NormalisedDimensions
from app.schemas.discovery import ProductSearchRequest
from app.schemas.product import ProductCandidate
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    RankingLean,
    ResolvedSearch,
    SemanticPreference,
)
from app.services.similar_search import leanings_of
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.complements import Companion

logger = get_logger(__name__)

MAX_COMPANION_CHIPS: Final[int] = 4
"""A row of chips, not a menu. The chips lead with the kinds the store has
most of, so the ones left off are the least stocked."""


class CompanionSearchBuilder:
    """A verified product and one of its companion types -> a search."""

    def __init__(self, attributes: CatalogAttributes) -> None:
        self._attributes = attributes

    def style_of(self, anchor: ProductCandidate) -> tuple[SemanticPreference, ...]:
        """The anchor's own styles, as preferences: what a piece beside it
        leans towards. Its colour is left out - a rug need not match the
        sofa's colour to go with it."""
        return tuple(
            lean
            for lean in leanings_of(anchor, self._attributes)
            if lean.family is AttributeFamily.STYLE
        )

    def build(self, anchor: ProductCandidate, companion: Companion) -> ResolvedSearch:
        """Products of the companion type, the anchor's styles first.

        The type is locked: a customer offered nightstands has not been offered
        whatever else a relaxation could reach. Nothing fuzzy was said, so no
        semantic text is invented.
        """
        preferences = self.style_of(anchor)
        logger.info(
            "companion_search_seeded",
            commerce_category=companion.commerce_category,
            commerce_subcategory=companion.commerce_subcategory,
            preference_count=len(preferences),
        )
        return ResolvedSearch(
            request=ProductSearchRequest(
                commerce_category=companion.commerce_category,
                commerce_subcategory=companion.commerce_subcategory,
            ),
            semantics=ConstraintSemantics(subcategory=ConstraintStrength.LOCKED),
            semantic_preferences=preferences,
            semantic_text=None,
        )


# ── Nora's plan for what goes with a pick (CLAUDE.md 10.3) ──────────────────

SIZE_CLOSE_SHARE: Final[Decimal] = Decimal("0.25")
"""How near a card's longer side must be to the planned one to count as
"near her size" in the plan-vs-shown log. Measurement only - nothing is
filtered or ranked by it."""


def live_plan(state: AgentStateV1) -> CrossSellPlan | None:
    """Nora's plan while the piece it was made for is still in the cart; once
    that piece leaves, nothing leans to it."""
    plan = state.cross_sell_plan
    if plan is None:
        return None
    if plan.anchor_product_id not in state.product_interaction.selected_product_ids:
        return None
    return plan


def longer_side(dimensions: NormalisedDimensions) -> Decimal | None:
    return max(
        (v for v in (dimensions.length_cm, dimensions.width_cm) if v is not None), default=None
    )


def plan_lean(
    anchor: ProductCandidate, plan: CrossSellKindPlan, *, with_size: bool = True
) -> RankingLean | None:
    """One kind's plan as a ranking lean: her colours and styles, what she
    would push down, and a longer side near her share of the pick's - worked
    out from the pick's real measurements, only where they exist. It orders
    and never filters. None when the plan leans nothing."""
    longer = longer_side(anchor.dimensions) if with_size else None
    target = (
        (longer * Decimal(str(plan.size_ratio))).quantize(Decimal(1))
        if longer is not None and plan.size_ratio is not None
        else None
    )
    lean = RankingLean(
        colours=plan.colours,
        styles=plan.styles,
        avoid_colours=plan.avoid_colours,
        avoid_styles=plan.avoid_styles,
        size_target_cm=target,
    )
    return None if lean == RankingLean() else lean


def log_plan_vs_shown(
    where: str, kind: str, lean: RankingLean | None, colour: str | None, size: Decimal | None
) -> None:
    """One structured line per card a plan ordered: whether it came in one of
    her colours and near her size. The match rate is read from these lines;
    no product, price or customer word is logged."""
    if lean is None:
        return
    colour_match = (
        None
        if not lean.colours
        else colour is not None and colour.casefold() in {c.casefold() for c in lean.colours}
    )
    target = lean.size_target_cm
    size_close = (
        None if target is None or size is None else abs(size - target) <= target * SIZE_CLOSE_SHARE
    )
    logger.info(
        "cross_sell_plan_vs_shown",
        where=where,
        kind=kind,
        colour_planned=bool(lean.colours),
        colour_match=colour_match,
        size_planned=target is not None,
        size_close=size_close,
    )


def plain_shopper(state: AgentStateV1) -> bool:
    """No taste, likes or room of their own on record: a plan Nora makes for
    such a shopper is about the product alone, so it may be kept for the next
    one, and a kept plan may be shown to them. Anyone with taste of their own
    gets a plan of their own (CLAUDE.md 10.3)."""
    taste = state.customer_preferences
    return (
        not taste.semantic_preferences
        and not taste.avoid_colours
        and not taste.avoid_styles
        and not state.product_interaction.liked_product_ids
        and state.room_project is None
    )
