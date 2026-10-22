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

from typing import Final

from app.core.logging import get_logger
from app.schemas.discovery import ProductSearchRequest
from app.schemas.product import ProductCandidate
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
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






