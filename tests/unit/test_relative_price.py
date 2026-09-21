"""Relative prices: the arithmetic the model must not do.

The subtle part is quantisation. Prices sit on a 0.01 grid, so a threshold of
80.005 has to move to a representable value - and moving it the wrong way lets
through a product that is not actually cheaper. Rounding to nearest would do
exactly that half the time, which is why the direction depends on the
comparator rather than on the digits.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from app.repositories.products import ProductRepository
from app.schemas.agent_decision import PresentedOrdinal
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    ProductInteractionState,
)
from app.schemas.dimensions import RawDimensions
from app.schemas.discovery import ProductSearchRequest
from app.schemas.product import CommerceClassification, ProductRow
from app.schemas.query import ConstraintStrength
from app.schemas.refinement import (
    PriceRefinementOp,
    PriceRelation,
    RelativePriceRefinement,
)
from app.schemas.resolution import (
    ReferenceFailureReason,
    RelativePriceFailureReason,
    RelativePriceUnresolved,
    ResolvedRelativePrice,
)
from app.schemas.retailer import RetailerContext
from app.services.reference_resolver import ProductReferenceResolver
from app.services.relative_price import (
    PRICE_QUANTUM,
    RelativePriceResolver,
    quantise,
    threshold_for,
)
from app.taxonomy.attributes import load_catalog_attributes

CONTEXT = RetailerContext(store_id=50)
REFERENCE_ID = 11


def _row(price: str, *, unit: str = "SAR", product_id: int = REFERENCE_ID) -> ProductRow:
    return ProductRow(
        id=product_id,
        uuid=uuid4(),
        store_id=50,
        name_english="Sofa",
        name_arabic="كنبة",
        price_amount=Decimal(price),
        price_unit=unit,
        image_url="https://example.test/1.jpg",
        product_url="https://example.test/1",
        visual_category="3-seater-sofa",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=RawDimensions(unit="cm"),
        main_color="Beige",
        styles=("Modern",),
        is_active=True,
    )


class FakeRepository:
    def __init__(self, rows: list[ProductRow]) -> None:
        self.rows = rows

    async def get_by_ids(
        self, product_ids: Any, context: RetailerContext
    ) -> list[ProductRow]:
        wanted = set(product_ids)
        return [
            r for r in self.rows if r.id in wanted and r.store_id == context.store_id
        ]


def _state() -> AgentStateV1:
    return AgentStateV1(
        active_search=ActiveSearchState(
            request=ProductSearchRequest(commerce_category="seating"), revision=1
        ),
        product_interaction=ProductInteractionState(
            presented_product_ids=(REFERENCE_ID,), presented_search_revision=1
        ),
    )


async def _resolve(
    relation: PriceRelation,
    *,
    price: str = "1000.00",
    percent: str | None = None,
    unit: str = "SAR",
    active_currency: str | None = None,
    rows: list[ProductRow] | None = None,
) -> Any:
    repository = FakeRepository(
        rows if rows is not None else [_row(price, unit=unit)]
    )
    resolver = RelativePriceResolver(
        ProductReferenceResolver(
            cast(ProductRepository, repository), load_catalog_attributes()
        ),
        cast(ProductRepository, repository),
    )
    return await resolver.resolve(
        RelativePriceRefinement(
            relation=relation,
            reference=PresentedOrdinal(position=1),
            percent=percent,
        ),
        _state(),
        CONTEXT,
        active_currency=active_currency,
    )


def _ok(outcome: Any) -> ResolvedRelativePrice:
    assert isinstance(outcome, ResolvedRelativePrice), outcome
    return outcome


# ── the six relations ═══════════════════════════════════════════════════════


async def test_cheaper_than_is_a_strict_ceiling() -> None:
    result = _ok(await _resolve(PriceRelation.CHEAPER_THAN, price="1000.00"))

    assert result.price.max_amount == "1000.00"
    assert result.price.max_exclusive is True
    assert result.price.min_amount is None


async def test_more_expensive_than_is_a_strict_floor() -> None:
    result = _ok(await _resolve(PriceRelation.MORE_EXPENSIVE_THAN, price="1000.00"))

    assert result.price.min_amount == "1000.00"
    assert result.price.min_exclusive is True
    assert result.price.max_amount is None


async def test_percent_cheaper_includes_the_threshold() -> None:
    """A product at exactly 80% of the price IS 20% cheaper."""
    result = _ok(
        await _resolve(PriceRelation.PERCENT_CHEAPER, price="1000.00", percent="20")
    )

    assert result.price.max_amount == "800.00"
    assert result.price.max_exclusive is False


async def test_more_than_percent_cheaper_excludes_the_threshold() -> None:
    result = _ok(
        await _resolve(
            PriceRelation.MORE_THAN_PERCENT_CHEAPER, price="1000.00", percent="20"
        )
    )

    assert result.price.max_amount == "800.00"
    assert result.price.max_exclusive is True


async def test_percent_more_expensive_includes_the_threshold() -> None:
    result = _ok(
        await _resolve(
            PriceRelation.PERCENT_MORE_EXPENSIVE, price="1000.00", percent="20"
        )
    )

    assert result.price.min_amount == "1200.00"
    assert result.price.min_exclusive is False


async def test_more_than_percent_more_expensive_excludes_the_threshold() -> None:
    result = _ok(
        await _resolve(
            PriceRelation.MORE_THAN_PERCENT_MORE_EXPENSIVE,
            price="1000.00",
            percent="20",
        )
    )

    assert result.price.min_amount == "1200.00"
    assert result.price.min_exclusive is True


# ── directional quantisation ════════════════════════════════════════════════

T = Decimal("80.005")


@pytest.mark.parametrize(
    ("upper", "exclusive", "expected"),
    [
        (True, False, Decimal("80.00")),   # price <= T  -> floor
        (True, True, Decimal("80.01")),    # price <  T  -> ceiling
        (False, False, Decimal("80.01")),  # price >= T  -> ceiling
        (False, True, Decimal("80.00")),   # price >  T  -> floor
    ],
)
def test_a_threshold_moves_away_from_admitting_a_violation(
    upper: bool, exclusive: bool, expected: Decimal
) -> None:
    assert quantise(T, upper=upper, exclusive=exclusive) == expected


@pytest.mark.parametrize(
    ("upper", "exclusive", "allowed", "rejected"),
    [
        (True, True, Decimal("80.00"), Decimal("80.01")),
        (True, False, Decimal("80.00"), Decimal("80.01")),
        (False, True, Decimal("80.01"), Decimal("80.00")),
        (False, False, Decimal("80.01"), Decimal("80.00")),
    ],
)
def test_the_quantised_bound_agrees_with_the_exact_relation(
    upper: bool, exclusive: bool, allowed: Decimal, rejected: Decimal
) -> None:
    """The point of the direction: the grid answer matches the real one."""
    bound = quantise(T, upper=upper, exclusive=exclusive)

    def satisfies(price: Decimal, threshold: Decimal) -> bool:
        if upper:
            return price < threshold if exclusive else price <= threshold
        return price > threshold if exclusive else price >= threshold

    assert satisfies(allowed, bound) is satisfies(allowed, T)
    assert satisfies(rejected, bound) is satisfies(rejected, T)


async def test_a_non_cent_threshold_is_quantised_end_to_end() -> None:
    """160.01 is 20% cheaper than 200.0125 exactly at the boundary."""
    result = _ok(
        await _resolve(
            PriceRelation.MORE_THAN_PERCENT_CHEAPER, price="100.01", percent="20"
        )
    )

    # 100.01 * 0.80 = 80.008 -> strictly-below ceiling -> 80.01
    assert result.price.max_amount == "80.01"
    assert result.price.max_exclusive is True


async def test_an_exact_cent_threshold_is_untouched() -> None:
    result = _ok(
        await _resolve(PriceRelation.PERCENT_CHEAPER, price="1000.00", percent="25")
    )

    assert result.price.max_amount == "750.00"


def test_the_quantum_matches_the_price_column() -> None:
    assert Decimal("0.01") == PRICE_QUANTUM


def test_the_threshold_is_computed_in_decimal() -> None:
    """A percentage of a price through a float arrives already wrong."""
    exact = threshold_for(
        PriceRelation.PERCENT_CHEAPER, Decimal("100.01"), Decimal("20")
    )

    assert exact == Decimal("80.008")
    assert isinstance(exact, Decimal)


def test_no_float_arithmetic_exists_in_the_resolver() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/services/relative_price.py").read_text()

    assert "float(" not in source
    assert "0.8" not in source and "1.2" not in source


async def test_a_fractional_percentage_is_honoured() -> None:
    result = _ok(
        await _resolve(PriceRelation.PERCENT_CHEAPER, price="1000.00", percent="12.5")
    )

    assert result.price.max_amount == "875.00"


async def test_a_large_more_expensive_percentage_is_honoured() -> None:
    result = _ok(
        await _resolve(
            PriceRelation.PERCENT_MORE_EXPENSIVE, price="1000.00", percent="150"
        )
    )

    assert result.price.min_amount == "2500.00"


def test_a_hundred_percent_cheaper_is_still_refused_by_the_contract() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="100% or more cheaper"):
        RelativePriceRefinement(
            relation=PriceRelation.PERCENT_CHEAPER,
            reference=PresentedOrdinal(position=1),
            percent="100",
        )


# ── currency ════════════════════════════════════════════════════════════════


async def test_the_reference_currency_governs_the_bound() -> None:
    result = _ok(await _resolve(PriceRelation.CHEAPER_THAN, unit="USD"))

    assert result.price.currency == "USD"
    assert result.reference_price_unit == "USD"


async def test_a_matching_active_currency_proceeds() -> None:
    result = _ok(
        await _resolve(PriceRelation.CHEAPER_THAN, unit="SAR", active_currency="SAR")
    )

    assert result.price.currency == "SAR"


async def test_no_active_currency_adopts_the_reference_currency() -> None:
    result = _ok(
        await _resolve(PriceRelation.CHEAPER_THAN, unit="SAR", active_currency=None)
    )

    assert result.price.currency == "SAR"


async def test_a_conflicting_currency_refuses_rather_than_converting() -> None:
    """Nothing converts, and replacing their stated currency changes the ask."""
    outcome = await _resolve(
        PriceRelation.CHEAPER_THAN, unit="USD", active_currency="SAR"
    )

    assert isinstance(outcome, RelativePriceUnresolved)
    assert outcome.reason is RelativePriceFailureReason.CURRENCY_CONFLICT


# ── strength, output shape and failures ═════════════════════════════════════


@pytest.mark.parametrize(
    "relation", [PriceRelation.CHEAPER_THAN, PriceRelation.MORE_EXPENSIVE_THAN]
)
async def test_a_derived_bound_is_locked(relation: PriceRelation) -> None:
    """A later widening that returned dearer products would answer differently."""
    result = _ok(await _resolve(relation))
    strength = result.price.max_strength or result.price.min_strength

    assert strength is ConstraintStrength.LOCKED


async def test_the_output_is_an_ordinary_absolute_operation() -> None:
    """The composer never learns a product was involved."""
    result = _ok(await _resolve(PriceRelation.CHEAPER_THAN))

    assert result.price.op is PriceRefinementOp.SET
    assert result.price.relative is None


async def test_the_reference_price_is_read_from_the_catalog() -> None:
    result = _ok(await _resolve(PriceRelation.CHEAPER_THAN, price="1234.56"))

    assert result.reference_price_amount == "1234.56"
    assert result.reference_product_id == REFERENCE_ID


async def test_an_unresolvable_reference_carries_its_own_reason() -> None:
    outcome = await _resolve(PriceRelation.CHEAPER_THAN, rows=[])

    assert isinstance(outcome, RelativePriceUnresolved)
    assert outcome.reason is RelativePriceFailureReason.REFERENCE_UNRESOLVED
    assert outcome.reference_reason is ReferenceFailureReason.PRODUCT_UNAVAILABLE


def test_the_resolver_never_calls_the_composer() -> None:
    """Composition stays pure and unaware of product facts."""
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/services/relative_price.py").read_text()

    assert "SearchRefinementComposer" not in source
    assert "refine(" not in source
