"""Runs the customer's exact search, then any widening the policy permits.

The exact request always runs first, whatever the semantics say. Even when
every constraint is approximate, nothing is pre-relaxed: the customer's own
request is a question worth asking before any other.

Widening stops the moment the unique pool reaches the target, so the mildest
widening that suffices is the one that stands. Failing to reach the target is a
valid outcome, not an error - the catalog may simply not hold enough.

Every attempt reads the COMPLETE eligible pool, never a presentation page.
Sufficiency is therefore judged against what the catalog actually holds, and
semantic ranking downstream sees every eligible product (CLAUDE.md 16.1).

No LLM, no SQL, no customer language, no conversation history. This service
consumes the structured meaning M7 produced and calls M6 with it.
"""

from __future__ import annotations

import time
from decimal import Decimal

from app.core.config import RelaxationSettings
from app.core.exceptions import InvalidRequestError, UnsupportedDimensionRoleError
from app.core.logging import get_logger
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PriceConstraint,
    ProductSearchRequest,
    size_words,
)
from app.schemas.product import EligibleProduct
from app.schemas.query import ResolvedSearch
from app.schemas.relaxation import (
    AppliedRelaxation,
    AttributeRelaxationChange,
    ClosestSizeChange,
    ControlledSearchResult,
    RelaxableField,
    RelaxationAttempt,
    RelaxedCandidate,
    SetAsideField,
    SetAsideOption,
    StopReason,
)
from app.schemas.retailer import RetailerContext
from app.services.discovery import ProductDiscoveryService
from app.services.relaxation import RelaxationPlanner
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.dimensions import DimensionRole, FloorSide

logger = get_logger(__name__)


class ControlledRelaxationService:
    def __init__(
        self,
        discovery: ProductDiscoveryService,
        planner: RelaxationPlanner,
        settings: RelaxationSettings,
    ) -> None:
        self._discovery = discovery
        self._planner = planner
        self._settings = settings

    async def search(
        self,
        resolved: ResolvedSearch,
        context: RetailerContext,
        *,
        explain_empty: bool = False,
        closest_sizes: bool = False,
    ) -> ControlledSearchResult:
        """Run the exact request, then widen only as far as policy allows.

        Accepts a :class:`ResolvedSearch` and nothing else. An unsupported
        requirement or a clarification must be settled conversationally first -
        widening a budget to compensate for a colour we never applied would
        compound one unmet requirement with another.

        `explain_empty` is for a search a customer will read about: when even
        the widest permitted search finds nothing, it also counts what each
        requirement set aside alone would find, so the reply can offer a real
        next step. Nothing is set aside for them.

        `closest_sizes` is for a customer's own search: when nothing met their
        sizes, the closest sizes are shown instead of nothing, and recorded so
        the reply says so. Never for a room piece's options, whose sizes are
        the floor the designer kept.
        """
        result = await self._search(resolved, context, closest_sizes=closest_sizes)
        if not explain_empty or result.candidates:
            return result
        return result.model_copy(
            update={"set_aside": await self._set_aside(result.original_request, context)}
        )

    async def _search(
        self, resolved: ResolvedSearch, context: RetailerContext, *, closest_sizes: bool
    ) -> ControlledSearchResult:
        self._require_resolved(resolved)
        original = resolved.request
        target = self._settings.target_candidates
        started = time.perf_counter()

        pool: dict[int, RelaxedCandidate] = {}
        attempts: list[RelaxationAttempt] = []

        if resolved.unmatched_strict:
            return await self._search_unmatchable(resolved, context, pool, attempts, started)

        exact_count = await self._attempt(
            original, context, depth=0, changes=(), pool=pool, attempts=attempts
        )
        if len(pool) >= target:
            return self._finish(
                original, attempts, pool, exact_count, StopReason.EXACT_SUFFICIENT, started
            )

        planned = self._planner.plan(resolved)
        for depth, step in enumerate(planned, start=1):
            await self._attempt(
                step.request,
                context,
                depth=depth,
                changes=step.changes,
                pool=pool,
                attempts=attempts,
            )
            if len(pool) >= target:
                return self._finish(
                    original, attempts, pool, exact_count, StopReason.TARGET_REACHED, started
                )

        if not pool and await self._lift_attributes(context, pool=pool, attempts=attempts):
            return self._finish(
                original, attempts, pool, exact_count, StopReason.ATTRIBUTE_FALLBACK, started
            )
        if (
            not pool
            and closest_sizes
            and await self._closest_sizes(original, context, pool=pool, attempts=attempts)
        ):
            return self._finish(
                original, attempts, pool, exact_count, StopReason.CLOSEST_SIZES, started
            )
        return self._finish(
            original,
            attempts,
            pool,
            exact_count,
            StopReason.POLICY_EXHAUSTED if planned else StopReason.NO_RELAXABLE_CONSTRAINTS,
            started,
        )

    async def _search_unmatchable(
        self,
        resolved: ResolvedSearch,
        context: RetailerContext,
        pool: dict[int, RelaxedCandidate],
        attempts: list[RelaxationAttempt],
        started: float,
    ) -> ControlledSearchResult:
        """A strict colour or style no approved value expresses ("only red").

        Its exact search cannot even be written: no product carries a value
        outside the vocabulary. So the exact attempt is recorded as finding
        nothing - which is what it would find - and the same last resort as
        `_lift_attributes` runs at once: the request without that requirement,
        everything else kept, recorded as a lifted colour or style so the reply
        says plainly that nothing matched. Planned widening still follows if
        even that finds too little.
        """
        original = resolved.request
        attempts.append(RelaxationAttempt(depth=0, request=original, changes=(), eligible_count=0))
        changes = tuple(
            AttributeRelaxationChange(field=field, required=words)
            for field, words in (
                (
                    RelaxableField.COLOR,
                    _words(resolved, AttributeFamily.COLOR),
                ),
                (
                    RelaxableField.STYLE,
                    _words(resolved, AttributeFamily.STYLE),
                ),
            )
            if words
        )
        await self._attempt(
            original, context, depth=1, changes=changes, pool=pool, attempts=attempts
        )
        if len(pool) < self._settings.target_candidates:
            for depth, step in enumerate(self._planner.plan(resolved), start=2):
                await self._attempt(
                    step.request,
                    context,
                    depth=depth,
                    changes=step.changes,
                    pool=pool,
                    attempts=attempts,
                )
                if len(pool) >= self._settings.target_candidates:
                    break
        return self._finish(
            original,
            attempts,
            pool,
            0,
            StopReason.ATTRIBUTE_FALLBACK if pool else StopReason.POLICY_EXHAUSTED,
            started,
        )

    async def _lift_attributes(
        self,
        context: RetailerContext,
        *,
        pool: dict[int, RelaxedCandidate],
        attempts: list[RelaxationAttempt],
    ) -> bool:
        """The last resort for a strict colour or style nothing matched.

        Only when the pool is empty after everything policy allowed: a strict
        requirement that some products meet is never lifted, however few they
        are. Everything else in the widest request tried so far is kept - the
        type, the budget, the size - so the customer sees the closest products
        that still honour the rest of what they asked. Ranking then puts the
        nearest colours or styles first, and the change is recorded so the
        reply says plainly that none matched.
        """
        widest = attempts[-1].request
        changes = tuple(
            AttributeRelaxationChange(field=field, required=values)
            for field, values in (
                (RelaxableField.COLOR, widest.colors_any_of),
                (RelaxableField.STYLE, widest.styles_all_of),
            )
            if values
        )
        if not changes:
            return False
        lifted = widest.model_copy(update={"colors_any_of": (), "styles_all_of": ()})
        found = await self._attempt(
            lifted,
            context,
            depth=attempts[-1].depth + 1,
            changes=changes,
            pool=pool,
            attempts=attempts,
        )
        return found > 0

    async def _closest_sizes(
        self,
        original: ProductSearchRequest,
        context: RetailerContext,
        *,
        pool: dict[int, RelaxedCandidate],
        attempts: list[RelaxationAttempt],
    ) -> bool:
        """The last resort for sizes nothing in the store met.

        Only when the pool is empty after everything else allowed. Everything
        but the sizes stays as the widest search tried - the type, the budget,
        the colour - and the sizes widen by a share of the customer's own
        figures, band by band (`closest_size_steps`), until enough are found;
        then any size, if even the widest band found none. Each band is a
        deeper attempt, so the nearest sizes lead (CLAUDE.md 16.1), and each
        records what they asked, so the reply says none is that size.
        """
        if not original.dimensions and original.planar_dimensions is None:
            return False
        base = attempts[-1].request
        asked = size_words(original)
        for share in (*self._settings.closest_size_steps, None):
            if share is None and pool:
                break
            try:
                await self._attempt(
                    base.model_copy(update=_sizes_within(original, share)),
                    context,
                    depth=attempts[-1].depth + 1,
                    changes=(ClosestSizeChange(asked=asked, within_share=share),),
                    pool=pool,
                    attempts=attempts,
                )
            except UnsupportedDimensionRoleError:
                # A size this kind cannot be searched by is never widened into one.
                return bool(pool)
            if len(pool) >= self._settings.target_candidates:
                break
        return bool(pool)

    async def _set_aside(
        self, request: ProductSearchRequest, context: RetailerContext
    ) -> tuple[SetAsideOption, ...]:
        """What each requirement, set aside on its own, would find.

        Only the customer's own requirements, each from their exact request:
        the product type is never one of them (CLAUDE.md 13.3). Only options
        that find something are returned - "without the colour there are none
        either" is not a way forward.
        """
        variants: list[tuple[SetAsideField, DimensionRole | None, ProductSearchRequest]] = []
        if request.price is not None:
            # Any price, but still only in their currency: a figure in another
            # one could not be compared with their budget at all.
            any_price = PriceConstraint(currency=request.price.currency, min_amount=Decimal(0))
            variants.append(
                (SetAsideField.PRICE, None, request.model_copy(update={"price": any_price}))
            )
        if request.seating_capacity is not None:
            variants.append(
                (SetAsideField.SEATS, None, request.model_copy(update={"seating_capacity": None}))
            )
        if request.weight is not None:
            variants.append(
                (SetAsideField.WEIGHT, None, request.model_copy(update={"weight": None}))
            )
        for constraint in request.dimensions:
            others = tuple(c for c in request.dimensions if c.role is not constraint.role)
            variants.append(
                (
                    SetAsideField.DIMENSION,
                    constraint.role,
                    request.model_copy(update={"dimensions": others}),
                )
            )
        if request.planar_dimensions is not None:
            variants.append(
                (
                    SetAsideField.SIZE_PAIR,
                    None,
                    request.model_copy(update={"planar_dimensions": None}),
                )
            )
        if request.colors_any_of:
            variants.append(
                (SetAsideField.COLOR, None, request.model_copy(update={"colors_any_of": ()}))
            )
        if request.styles_all_of:
            variants.append(
                (SetAsideField.STYLE, None, request.model_copy(update={"styles_all_of": ()}))
            )

        options: list[SetAsideOption] = []
        for field, role, variant in variants:
            pool = await self._discovery.eligible_pool(variant, context)
            if not pool:
                continue
            nearest = (
                _nearest_price(request.price, pool)
                if field is SetAsideField.PRICE and request.price is not None
                else None
            )
            options.append(
                SetAsideOption(
                    field=field,
                    role=role,
                    eligible_count=len(pool),
                    nearest_price=nearest,
                    currency=(
                        request.price.currency if nearest is not None and request.price else None
                    ),
                )
            )
        logger.info(
            "controlled_search_set_aside",
            commerce_subcategory=request.commerce_subcategory,
            checked=len(variants),
            options=[f"{o.field}:{o.role or ''}={o.eligible_count}" for o in options],
        )
        return tuple(options)

    # ── one executed search ─────────────────────────────────────────────────

    async def _attempt(
        self,
        request: ProductSearchRequest,
        context: RetailerContext,
        *,
        depth: int,
        changes: tuple[AppliedRelaxation, ...],
        pool: dict[int, RelaxedCandidate],
        attempts: list[RelaxationAttempt],
    ) -> int:
        # The same immutable context reaches every attempt; this service never
        # sees or chooses a store. The pool is complete: no limit is passed,
        # and `eligible_pool` accepts none.
        eligible = await self._discovery.eligible_pool(request, context)
        for product in eligible:
            # First sighting wins, so a product that also matches a later,
            # wider attempt keeps the lowest depth it earned.
            pool.setdefault(
                product.product_id,
                RelaxedCandidate(product=product, relaxation_depth=depth),
            )
        attempts.append(
            RelaxationAttempt(
                depth=depth,
                request=request,
                changes=changes,
                eligible_count=len(eligible),
            )
        )
        return len(eligible)

    # ── result assembly ─────────────────────────────────────────────────────

    def _finish(
        self,
        original: ProductSearchRequest,
        attempts: list[RelaxationAttempt],
        pool: dict[int, RelaxedCandidate],
        exact_count: int,
        stop_reason: StopReason,
        started: float,
    ) -> ControlledSearchResult:
        target = self._settings.target_candidates
        candidates = tuple(pool.values())
        result = ControlledSearchResult(
            original_request=original,
            final_request=attempts[-1].request,
            candidates=candidates,
            exact_candidate_count=exact_count,
            target_candidates=target,
            target_reached=len(candidates) >= target,
            stop_reason=stop_reason,
            attempts=tuple(attempts),
        )
        logger.info(
            "controlled_search_completed",
            commerce_category=original.commerce_category,
            commerce_subcategory=original.commerce_subcategory,
            exact_candidate_count=exact_count,
            target_candidates=target,
            relaxation_attempt_count=result.relaxation_attempt_count,
            relaxed_fields=sorted(
                {str(change.field) for attempt in attempts for change in attempt.changes}
            ),
            final_candidate_count=len(candidates),
            max_relaxation_depth=max((c.relaxation_depth for c in candidates), default=0),
            target_reached=result.target_reached,
            stop_reason=str(stop_reason),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return result

    @staticmethod
    def _require_resolved(resolved: ResolvedSearch) -> None:
        """Guard the boundary at runtime as well as in the type signature.

        `UnsupportedRequirement` carries an identically shaped `request` and
        `semantics`, so it would satisfy duck typing and quietly widen a search
        whose stated requirement was never applied.
        """
        if type(resolved) is not ResolvedSearch:
            raise InvalidRequestError(
                detail=f"controlled search requires a ResolvedSearch, got {type(resolved).__name__}"
            )


def _words(resolved: ResolvedSearch, family: AttributeFamily) -> tuple[str, ...]:
    """The customer's words for one family's unmatchable strict values."""
    return tuple(a.raw_value for a in resolved.unmatched_strict if a.family is family)


_CENTIMETRE_QUANTUM = Decimal("0.0001")


def _sizes_within(original: ProductSearchRequest, share: Decimal | None) -> dict[str, object]:
    """The customer's sizes widened by `share` of each of their own figures -
    a pair as its shorter and longer side - or no sizes at all for None."""
    if share is None:
        return {"dimensions": (), "planar_dimensions": None}
    sizes = [_widened(c, share) for c in original.dimensions]
    pair = original.planar_dimensions
    taken = {c.role for c in sizes}
    if pair is not None and not taken & {DimensionRole.LENGTH, DimensionRole.OVERALL_WIDTH}:
        shorter, longer = pair.sides
        for role, side, figure in (
            (DimensionRole.OVERALL_WIDTH, FloorSide.SHORTER, shorter),
            (DimensionRole.LENGTH, FloorSide.LONGER, longer),
        ):
            sizes.append(
                DimensionConstraint(
                    role=role,
                    side=side,
                    kind=DimensionConstraintKind.RANGE,
                    min_cm=_cm(figure * (1 - share)),
                    max_cm=_cm(figure * (1 + share)),
                )
            )
    return {"dimensions": tuple(sizes), "planar_dimensions": None}


def _widened(constraint: DimensionConstraint, share: Decimal) -> DimensionConstraint:
    """One size widened outward by `share` of the customer's own figure; a
    figure to sit near becomes a band around it."""
    kind = constraint.kind
    low = constraint.target_cm if kind is DimensionConstraintKind.TARGET else constraint.min_cm
    high = constraint.target_cm if kind is DimensionConstraintKind.TARGET else constraint.max_cm
    return DimensionConstraint(
        role=constraint.role,
        side=constraint.side,
        kind=DimensionConstraintKind.RANGE if kind is DimensionConstraintKind.TARGET else kind,
        min_cm=None if low is None else _cm(low * (1 - share)),
        max_cm=None if high is None else _cm(high * (1 + share)),
        source_value=constraint.source_value,
        source_unit=constraint.source_unit,
    )


def _cm(value: Decimal) -> Decimal:
    return value.quantize(_CENTIMETRE_QUANTUM)


def _nearest_price(price: PriceConstraint, pool: tuple[EligibleProduct, ...]) -> Decimal | None:
    """The real price closest to a budget nothing met, or None if it straddles.

    Every product above their ceiling: the cheapest is the nearest. Every one
    below their floor: the dearest. A mixture means price alone was not what
    stood in the way, so no single figure would be honest.
    """
    amounts = [p.price_amount for p in pool]
    if price.max_amount is not None and min(amounts) > price.max_amount:
        return min(amounts)
    if price.min_amount is not None and max(amounts) < price.min_amount:
        return max(amounts)
    return None
