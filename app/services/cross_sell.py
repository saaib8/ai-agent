"""Searches for the pieces that go with a product the customer asked about.

"These nightstands go with that bed" is a search for nightstands, leaning
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

from collections.abc import Sequence
from typing import Final

from app.core.logging import get_logger
from app.schemas.chat import ReplyChoice
from app.schemas.discovery import ProductSearchRequest
from app.schemas.product import ProductCandidate
from app.schemas.product_action import CompanionAction, CompanionOffer
from app.schemas.query import ConstraintSemantics, ConstraintStrength, ResolvedSearch
from app.services.similar_search import leanings_of
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.complements import Companion

logger = get_logger(__name__)

MAX_COMPANION_CHIPS: Final[int] = 4
"""A row of chips, not a menu. The pairings are in design order, so the ones
left off are the least worth offering."""


class CompanionSearchBuilder:
    """A verified product and one of its companion types -> a search."""

    def __init__(self, attributes: CatalogAttributes) -> None:
        self._attributes = attributes

    def build(self, anchor: ProductCandidate, companion: Companion) -> ResolvedSearch:
        """Products of the companion type, the anchor's styles first.

        The type is locked: a customer offered nightstands has not been offered
        whatever else a relaxation could reach. Nothing fuzzy was said, so no
        semantic text is invented.
        """
        preferences = tuple(
            lean
            for lean in leanings_of(anchor, self._attributes)
            if lean.family is AttributeFamily.STYLE
        )
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


def companion_choices(companions: Sequence[CompanionOffer]) -> tuple[ReplyChoice, ...]:
    """The other companions of the product in focus, as chips.

    Each chip carries the action it performs, so tapping "Matching rugs" runs
    that search for the focused product directly; `value` is the words
    recorded for the customer's side of the turn.
    """
    return tuple(
        ReplyChoice(
            label=f"Matching {offer.label}",
            value=f"Show me matching {offer.label}",
            product_action=CompanionAction(category=offer.category, subcategory=offer.subcategory),
        )
        for offer in companions[:MAX_COMPANION_CHIPS]
    )
