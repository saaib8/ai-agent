"""Building a search from one product the customer pointed at.

Structural, not semantic. "Something similar to this" becomes a search for the
same kind of product, leaning towards the same colour and style - built
entirely from reviewed catalog facts about the reference. No embedding is
consulted and no adjective is invented (CLAUDE.md 31).

What the seed deliberately does not use: the product's name, its price, its
dimensions, its material. Each would narrow the search on something the
customer never said, and a name would smuggle a specific product into a search
for alternatives to it.

Colour and style are **preferences**. Filtering on them would discard products
the customer never excluded, which is the opposite of what "similar" asks for
(CLAUDE.md 12.4).

Pure: it takes an already-hydrated, already-scoped product and returns a
search. Executing it belongs to the pipeline.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.schemas.discovery import ProductSearchRequest, SeatingCapacityConstraint
from app.schemas.product import ProductCandidate
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    ResolvedSearch,
    SemanticPreference,
)
from app.schemas.resolution import (
    SimilarSearchFailureReason,
    SimilarSearchOutcome,
    SimilarSearchSeed,
    SimilarSearchUnavailable,
)
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.registry import CommerceTaxonomy

logger = get_logger(__name__)

# The customer asked for something *like* this one, not for that exact seat
# count. Recording it as approximate lets relaxation widen by a seat when the
# exact pool is thin, which is the point of asking for alternatives.
_CAPACITY_STRENGTH = ConstraintStrength.APPROXIMATE

_PREFERENCE_STRENGTH = ConstraintStrength.PREFERRED


class SimilarSearchBuilder:
    """A verified product -> a new-task search for others like it."""

    def __init__(
        self, taxonomy: CommerceTaxonomy, attributes: CatalogAttributes
    ) -> None:
        self._taxonomy = taxonomy
        self._attributes = attributes

    def build(self, product: ProductCandidate) -> SimilarSearchOutcome:
        category = product.commerce.category
        subcategory = product.commerce.subcategory
        if category is None:
            # No reviewed classification, so there is nothing to search for.
            # Deriving one from the name is exactly what this service must not
            # do (CLAUDE.md 6.1).
            return SimilarSearchUnavailable(
                reason=SimilarSearchFailureReason.NO_COMMERCE_CATEGORY
            )
        if not self._taxonomy.is_category(category):
            return SimilarSearchUnavailable(
                reason=SimilarSearchFailureReason.UNAPPROVED_COMMERCE_CATEGORY
            )
        if subcategory is not None and not self._taxonomy.is_pair(
            category, subcategory
        ):
            # A stale classification must not become a search filter.
            return SimilarSearchUnavailable(
                reason=SimilarSearchFailureReason.UNAPPROVED_COMMERCE_SUBCATEGORY
            )

        capacity = product.commerce.seating_capacity
        request = ProductSearchRequest(
            commerce_category=category,
            commerce_subcategory=subcategory,
            seating_capacity=(
                SeatingCapacityConstraint.exactly(capacity)
                if capacity is not None
                else None
            ),
            # The one product this search must not return.
            exclude_product_ids=(product.product_id,),
        )
        semantics = ConstraintSemantics(
            subcategory=ConstraintStrength.LOCKED if subcategory else None,
            seating_min=_CAPACITY_STRENGTH if capacity is not None else None,
            seating_max=_CAPACITY_STRENGTH if capacity is not None else None,
        )
        preferences = self._preferences(product)
        logger.info(
            "similar_search_seeded",
            commerce_category=category,
            commerce_subcategory=subcategory,
            seating_capacity_known=capacity is not None,
            preference_count=len(preferences),
        )
        return SimilarSearchSeed(
            resolved=ResolvedSearch(
                request=request,
                semantics=semantics,
                semantic_preferences=preferences,
                # Nothing fuzzy was said, so nothing fuzzy is invented.
                semantic_text=None,
            ),
            reference_product_id=product.product_id,
        )

    def _preferences(
        self, product: ProductCandidate
    ) -> tuple[SemanticPreference, ...]:
        """The reference's own colour and styles, where the registry knows them.

        An unapproved token is skipped rather than replaced: "close enough" is
        how a search starts answering a question nobody asked. One unusable
        preference does not cost the whole search - it only costs that lean.
        """
        values: list[tuple[AttributeFamily, str]] = []
        if product.main_color is not None:
            values.append((AttributeFamily.COLOR, product.main_color))
        values.extend((AttributeFamily.STYLE, style) for style in product.styles)

        return tuple(
            SemanticPreference(
                family=family,
                raw_value=value,
                canonical_value=value,
                strength=_PREFERENCE_STRENGTH,
            )
            for family, value in values
            if self._attributes.is_value(family, value)
        )
