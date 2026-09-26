"""Nothing found is never a dead end.

When even the widest permitted search is empty, the relaxation layer counts
what each of the customer's own requirements, set aside alone, would find - and
for a budget nothing meets, where prices actually start. Nothing is set aside
for them; the reply offers it as a next step.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from app.core.config import RelaxationSettings
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PriceConstraint,
    ProductSearchRequest,
    SeatingCapacityConstraint,
)
from app.schemas.product import EligibleProduct
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    ResolvedSearch,
)
from app.schemas.relaxation import SetAsideField, SetAsideOption
from app.schemas.response import ResponseOutcomeKind
from app.schemas.retailer import RetailerContext
from app.services.controlled_search import ControlledRelaxationService
from app.services.discovery import ProductDiscoveryService
from app.services.numeric_guard import build_allowance, check_numeric_policy
from app.services.relaxation import RelaxationPlanner
from app.services.response_wording import FALLBACK_WORDING
from app.services.search_pipeline import ProductSearchPipeline
from app.taxonomy.dimensions import DimensionRole

from tests.unit.test_search_pipeline import FakeHydration, FakeRanking

CONTEXT = RetailerContext(store_id=50)
LOCKED = ConstraintStrength.LOCKED
WIDTH = DimensionRole.OVERALL_WIDTH
BUDGET = PriceConstraint(currency="SAR", max_amount=Decimal("5000"))
NARROW = DimensionConstraint(role=WIDTH, kind=DimensionConstraintKind.MAX, max_cm=Decimal("200"))
THREE_SEATS = SeatingCapacityConstraint(min_capacity=3, max_capacity=3)


def _resolved(request: ProductSearchRequest) -> ResolvedSearch:
    return ResolvedSearch(
        request=request,
        semantics=ConstraintSemantics(
            subcategory=LOCKED,
            price_min=LOCKED if request.price and request.price.min_amount is not None else None,
            price_max=LOCKED if request.price and request.price.max_amount is not None else None,
            seating_min=LOCKED if request.seating_capacity else None,
            seating_max=LOCKED if request.seating_capacity else None,
            dimensions=tuple(
                DimensionConstraintSemantics(role=d.role, strength=LOCKED)
                for d in request.dimensions
            ),
        ),
    )


def _everything() -> ResolvedSearch:
    """A budget, a seat count and a width, all locked so nothing is widened.

    No strict colour or style: when only a colour stood in the way, the
    colour last resort (12.4) has already shown those products, so the search
    is not empty and there is nothing to explain.
    """
    return _resolved(
        ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sofa",
            price=BUDGET,
            seating_capacity=THREE_SEATS,
            dimensions=(NARROW,),
        )
    )


class _Catalog:
    """A catalog of (price, currency, width, seats, colour, style) rows,
    filtered the way PostgreSQL would."""

    def __init__(self, rows: list[tuple[str, str, int, int, str, str]]) -> None:
        self.rows = rows
        self.requests: list[ProductSearchRequest] = []

    async def eligible_pool(
        self, request: ProductSearchRequest, context: RetailerContext
    ) -> tuple[EligibleProduct, ...]:
        self.requests.append(request)
        return tuple(
            EligibleProduct(product_id=n, price_amount=Decimal(price))
            for n, (price, currency, width, seats, colour, style) in enumerate(self.rows, 1)
            if _matches(request, Decimal(price), currency, width, seats, colour, style)
        )


def _matches(
    request: ProductSearchRequest,
    price: Decimal,
    currency: str,
    width: int,
    seats: int,
    colour: str,
    style: str,
) -> bool:
    bound = request.price
    if bound is not None:
        if currency != bound.currency:
            return False
        if bound.min_amount is not None and price < bound.min_amount:
            return False
        if bound.max_amount is not None and price > bound.max_amount:
            return False
    capacity = request.seating_capacity
    if capacity is not None and not (
        (capacity.min_capacity or 0) <= seats <= (capacity.max_capacity or 99)
    ):
        return False
    if any(c.max_cm is not None and width > c.max_cm for c in request.dimensions):
        return False
    if request.colors_any_of and colour not in request.colors_any_of:
        return False
    return not request.styles_all_of or style in request.styles_all_of


def _service(catalog: _Catalog) -> ControlledRelaxationService:
    settings = RelaxationSettings()
    return ControlledRelaxationService(
        cast(ProductDiscoveryService, catalog), RelaxationPlanner(settings), settings
    )


def _options(options: tuple[SetAsideOption, ...]) -> dict[tuple[SetAsideField, Any], int]:
    return {(o.field, o.role): o.eligible_count for o in options}


# Each row fails exactly one requirement of `_everything`.
ONE_MISS_EACH = [
    ("7000", "SAR", 180, 3, "Beige", "Modern"),  # over budget
    ("4000", "SAR", 240, 3, "Beige", "Modern"),  # too wide
    ("4100", "SAR", 250, 3, "Beige", "Modern"),  # too wide
]


async def test_each_requirement_is_counted_set_aside_alone() -> None:
    result = await _service(_Catalog(ONE_MISS_EACH)).search(
        _everything(), CONTEXT, explain_empty=True
    )

    assert result.candidates == ()
    assert _options(result.set_aside) == {
        (SetAsideField.PRICE, None): 1,
        (SetAsideField.DIMENSION, WIDTH): 2,
        # The seat count set aside finds nothing more: no way forward, so absent.
    }


async def test_the_counts_come_from_the_customers_own_request() -> None:
    catalog = _Catalog(ONE_MISS_EACH)

    result = await _service(catalog).search(_everything(), CONTEXT, explain_empty=True)

    variants = catalog.requests[-3:]
    assert result.original_request == _everything().request
    assert all(v.commerce_subcategory == "sofa" for v in variants)
    assert all(c == NARROW for v in variants for c in v.dimensions)


async def test_a_found_search_or_an_unasked_one_costs_no_extra_queries() -> None:
    plain = _Catalog(ONE_MISS_EACH)
    await _service(plain).search(_everything(), CONTEXT)
    explained = _Catalog(ONE_MISS_EACH)
    await _service(explained).search(_everything(), CONTEXT, explain_empty=True)
    assert len(explained.requests) == len(plain.requests) + 3

    found = _Catalog([("4000", "SAR", 180, 3, "Beige", "Modern")])
    result = await _service(found).search(_everything(), CONTEXT, explain_empty=True)
    unexplained = _Catalog([("4000", "SAR", 180, 3, "Beige", "Modern")])
    await _service(unexplained).search(_everything(), CONTEXT)

    assert result.set_aside == ()
    assert len(found.requests) == len(unexplained.requests)


# ── the nearest real price ──────────────────────────────────────────────────

PRICES = [
    ("990", "SAR", 180, 3, "Beige", "Modern"),
    ("1250", "SAR", 180, 3, "Beige", "Modern"),
    ("8000", "SAR", 180, 3, "Beige", "Modern"),
    ("40", "USD", 180, 3, "Beige", "Modern"),
]


async def _price_option(price: PriceConstraint) -> SetAsideOption | None:
    request = ProductSearchRequest(
        commerce_category="seating", commerce_subcategory="sofa", price=price
    )
    result = await _service(_Catalog(PRICES)).search(
        _resolved(request), CONTEXT, explain_empty=True
    )
    assert result.candidates == ()
    return next((o for o in result.set_aside if o.field is SetAsideField.PRICE), None)


async def test_a_budget_too_low_reports_where_prices_start() -> None:
    option = await _price_option(PriceConstraint(currency="SAR", max_amount=Decimal("500")))

    assert option is not None
    assert option.nearest_price == Decimal("990")
    assert option.currency == "SAR"
    # The USD 40 row never counts toward a riyal budget.
    assert option.eligible_count == 3


async def test_a_minimum_too_high_reports_where_prices_end() -> None:
    option = await _price_option(PriceConstraint(currency="SAR", min_amount=Decimal("9000")))

    assert option is not None
    assert option.nearest_price == Decimal("8000")


async def test_a_range_that_straddles_the_catalog_names_no_single_price() -> None:
    option = await _price_option(
        PriceConstraint(currency="SAR", min_amount=Decimal("1000"), max_amount=Decimal("1200"))
    )

    assert option is not None
    assert option.nearest_price is None
    assert option.currency is None


async def test_a_currency_the_store_does_not_price_in_offers_nothing() -> None:
    assert await _price_option(PriceConstraint(currency="AED", max_amount=Decimal("500"))) is None


# ── only a customer search is explained ─────────────────────────────────────


def _pipeline(catalog: _Catalog) -> ProductSearchPipeline:
    return ProductSearchPipeline(
        _service(catalog),
        cast(Any, FakeRanking()),
        cast(Any, FakeHydration()),
        presentation_limit=3,
    )


async def test_a_customer_search_carries_the_counts_to_the_reply() -> None:
    execution = await _pipeline(_Catalog(ONE_MISS_EACH)).execute(_everything(), CONTEXT)

    assert {o.field for o in execution.grounding.set_aside} == {
        SetAsideField.PRICE,
        SetAsideField.DIMENSION,
    }


async def test_room_optimisation_never_pays_for_the_counts() -> None:
    catalog = _Catalog(ONE_MISS_EACH)
    plain = _Catalog(ONE_MISS_EACH)
    await _service(plain).search(_everything(), CONTEXT)

    await _pipeline(catalog).execute_candidate_pool(_everything(), CONTEXT)

    assert len(catalog.requests) == len(plain.requests)


# ── the reply may say them ──────────────────────────────────────────────────


def test_the_counts_and_the_nearest_price_are_sayable() -> None:
    allowance = build_allowance("sofas under 500 please", counts=(14,), figures=(Decimal("990"),))

    said = "Sofas here start at 990 SAR, and without the width limit there are 14."
    assert check_numeric_policy(message=said, follow_up_question=None, allowance=allowance) is None


def test_a_figure_nobody_supplied_is_still_refused() -> None:
    allowance = build_allowance("sofas under 500 please", counts=(14,), figures=(Decimal("990"),))

    violation = check_numeric_policy(
        message="The next one up is 1250.", follow_up_question=None, allowance=allowance
    )

    assert violation is not None


def test_the_fixed_zero_results_sentence_offers_a_next_step() -> None:
    assert "widen" in FALLBACK_WORDING[ResponseOutcomeKind.ZERO_RESULTS]
