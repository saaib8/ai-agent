"""Controlled search orchestration: exact first, stop early, never leak scope."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from app.core.config import RelaxationSettings
from app.core.exceptions import InvalidRequestError
from app.schemas.discovery import (
    PriceConstraint,
    ProductSearchRequest,
    ProductSort,
    SeatingCapacityConstraint,
)
from app.schemas.product import EligibleProduct
from app.schemas.query import (
    ClarificationReason,
    ClarificationRequired,
    ConstraintSemantics,
    ConstraintStrength,
    RequirementFamily,
    ResolvedSearch,
    UnsupportedRequirement,
)
from app.schemas.relaxation import RelaxationChange, StopReason
from app.schemas.retailer import RetailerContext
from app.services.controlled_search import ControlledRelaxationService
from app.services.discovery import ProductDiscoveryService
from app.services.relaxation import RelaxationPlanner

APPROXIMATE = ConstraintStrength.APPROXIMATE
PREFERRED = ConstraintStrength.PREFERRED
LOCKED = ConstraintStrength.LOCKED
SAR = "SAR"
CONTEXT = RetailerContext(store_id=50)
OTHER_CONTEXT = RetailerContext(store_id=60)


def _candidate(product_id: int) -> EligibleProduct:
    return EligibleProduct(product_id=product_id, price_amount=Decimal("2450.00"))


class FakeDiscovery:
    """Returns a scripted eligible pool per call and records what it was asked.

    Mirrors the real signature: `eligible_pool` takes no limit, so this fake
    could not honour one even if a caller tried to pass it.
    """

    def __init__(self, per_call: list[list[int]]) -> None:
        self._per_call = per_call
        self.calls: list[dict[str, Any]] = []

    async def eligible_pool(
        self, request: ProductSearchRequest, context: RetailerContext
    ) -> tuple[EligibleProduct, ...]:
        index = len(self.calls)
        self.calls.append({"request": request, "context": context})
        ids = self._per_call[index] if index < len(self._per_call) else []
        return tuple(_candidate(i) for i in ids)


def _service(
    per_call: list[list[int]], **settings: Any
) -> tuple[ControlledRelaxationService, FakeDiscovery]:
    policy = RelaxationSettings(**settings)
    fake = FakeDiscovery(per_call)
    return (
        ControlledRelaxationService(
            cast(ProductDiscoveryService, fake), RelaxationPlanner(policy), policy
        ),
        fake,
    )


def _resolved(
    *,
    price: PriceConstraint | None = None,
    capacity: SeatingCapacityConstraint | None = None,
    limit: int | None = None,
    sort: ProductSort = ProductSort.DEFAULT,
    **semantics: ConstraintStrength | None,
) -> ResolvedSearch:
    return ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sofa",
            price=price,
            seating_capacity=capacity,
            limit=limit,
            sort=sort,
        ),
        semantics=ConstraintSemantics(**semantics),
    )


def _soft_price(amount: str = "5000") -> dict[str, Any]:
    return {
        "price": PriceConstraint(currency=SAR, max_amount=Decimal(amount)),
        "price_max": APPROXIMATE,
    }


# ── exact search always first ───────────────────────────────────────────────


async def test_the_exact_request_is_always_the_first_search() -> None:
    """Even when every constraint is soft, nothing is pre-relaxed."""
    service, fake = _service([[1, 2, 3, 4, 5]])

    result = await service.search(_resolved(**_soft_price()), CONTEXT)

    assert fake.calls[0]["request"].price is not None
    assert fake.calls[0]["request"].price.max_amount == Decimal("5000")
    assert result.attempts[0].depth == 0
    assert result.attempts[0].changes == ()


async def test_a_sufficient_exact_search_triggers_no_relaxation() -> None:
    service, fake = _service([[1, 2, 3, 4, 5]])

    result = await service.search(_resolved(**_soft_price()), CONTEXT)

    assert len(fake.calls) == 1
    assert result.stop_reason is StopReason.EXACT_SUFFICIENT
    assert result.relaxation_attempt_count == 0
    assert result.was_relaxed is False
    assert result.final_request == result.original_request


async def test_relaxation_stops_the_moment_the_target_is_reached() -> None:
    service, fake = _service([[1], [1, 2, 3, 4, 5], [9, 9, 9]])

    result = await service.search(_resolved(**_soft_price()), CONTEXT)

    assert len(fake.calls) == 2  # the third step is never needed
    assert result.stop_reason is StopReason.TARGET_REACHED
    assert result.target_reached is True


async def test_nothing_relaxable_stops_after_the_exact_search() -> None:
    service, fake = _service([[1, 2]])

    result = await service.search(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            price_max=LOCKED,
        ),
        CONTEXT,
    )

    assert len(fake.calls) == 1
    assert result.stop_reason is StopReason.NO_RELAXABLE_CONSTRAINTS
    assert result.target_reached is False
    assert result.final_request == result.original_request


async def test_exhausting_the_policy_is_a_valid_outcome() -> None:
    service, fake = _service([[1], [1], [1]])

    result = await service.search(_resolved(**_soft_price()), CONTEXT)

    assert len(fake.calls) == 3  # exact + two price steps
    assert result.stop_reason is StopReason.POLICY_EXHAUSTED
    assert result.target_reached is False
    assert result.exact_candidate_count == 1


async def test_zero_results_everywhere_is_a_valid_outcome() -> None:
    service, _ = _service([[], [], []])

    result = await service.search(_resolved(**_soft_price()), CONTEXT)

    assert result.candidates == ()
    assert result.exact_candidate_count == 0
    assert result.stop_reason is StopReason.POLICY_EXHAUSTED


# ── the sequence of requests ────────────────────────────────────────────────


async def test_the_request_sequence_follows_the_policy_order() -> None:
    service, fake = _service([[1], [1], [1], [1]])

    await service.search(
        _resolved(
            price=PriceConstraint(currency=SAR, max_amount=Decimal("5000")),
            capacity=SeatingCapacityConstraint.exactly(4),
            price_max=APPROXIMATE,
            seating_min=PREFERRED,
            seating_max=PREFERRED,
        ),
        CONTEXT,
    )

    ceilings = [c["request"].price.max_amount for c in fake.calls]
    seats = [
        (c["request"].seating_capacity.min_capacity, c["request"].seating_capacity.max_capacity)
        for c in fake.calls
    ]
    assert ceilings == [
        Decimal("5000"), Decimal("5500.00"), Decimal("6000.00"), Decimal("6000.00")
    ]
    assert seats == [(4, 4), (4, 4), (4, 4), (3, 5)]


async def test_the_final_request_is_the_last_one_executed() -> None:
    service, fake = _service([[1], [1], [1]])

    result = await service.search(_resolved(**_soft_price()), CONTEXT)

    assert result.final_request == fake.calls[-1]["request"]
    assert result.final_request.price is not None
    assert result.final_request.price.max_amount == Decimal("6000.00")


async def test_the_original_request_survives_untouched() -> None:
    resolved = _resolved(**_soft_price())
    service, _ = _service([[1], [1], [1]])

    result = await service.search(resolved, CONTEXT)

    assert result.original_request == resolved.request
    assert result.original_request.price is not None
    assert result.original_request.price.max_amount == Decimal("5000")


# ── candidate pool ──────────────────────────────────────────────────────────


async def test_candidates_are_deduplicated_across_attempts() -> None:
    service, _ = _service([[1, 2, 3], [1, 2, 3, 4, 5]])

    result = await service.search(_resolved(**_soft_price()), CONTEXT)

    assert [c.product.product_id for c in result.candidates] == [1, 2, 3, 4, 5]


async def test_a_product_keeps_the_lowest_depth_it_earned() -> None:
    service, _ = _service([[1, 2, 3], [1, 2, 3, 4, 5]])

    result = await service.search(_resolved(**_soft_price()), CONTEXT)
    depths = {c.product.product_id: c.relaxation_depth for c in result.candidates}

    assert depths == {1: 0, 2: 0, 3: 0, 4: 1, 5: 1}


async def test_exact_candidates_are_always_depth_zero() -> None:
    service, _ = _service([[1, 2], [1, 2, 3], [1, 2, 3, 4, 5]])

    result = await service.search(_resolved(**_soft_price()), CONTEXT)
    depths = {c.product.product_id: c.relaxation_depth for c in result.candidates}

    assert depths[1] == 0
    assert depths[2] == 0
    assert depths[3] == 1
    assert depths[4] == 2


async def test_first_seen_order_is_preserved() -> None:
    service, _ = _service([[7, 3], [9, 1, 5]])

    result = await service.search(_resolved(**_soft_price()), CONTEXT)

    assert [c.product.product_id for c in result.candidates] == [7, 3, 9, 1, 5]


async def test_provenance_is_not_a_score() -> None:
    service, _ = _service([[1, 2, 3, 4, 5]])

    result = await service.search(_resolved(**_soft_price()), CONTEXT)

    assert "score" not in result.candidates[0].model_dump()
    assert "score" not in result.model_dump()


# ── the request limit no longer touches sufficiency ─────────────────────────
#
# Every attempt reads the COMPLETE eligible pool, so a presentation bound on
# the request cannot make the catalog look smaller than it is. The workaround
# that used to raise the limit to the target is gone with the bound it worked
# around.


async def test_a_low_request_limit_does_not_cause_false_relaxation() -> None:
    """Asking for two must not make a catalog of five look exhausted."""
    service, fake = _service([[1, 2, 3, 4, 5]])

    result = await service.search(_resolved(limit=2, **_soft_price()), CONTEXT)

    assert len(fake.calls) == 1
    assert result.stop_reason is StopReason.EXACT_SUFFICIENT
    assert result.exact_candidate_count == 5


async def test_the_request_limit_is_passed_through_untouched() -> None:
    """Nothing raises, lowers or rewrites it on the way to the pool query."""
    service, fake = _service([[1, 2, 3, 4, 5]])

    result = await service.search(_resolved(limit=2, **_soft_price()), CONTEXT)

    assert fake.calls[0]["request"].limit == 2
    assert result.original_request.limit == 2
    assert result.attempts[0].request.limit == 2
    assert result.attempts[0].changes == ()


async def test_the_pool_query_accepts_no_limit_argument() -> None:
    """The bound cannot come back by a caller passing one."""
    import inspect

    from app.services.discovery import ProductDiscoveryService

    signature = inspect.signature(ProductDiscoveryService.eligible_pool)

    assert set(signature.parameters) == {"self", "request", "context"}


# ── retailer isolation ──────────────────────────────────────────────────────


async def test_every_attempt_receives_the_same_context() -> None:
    service, fake = _service([[1], [1], [1]])

    await service.search(_resolved(**_soft_price()), CONTEXT)

    assert len(fake.calls) == 3
    assert all(call["context"] is CONTEXT for call in fake.calls)


async def test_a_different_context_is_never_substituted() -> None:
    service, fake = _service([[1], [1], [1]])

    await service.search(_resolved(**_soft_price()), OTHER_CONTEXT)

    assert {call["context"].store_id for call in fake.calls} == {60}


def test_no_request_type_can_carry_a_store() -> None:
    from app.schemas.relaxation import ControlledSearchResult

    assert "store_id" not in ControlledSearchResult.model_fields
    assert "store_id" not in ProductSearchRequest.model_fields


# ── boundary with M7 outcomes ───────────────────────────────────────────────


async def test_an_unsupported_requirement_cannot_enter_relaxation() -> None:
    """Widening a budget cannot compensate for a colour we never applied."""
    service, fake = _service([[1]])
    unsupported = UnsupportedRequirement(
        request=ProductSearchRequest(commerce_category="seating"),
        semantics=ConstraintSemantics(price_max=APPROXIMATE),
        unsupported=(RequirementFamily.COLOR,),
    )

    with pytest.raises(InvalidRequestError):
        await service.search(cast(ResolvedSearch, unsupported), CONTEXT)

    assert fake.calls == []


async def test_a_clarification_cannot_enter_relaxation() -> None:
    service, fake = _service([[1]])
    clarification = ClarificationRequired(
        reason=ClarificationReason.MULTIPLE_PRODUCT_TYPES
    )

    with pytest.raises(InvalidRequestError):
        await service.search(cast(ResolvedSearch, clarification), CONTEXT)

    assert fake.calls == []


# ── sort ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sort", list(ProductSort))
async def test_the_sort_is_preserved_on_every_attempt(sort: ProductSort) -> None:
    service, fake = _service([[1], [1], [1]])

    await service.search(_resolved(sort=sort, **_soft_price()), CONTEXT)

    assert all(call["request"].sort is sort for call in fake.calls)


# ── bounds ──────────────────────────────────────────────────────────────────


async def test_at_most_seven_searches_are_ever_made() -> None:
    """One exact plus at most six widenings."""
    service, fake = _service([[1]] * 12)

    result = await service.search(
        _resolved(
            price=PriceConstraint(
                currency=SAR, min_amount=Decimal("3000"), max_amount=Decimal("5000")
            ),
            capacity=SeatingCapacityConstraint(min_capacity=3, max_capacity=5),
            price_min=APPROXIMATE,
            price_max=PREFERRED,
            seating_min=APPROXIMATE,
            seating_max=PREFERRED,
        ),
        CONTEXT,
    )

    assert len(fake.calls) == 7
    assert result.relaxation_attempt_count == 6


async def test_attempt_metadata_records_each_change() -> None:
    service, _ = _service([[1], [1], [1]])

    result = await service.search(_resolved(**_soft_price()), CONTEXT)

    assert [a.depth for a in result.attempts] == [0, 1, 2]
    assert result.attempts[0].changes == ()
    first, second = result.attempts[1].changes[0], result.attempts[2].changes[0]
    assert isinstance(first, RelaxationChange)
    assert isinstance(second, RelaxationChange)
    assert first.to_value == Decimal("5500.00")
    assert first.strength is APPROXIMATE
    assert second.to_value == Decimal("6000.00")


async def test_a_configurable_target_is_respected() -> None:
    """The target is policy, not a literal buried in the service."""
    service, fake = _service([[1, 2], [1, 2, 3]], target_candidates=3)

    result = await service.search(_resolved(**_soft_price()), CONTEXT)

    assert result.target_candidates == 3
    assert result.stop_reason is StopReason.TARGET_REACHED
    assert len(fake.calls) == 2
