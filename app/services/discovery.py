"""Structured product discovery.

Orchestration only: validate the request against the approved taxonomy, resolve
its measurements to stored axes, and hand it to the repository.

Two entry points, differing in exactly one thing - whether the result is
bounded - and the difference is deliberate.

``search`` answers a request with a page of candidates, bounded by the
configured candidate limits. ``eligible_pool`` answers with **every** eligible
product and takes no bound at all, because the conversational path ranks the
complete pool: reranking the first fifty of a hundred and seventy-three would
leave the best match unreachable, and a limit applied here would decide that
silently (CLAUDE.md 16.1). Presentation is chosen after ranking, not before it.

Both validate identically, so a request refused by one is refused by the other.

It owns no SQL (the repository does), no vocabulary (the taxonomy registry
does), no retailer identification (``RetailerContext`` arrives resolved), and
no language understanding. There is no ranking and no relaxation: an empty
result is returned as an empty result, never widened by dropping a constraint
or falling back to another category (CLAUDE.md 26).
"""

from __future__ import annotations

import time

from app.core.config import DiscoverySettings
from app.core.exceptions import (
    InvalidRequestError,
    UnknownCatalogAttributeError,
    UnknownCommerceCategoryError,
    UnsupportedDimensionRoleError,
)
from app.core.logging import get_logger
from app.repositories.products import ProductRepository
from app.schemas.discovery import (
    AxisConstraint,
    PlanarDimensionConstraint,
    ProductSearchRequest,
    ProductSearchResult,
)
from app.schemas.product import EligibleProduct, ProductCandidate, ProductRow
from app.schemas.retailer import RetailerContext
from app.services.dimensions import normalise_dimensions
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.dimensions import DimensionSemantics, SourceAxis
from app.taxonomy.registry import CommerceTaxonomy

logger = get_logger(__name__)


def to_candidate(row: ProductRow) -> ProductCandidate:
    return ProductCandidate(
        product_id=row.id,
        name_english=row.name_english,
        name_arabic=row.name_arabic,
        price_amount=row.price_amount,
        price_unit=row.price_unit,
        image_url=row.image_url,
        product_url=row.product_url,
        commerce=row.commerce,
        dimensions=normalise_dimensions(row.dimensions),
        main_color=row.main_color,
        styles=row.styles,
    )


class ProductDiscoveryService:
    def __init__(
        self,
        repository: ProductRepository,
        taxonomy: CommerceTaxonomy,
        settings: DiscoverySettings,
        attributes: CatalogAttributes,
        dimensions: DimensionSemantics,
    ) -> None:
        self._repository = repository
        self._taxonomy = taxonomy
        self._settings = settings
        self._attributes = attributes
        self._dimensions = dimensions

    async def search(
        self, request: ProductSearchRequest, context: RetailerContext
    ) -> ProductSearchResult:
        self._validate_taxonomy(request)
        self._validate_attributes(request)
        axis_constraints = self._resolve_dimensions(request)
        planar = self._resolve_planar(request)
        limit = self._resolve_limit(request.limit)

        started = time.perf_counter()
        # One extra row answers "was there more?" without a second count query
        # over a 77k-row table.
        rows = await self._repository.search(
            request,
            context,
            limit=limit + 1,
            axis_constraints=axis_constraints,
            planar=planar,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

        truncated = len(rows) > limit
        candidates = tuple(to_candidate(row) for row in rows[:limit])

        logger.info(
            "product_discovery_completed",
            store_id=context.store_id,
            commerce_category=request.commerce_category,
            commerce_subcategory=request.commerce_subcategory,
            applied_filters=list(request.applied_filters()),
            colors_any_of=list(request.colors_any_of),
            styles_all_of=list(request.styles_all_of),
            dimension_roles=[str(d.role) for d in request.dimensions],
            planar_dimensions=request.planar_dimensions is not None,
            sort=str(request.sort),
            limit=limit,
            candidate_count=len(candidates),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )
        return ProductSearchResult(
            candidates=candidates, limit=limit, truncated=truncated
        )

    async def eligible_pool(
        self, request: ProductSearchRequest, context: RetailerContext
    ) -> tuple[EligibleProduct, ...]:
        """Every eligible product for this request. No limit, ever.

        The conversational path ranks the COMPLETE eligible pool, so this
        deliberately takes no bound and never consults the presentation limits
        `search` resolves: reranking the first fifty of a hundred and
        seventy-three would leave the best match unreachable (CLAUDE.md 16.1).

        Same validation and the same role-to-axis resolution as `search`, so a
        request that would be refused there is refused here too.
        """
        self._validate_taxonomy(request)
        self._validate_attributes(request)
        axis_constraints = self._resolve_dimensions(request)
        planar = self._resolve_planar(request)

        started = time.perf_counter()
        pool = await self._repository.search_eligible_pool(
            request,
            context,
            axis_constraints=axis_constraints,
            planar=planar,
        )
        logger.info(
            "product_eligible_pool_completed",
            store_id=context.store_id,
            commerce_category=request.commerce_category,
            commerce_subcategory=request.commerce_subcategory,
            applied_filters=list(request.applied_filters()),
            eligible_count=len(pool),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return tuple(pool)

    def _validate_taxonomy(self, request: ProductSearchRequest) -> None:
        """Reject unapproved values before any SQL runs (CLAUDE.md 14.3)."""
        if request.commerce_subcategory is not None:
            self._taxonomy.validate_pair(
                request.commerce_category, request.commerce_subcategory
            )
        elif not self._taxonomy.is_category(request.commerce_category):
            raise UnknownCommerceCategoryError(category=request.commerce_category)

    def _validate_attributes(self, request: ProductSearchRequest) -> None:
        """Strict colour and style values must be approved before any SQL runs.

        Membership is checked per family, so a colour can never be accepted as
        a style or the reverse.
        """
        for family, values in (
            (AttributeFamily.COLOR, request.colors_any_of),
            (AttributeFamily.STYLE, request.styles_all_of),
        ):
            for value in values:
                if not self._attributes.is_value(family, value):
                    raise UnknownCatalogAttributeError(family=str(family), value=value)

    def _resolve_dimensions(
        self, request: ProductSearchRequest
    ) -> tuple[AxisConstraint, ...]:
        """Turn customer-facing roles into stored axes, through the registry.

        This is the only place a role becomes a column. A role the registry does
        not support for this subcategory is refused rather than answered with a
        plausible-looking axis.
        """
        resolved: list[AxisConstraint] = []
        for constraint in request.dimensions:
            axis = self._dimensions.source_axis(
                request.commerce_subcategory, constraint.role
            )
            if axis is None:
                raise UnsupportedDimensionRoleError(
                    subcategory=request.commerce_subcategory,
                    role=str(constraint.role),
                    reason=str(
                        self._dimensions.unsupported_reason(
                            request.commerce_subcategory, constraint.role
                        )
                    ),
                )
            resolved.append(
                AxisConstraint(
                    axis=axis,
                    kind=constraint.kind,
                    min_cm=constraint.min_cm,
                    max_cm=constraint.max_cm,
                    target_cm=constraint.target_cm,
                )
            )
        return tuple(resolved)

    def _resolve_planar(
        self, request: ProductSearchRequest
    ) -> tuple[PlanarDimensionConstraint, tuple[SourceAxis, SourceAxis]] | None:
        """Only subcategories the registry marks planar may match a pair."""
        constraint = request.planar_dimensions
        if constraint is None:
            return None
        pair = self._dimensions.planar_pair(request.commerce_subcategory)
        if pair is None:
            raise UnsupportedDimensionRoleError(
                subcategory=request.commerce_subcategory,
                role="planar_pair",
                reason="role_not_defined",
            )
        return constraint, pair.axes

    def _resolve_limit(self, requested: int | None) -> int:
        if requested is None:
            return self._settings.default_candidate_limit
        if requested > self._settings.max_candidate_limit:
            raise InvalidRequestError(
                public_message="That request asks for more results than we return at once.",
                requested_limit=requested,
                max_candidate_limit=self._settings.max_candidate_limit,
            )
        return requested
