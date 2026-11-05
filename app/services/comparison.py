"""Comparing products on verified facts.

The application computes the difference; a later layer explains what it means.
A model that computed the diff itself would be asserting facts, and "these two
are the same width" is exactly the kind of claim that sounds harmless and is
checkable (CLAUDE.md 3.3).

Three rules carry the design.

**Unknown is not equal.** Two products that both record no seating capacity are
not "the same"; nobody has established anything about either. The row contract
computes its own status from the cells, so claiming otherwise is
unrepresentable.

**Currency travels with a price.** SAR 100 and USD 100 are different values,
and nothing here converts between them.

**Measurements are compared by role, resolved per product.** A sofa's
along-wall span lives in `length` and a bed's planar axes are unreliable, so
comparing columns because both hold a number would state a difference that does
not exist (CLAUDE.md 15.1).

**Every detail that applies is shown, filled or not.** A rating, a warranty or
a size the store left empty is a row with gaps, never a missing row: the gap
is itself worth seeing beside a product that lists it. Only a detail the kinds
compared do not have is left out - seats on a coffee maker, a depth on a rug.

There is no winner. No score, no recommendation, no "better value".
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.core.config import DEFAULT_DISCOUNT_MAX_SHARE, CustomerAgentSettings
from app.core.logging import get_logger
from app.repositories.products import ProductRepository
from app.schemas.comparison import (
    MIN_COMPARED_PRODUCTS,
    ComparisonCell,
    ComparisonField,
    ComparisonRow,
    ComparisonStatus,
    ProductComparisonResult,
)
from app.schemas.product import ProductCandidate
from app.schemas.resolution import (
    ComparisonFailureReason,
    ComparisonOutcome,
    ComparisonUnavailable,
)
from app.schemas.retailer import RetailerContext
from app.services.discovery import to_candidate
from app.services.grounding_builder import to_grounded_product
from app.services.product_size import lone_floor_side, measurement
from app.taxonomy.dimensions import DimensionRole, DimensionSemantics, FloorSide
from app.taxonomy.seating import SeatingSemantics

logger = get_logger(__name__)

_DIMENSION_FIELDS: dict[ComparisonField, DimensionRole] = {
    ComparisonField.LENGTH: DimensionRole.LENGTH,
    ComparisonField.OVERALL_WIDTH: DimensionRole.OVERALL_WIDTH,
    ComparisonField.DEPTH: DimensionRole.DEPTH,
    ComparisonField.HEIGHT: DimensionRole.HEIGHT,
}

_UNKNOWN = ComparisonCell(known=False)
ONE_SIDE = " (one side listed)"
"""Marks a lone floor size in the English value the agent reads."""


class ProductComparisonService:
    """Resolved product ids -> a typed factual diff, or a typed refusal."""

    def __init__(
        self,
        repository: ProductRepository,
        dimensions: DimensionSemantics,
        seating: SeatingSemantics,
        settings: CustomerAgentSettings,
        *,
        discount_max_share: float = DEFAULT_DISCOUNT_MAX_SHARE,
    ) -> None:
        self._repository = repository
        self._dimension_semantics = dimensions
        self._seating = seating
        self._settings = settings
        self._discount_max_share = discount_max_share

    async def compare(
        self, product_ids: Sequence[int], context: RetailerContext
    ) -> ComparisonOutcome:
        """Compare products the caller has already resolved, in their order."""
        maximum = self._settings.comparison_max_products
        requested = len(product_ids)
        refusal = _count_refusal(product_ids, maximum)
        if refusal is not None:
            return refusal

        rows = await self._repository.get_by_ids(list(product_ids), context)
        if len(rows) != requested:
            # A partial comparison is not a comparison, and substituting
            # another product would answer a different question.
            logger.warning(
                "comparison_product_unavailable",
                store_id=context.store_id,
                requested_count=requested,
                readable_count=len(rows),
            )
            return ComparisonUnavailable(
                reason=ComparisonFailureReason.PRODUCT_UNAVAILABLE,
                requested_count=requested,
                allowed_maximum=maximum,
            )

        share = self._discount_max_share
        by_id = {row.id: to_candidate(row, discount_max_share=share) for row in rows}
        # The customer's order: "the first two" is how they referred to them.
        products = [by_id[product_id] for product_id in product_ids]
        return ProductComparisonResult(
            products=tuple(
                to_grounded_product(
                    product,
                    grounding_ref=position,
                    # Neither is known here: no search returned these, and
                    # they hold no position in this turn's result list.
                    presented_ordinal=None,
                    relaxation_depth=None,
                )
                for position, product in enumerate(products, start=1)
            ),
            rows=tuple(
                self._row(field, products)
                for field in ComparisonField
                if self._applies(field, products)
            ),
        )

    def _applies(self, field: ComparisonField, products: Sequence[ProductCandidate]) -> bool:
        """Whether some compared kind has this detail at all. A detail it has
        but the store left empty still applies - that row shows the gap."""
        if any(self._cell(field, product).known for product in products):
            return True
        kinds = [product.commerce.subcategory for product in products]
        if field is ComparisonField.SEATING_CAPACITY:
            return any(
                self._seating.seats_one(kind) or self._seating.seats_several(kind) for kind in kinds
            )
        if field in _DIMENSION_FIELDS:
            role = _DIMENSION_FIELDS[field]
            return any(role in self._dimension_semantics.shown_by(kind) for kind in kinds)
        return True

    # ── one field across the products ───────────────────────────────────────

    def _row(self, field: ComparisonField, products: Sequence[ProductCandidate]) -> ComparisonRow:
        cells = tuple(self._cell(field, product) for product in products)
        return ComparisonRow(field=field, cells=cells, status=_status(cells))

    def _cell(self, field: ComparisonField, product: ProductCandidate) -> ComparisonCell:
        if field in _DIMENSION_FIELDS:
            return self._measurement(_DIMENSION_FIELDS[field], product)
        return _catalog_value(field, product)

    def _measurement(self, role: DimensionRole, product: ProductCandidate) -> ComparisonCell:
        """Only where the registry maps this role for this product's type.

        A role it does not map is unknown rather than approximated by whichever
        column happens to hold a number.
        """
        by = self._dimension_semantics.shown_by(product.commerce.subcategory).get(role)
        if by is None:
            return _UNKNOWN
        value = measurement(product.dimensions, by)
        if value is None and by is FloorSide.LONGER:
            # Only one floor size recorded: shown in the width row as listed,
            # never claimed as the longer side - a size on the card is never
            # missing from the table.
            lone = lone_floor_side(product.dimensions)
            if lone is not None:
                return ComparisonCell(known=True, value=f"{lone} cm{ONE_SIDE}", one_side=True)
        return _optional(None if value is None else f"{value} cm")


def _count_refusal(product_ids: Sequence[int], maximum: int) -> ComparisonUnavailable | None:
    requested = len(product_ids)
    reason: ComparisonFailureReason | None = None
    if requested < MIN_COMPARED_PRODUCTS:
        reason = ComparisonFailureReason.TOO_FEW_PRODUCTS
    elif requested > maximum:
        # Refused rather than truncated: dropping the last one silently would
        # compare a different set from the one asked about.
        reason = ComparisonFailureReason.TOO_MANY_PRODUCTS
    elif len(set(product_ids)) != requested:
        # Two selectors landed on one product. Comparing it with itself
        # answers nothing, and continuing with fewer answers another question.
        reason = ComparisonFailureReason.DUPLICATE_PRODUCT
    if reason is None:
        return None
    return ComparisonUnavailable(reason=reason, requested_count=requested, allowed_maximum=maximum)


def _catalog_value(field: ComparisonField, product: ProductCandidate) -> ComparisonCell:
    match field:
        case ComparisonField.PRICE:
            # The currency is part of the value: SAR 100 is not USD 100, and
            # nothing here converts between them.
            return _known(f"{product.price_amount} {product.price_unit}")
        case ComparisonField.COMMERCE_SUBCATEGORY:
            return _optional(product.commerce.subcategory)
        case ComparisonField.SEATING_CAPACITY:
            capacity = product.commerce.seating_capacity
            return _optional(None if capacity is None else str(capacity))
        case ComparisonField.MAIN_COLOR:
            return _optional(product.main_color)
        case ComparisonField.STYLES:
            # Sorted, so ("Modern", "Zen") and ("Zen", "Modern") are the same
            # set of styles rather than a difference in data-entry order.
            return _optional(", ".join(sorted(product.styles)) or None)
        case ComparisonField.MAIN_MATERIAL:
            return _optional(product.listing.material)
        case ComparisonField.RATING:
            listing = product.listing
            if listing.rating is None:
                return _UNKNOWN
            if listing.review_count is None:
                return _known(f"rated {plain(listing.rating)}")
            reviews = "review" if listing.review_count == 1 else "reviews"
            return _known(f"{plain(listing.rating)} from {listing.review_count} {reviews}")
        case ComparisonField.WEIGHT:
            weight = product.listing.weight_kg
            return _optional(None if weight is None else f"{plain(weight)} kg")
        case ComparisonField.WARRANTY:
            months = product.listing.warranty_months
            return _optional(f"{months} month{'' if months == 1 else 's'}" if months else None)
        case ComparisonField.DISCOUNT:
            # Prices are complete, so no reduction is a known value, not a gap.
            found = product.discount
            if found is None:
                return _known("none")
            was = f"{plain(found.old_price)} {product.price_unit}"
            return _known(f"{found.percent}% off, was {was}")
        case _:
            return _UNKNOWN


def plain(value: Decimal) -> str:
    """A figure as a person writes it: 4.5, 42.5 - never 4.50 or 4.5E+0."""
    return format(value.normalize(), "f")


def _known(value: str) -> ComparisonCell:
    return ComparisonCell(known=True, value=value)


def _optional(value: str | None) -> ComparisonCell:
    return _UNKNOWN if value is None else _known(value)


def _status(cells: Sequence[ComparisonCell]) -> ComparisonStatus:
    """Unknown wins. Two absences are not an agreement."""
    if any(not cell.known for cell in cells):
        return ComparisonStatus.UNKNOWN
    if len({cell.value for cell in cells}) == 1:
        return ComparisonStatus.SAME
    return ComparisonStatus.DIFFERENT
