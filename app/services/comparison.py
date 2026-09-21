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

There is no winner. No score, no recommendation, no "better value".
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.core.config import CustomerAgentSettings
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
from app.schemas.dimensions import NormalisedDimensions
from app.schemas.product import ProductCandidate
from app.schemas.resolution import (
    ComparisonFailureReason,
    ComparisonOutcome,
    ComparisonUnavailable,
)
from app.schemas.retailer import RetailerContext
from app.services.discovery import to_candidate
from app.services.grounding_builder import to_grounded_product
from app.taxonomy.dimensions import DimensionRole, DimensionSemantics, SourceAxis

logger = get_logger(__name__)

_DIMENSION_FIELDS: dict[ComparisonField, DimensionRole] = {
    ComparisonField.LENGTH: DimensionRole.LENGTH,
    ComparisonField.OVERALL_WIDTH: DimensionRole.OVERALL_WIDTH,
    ComparisonField.DEPTH: DimensionRole.DEPTH,
    ComparisonField.HEIGHT: DimensionRole.HEIGHT,
}

_AXIS_VALUES: dict[SourceAxis, str] = {
    SourceAxis.LENGTH: "length_cm",
    SourceAxis.WIDTH: "width_cm",
    SourceAxis.HEIGHT: "height_cm",
}

_UNKNOWN = ComparisonCell(known=False)


class ProductComparisonService:
    """Resolved product ids -> a typed factual diff, or a typed refusal."""

    def __init__(
        self,
        repository: ProductRepository,
        dimensions: DimensionSemantics,
        settings: CustomerAgentSettings,
    ) -> None:
        self._repository = repository
        self._dimension_semantics = dimensions
        self._settings = settings

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

        by_id = {row.id: to_candidate(row) for row in rows}
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
            rows=tuple(self._row(field, products) for field in ComparisonField),
        )

    # ── one field across the products ───────────────────────────────────────

    def _row(
        self, field: ComparisonField, products: Sequence[ProductCandidate]
    ) -> ComparisonRow:
        cells = tuple(self._cell(field, product) for product in products)
        return ComparisonRow(field=field, cells=cells, status=_status(cells))

    def _cell(
        self, field: ComparisonField, product: ProductCandidate
    ) -> ComparisonCell:
        if field in _DIMENSION_FIELDS:
            return self._measurement(_DIMENSION_FIELDS[field], product)
        return _catalog_value(field, product)

    def _measurement(
        self, role: DimensionRole, product: ProductCandidate
    ) -> ComparisonCell:
        """Only where the registry maps this role for this product's type.

        A role it does not map is unknown rather than approximated by whichever
        column happens to hold a number.
        """
        axis = self._dimension_semantics.source_axis(
            product.commerce.subcategory, role
        )
        if axis is None:
            return _UNKNOWN
        return _centimetres(product.dimensions, axis)


def _count_refusal(
    product_ids: Sequence[int], maximum: int
) -> ComparisonUnavailable | None:
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
    return ComparisonUnavailable(
        reason=reason, requested_count=requested, allowed_maximum=maximum
    )


def _catalog_value(
    field: ComparisonField, product: ProductCandidate
) -> ComparisonCell:
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
        case _:
            return _UNKNOWN


def _centimetres(dimensions: NormalisedDimensions, axis: SourceAxis) -> ComparisonCell:
    if not dimensions.is_usable:
        return _UNKNOWN
    value: Decimal | None = getattr(dimensions, _AXIS_VALUES[axis])
    return _optional(None if value is None else f"{value} cm")


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
