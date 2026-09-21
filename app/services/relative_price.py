"""Turning "cheaper than that one" into a bound.

The customer named a relation and a product; the amount comes from the
catalog. So this reads the reference product's price fresh from PostgreSQL and
does the arithmetic, and the model does neither (CLAUDE.md 3.3).

It rewrites the price operation and stops there. The composer stays pure and
never learns a product was involved, which is what keeps search composition
free of catalog facts.

**Quantisation is directional, not approximate.** Prices live on a 0.01 grid,
so a threshold like 80.005 has to be moved to a representable value - and
moved the way that cannot admit a product violating the relation. Rounding to
nearest would sometimes let a product through that is not actually cheaper.

    price <  80.005   ->  price <  80.01   (ceiling: 80.00 in, 80.01 out)
    price <= 80.005   ->  price <= 80.00   (floor)
    price >  80.005   ->  price >  80.00   (floor: 80.01 in, 80.00 out)
    price >= 80.005   ->  price >= 80.01   (ceiling)

A derived bound is **locked**. "Cheaper than this" is a correctness statement,
not a leaning, and a later widening that returned dearer products would answer
a different question.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.core.logging import get_logger
from app.repositories.products import ProductRepository
from app.schemas.agent_state import AgentStateV1
from app.schemas.query import ConstraintStrength
from app.schemas.refinement import (
    PriceRefinement,
    PriceRefinementOp,
    PriceRelation,
    RelativePriceRefinement,
)
from app.schemas.resolution import (
    ReferenceFailureReason,
    ReferenceUnresolved,
    RelativePriceFailureReason,
    RelativePriceOutcome,
    RelativePriceUnresolved,
    ResolvedProductReference,
    ResolvedRelativePrice,
)
from app.schemas.retailer import RetailerContext
from app.services.reference_resolver import ProductReferenceResolver

logger = get_logger(__name__)

PRICE_QUANTUM = Decimal("0.01")
"""The grid `core_product.price_amount` lives on: NUMERIC(10, 2)."""

_HUNDRED = Decimal(100)

# A derived relation is a requirement, so its bound is never widened.
_DERIVED_STRENGTH = ConstraintStrength.LOCKED

# relation -> (is_upper_bound, is_exclusive)
_BOUNDS: dict[PriceRelation, tuple[bool, bool]] = {
    PriceRelation.CHEAPER_THAN: (True, True),
    PriceRelation.PERCENT_CHEAPER: (True, False),
    PriceRelation.MORE_THAN_PERCENT_CHEAPER: (True, True),
    PriceRelation.MORE_EXPENSIVE_THAN: (False, True),
    PriceRelation.PERCENT_MORE_EXPENSIVE: (False, False),
    PriceRelation.MORE_THAN_PERCENT_MORE_EXPENSIVE: (False, True),
}


def quantise(threshold: Decimal, *, upper: bool, exclusive: bool) -> Decimal:
    """Move a threshold onto the price grid, away from admitting a violation.

    An inclusive ceiling rounds down and an exclusive one rounds up; a floor
    does the opposite. Either way the products the relation excludes stay
    excluded, and no product the relation allows is lost.
    """
    rounding = ROUND_FLOOR if upper != exclusive else ROUND_CEILING
    return threshold.quantize(PRICE_QUANTUM, rounding=rounding)


def threshold_for(relation: PriceRelation, price: Decimal, percent: Decimal | None) -> Decimal:
    """The exact, unquantised figure the relation describes.

    Decimal throughout: a percentage of a price through a binary float would
    arrive already wrong.
    """
    if percent is None:
        return price
    factor = (
        _HUNDRED - percent if relation.is_cheaper else _HUNDRED + percent
    )
    return price * factor / _HUNDRED


class RelativePriceResolver:
    """Relation plus reference product -> an absolute price operation."""

    def __init__(
        self, resolver: ProductReferenceResolver, repository: ProductRepository
    ) -> None:
        self._resolver = resolver
        self._repository = repository

    async def resolve(
        self,
        refinement: RelativePriceRefinement,
        state: AgentStateV1,
        context: RetailerContext,
        *,
        active_currency: str | None = None,
    ) -> RelativePriceOutcome:
        reference = await self._resolver.resolve(
            refinement.reference, state, context
        )
        if isinstance(reference, ReferenceUnresolved):
            return RelativePriceUnresolved(
                reason=RelativePriceFailureReason.REFERENCE_UNRESOLVED,
                reference_reason=reference.reason,
            )
        return await self._from_reference(
            refinement, reference, context, active_currency
        )

    async def _from_reference(
        self,
        refinement: RelativePriceRefinement,
        reference: ResolvedProductReference,
        context: RetailerContext,
        active_currency: str | None,
    ) -> RelativePriceOutcome:
        rows = await self._repository.get_by_ids([reference.product_id], context)
        if not rows:
            # It existed a moment ago and does not now.
            return RelativePriceUnresolved(
                reason=RelativePriceFailureReason.REFERENCE_UNRESOLVED,
                reference_reason=ReferenceFailureReason.PRODUCT_UNAVAILABLE,
            )
        product = rows[0]

        # The bound is denominated in the reference's own currency: it is the
        # only one in which "cheaper than this" means anything. Replacing a
        # currency the customer already stated would change their question.
        if active_currency is not None and active_currency != product.price_unit:
            return RelativePriceUnresolved(
                reason=RelativePriceFailureReason.CURRENCY_CONFLICT
            )

        percent = refinement.percent_value
        if refinement.relation.needs_percent and percent is None:
            return RelativePriceUnresolved(
                reason=RelativePriceFailureReason.MALFORMED_PERCENT
            )

        upper, exclusive = _BOUNDS[refinement.relation]
        exact = threshold_for(refinement.relation, product.price_amount, percent)
        bound = quantise(exact, upper=upper, exclusive=exclusive)

        logger.info(
            "relative_price_resolved",
            store_id=context.store_id,
            relation=str(refinement.relation),
            upper_bound=upper,
            exclusive=exclusive,
        )
        return ResolvedRelativePrice(
            price=_absolute(bound, product.price_unit, upper=upper, exclusive=exclusive),
            reference_product_id=product.id,
            reference_price_amount=str(product.price_amount),
            reference_price_unit=product.price_unit,
        )


def _absolute(
    bound: Decimal, currency: str, *, upper: bool, exclusive: bool
) -> PriceRefinement:
    """The ordinary absolute operation the composer already understands."""
    if upper:
        return PriceRefinement(
            op=PriceRefinementOp.SET,
            max_amount=str(bound),
            currency=currency,
            max_exclusive=exclusive,
            max_strength=_DERIVED_STRENGTH,
        )
    return PriceRefinement(
        op=PriceRefinementOp.SET,
        min_amount=str(bound),
        currency=currency,
        min_exclusive=exclusive,
        min_strength=_DERIVED_STRENGTH,
    )
