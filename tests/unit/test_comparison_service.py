"""Comparing products on facts, and admitting what is not known.

The mistake this service exists to avoid: reporting two absences as agreement.
Two products that both record no seating capacity are not "the same" — nobody
has established anything about either, and a customer told they match would be
told something false.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from app.core.config import CustomerAgentSettings
from app.repositories.products import ProductRepository
from app.schemas.comparison import (
    ComparisonField,
    ComparisonStatus,
    ProductComparisonResult,
)
from app.schemas.dimensions import RawDimensions
from app.schemas.product import CommerceClassification, ProductRow
from app.schemas.resolution import ComparisonFailureReason, ComparisonUnavailable
from app.schemas.retailer import RetailerContext
from app.services.comparison import ProductComparisonService
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy

CONTEXT = RetailerContext(store_id=50)


def _row(
    product_id: int,
    *,
    store_id: int = 50,
    price: str = "1000.00",
    unit: str = "SAR",
    subcategory: str | None = "sofa",
    capacity: int | None = 3,
    color: str | None = "Beige",
    styles: tuple[str, ...] = ("Modern",),
    dimensions: RawDimensions | None = None,
) -> ProductRow:
    return ProductRow(
        id=product_id,
        uuid=uuid4(),
        store_id=store_id,
        name_english=f"Sofa {product_id}",
        name_arabic="كنبة",
        price_amount=Decimal(price),
        price_unit=unit,
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        visual_category="3-seater-sofa",
        commerce=CommerceClassification(
            category="seating", subcategory=subcategory, seating_capacity=capacity
        ),
        dimensions=dimensions or RawDimensions(unit="cm"),
        main_color=color,
        styles=styles,
        is_active=True,
    )


class FakeRepository:
    def __init__(self, rows: list[ProductRow]) -> None:
        self.rows = rows
        self.calls: list[RetailerContext] = []

    async def get_by_ids(
        self, product_ids: Any, context: RetailerContext
    ) -> list[ProductRow]:
        self.calls.append(context)
        wanted = set(product_ids)
        return [
            r for r in self.rows if r.id in wanted and r.store_id == context.store_id
        ]


def _service(
    rows: list[ProductRow], maximum: int = 3
) -> tuple[ProductComparisonService, FakeRepository]:
    repository = FakeRepository(rows)
    taxonomy = load_taxonomy()
    return (
        ProductComparisonService(
            cast(ProductRepository, repository),
            load_dimension_semantics(taxonomy=taxonomy),
            CustomerAgentSettings(comparison_max_products=maximum),
        ),
        repository,
    )


async def _compare(rows: list[ProductRow], ids: list[int], maximum: int = 3) -> Any:
    service, _ = _service(rows, maximum)
    return await service.compare(ids, CONTEXT)


def _ok(outcome: Any) -> ProductComparisonResult:
    assert isinstance(outcome, ProductComparisonResult), outcome
    return outcome


def _row_for(result: ProductComparisonResult, field: ComparisonField) -> Any:
    return next(r for r in result.rows if r.field is field)


# ── counts ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("count", [2, 3, 4])
async def test_a_comparison_within_the_configured_maximum_runs(count: int) -> None:
    rows = [_row(i) for i in range(1, count + 1)]

    result = _ok(await _compare(rows, [r.id for r in rows], maximum=4))

    assert len(result.products) == count


async def test_one_product_is_not_a_comparison() -> None:
    outcome = await _compare([_row(1)], [1])

    assert isinstance(outcome, ComparisonUnavailable)
    assert outcome.reason is ComparisonFailureReason.TOO_FEW_PRODUCTS


async def test_more_than_the_configured_maximum_is_refused_not_truncated() -> None:
    """Dropping the last one silently would compare a different set."""
    rows = [_row(i) for i in range(1, 5)]

    outcome = await _compare(rows, [1, 2, 3, 4], maximum=3)

    assert isinstance(outcome, ComparisonUnavailable)
    assert outcome.reason is ComparisonFailureReason.TOO_MANY_PRODUCTS
    assert (outcome.requested_count, outcome.allowed_maximum) == (4, 3)


def test_the_configured_maximum_cannot_exceed_the_schema_ceiling() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CustomerAgentSettings(comparison_max_products=5)
    with pytest.raises(ValidationError):
        CustomerAgentSettings(comparison_max_products=1)


def test_the_default_maximum_is_three() -> None:
    assert CustomerAgentSettings().comparison_max_products == 3


async def test_the_same_product_twice_is_refused() -> None:
    """Two selectors landed on one product; comparing it with itself says nothing."""
    outcome = await _compare([_row(1), _row(2)], [1, 1])

    assert isinstance(outcome, ComparisonUnavailable)
    assert outcome.reason is ComparisonFailureReason.DUPLICATE_PRODUCT


# ── hydration ═══════════════════════════════════════════════════════════════


async def test_input_order_is_preserved() -> None:
    """"The first two" is how the customer referred to them."""
    result = _ok(await _compare([_row(1), _row(2), _row(3)], [3, 1]))

    assert [p.name_english for p in result.products] == ["Sofa 3", "Sofa 1"]


async def test_a_missing_product_refuses_the_whole_comparison() -> None:
    outcome = await _compare([_row(1)], [1, 2])

    assert isinstance(outcome, ComparisonUnavailable)
    assert outcome.reason is ComparisonFailureReason.PRODUCT_UNAVAILABLE


async def test_another_retailers_product_is_simply_absent() -> None:
    outcome = await _compare([_row(1), _row(2, store_id=60)], [1, 2])

    assert isinstance(outcome, ComparisonUnavailable)
    assert outcome.reason is ComparisonFailureReason.PRODUCT_UNAVAILABLE


async def test_every_read_carries_the_request_scope() -> None:
    service, repository = _service([_row(1), _row(2)])

    await service.compare([1, 2], CONTEXT)

    assert repository.calls == [CONTEXT]


# ── statuses ════════════════════════════════════════════════════════════════


async def test_equal_known_values_are_the_same() -> None:
    result = _ok(await _compare([_row(1, color="Beige"), _row(2, color="Beige")], [1, 2]))

    assert _row_for(result, ComparisonField.MAIN_COLOR).status is ComparisonStatus.SAME


async def test_differing_known_values_differ() -> None:
    result = _ok(await _compare([_row(1, color="Beige"), _row(2, color="Taupe")], [1, 2]))

    assert (
        _row_for(result, ComparisonField.MAIN_COLOR).status is ComparisonStatus.DIFFERENT
    )


async def test_two_absences_are_unknown_not_the_same() -> None:
    result = _ok(
        await _compare([_row(1, capacity=None), _row(2, capacity=None)], [1, 2])
    )

    row = _row_for(result, ComparisonField.SEATING_CAPACITY)
    assert row.status is ComparisonStatus.UNKNOWN
    assert all(not cell.known for cell in row.cells)


async def test_one_absence_makes_the_row_unknown() -> None:
    result = _ok(await _compare([_row(1, capacity=3), _row(2, capacity=None)], [1, 2]))

    assert (
        _row_for(result, ComparisonField.SEATING_CAPACITY).status
        is ComparisonStatus.UNKNOWN
    )


# ── fields ══════════════════════════════════════════════════════════════════


async def test_a_price_carries_its_currency() -> None:
    """SAR 100 and USD 100 are different values, and nothing converts."""
    result = _ok(
        await _compare(
            [_row(1, price="100.00", unit="SAR"), _row(2, price="100.00", unit="USD")],
            [1, 2],
        )
    )
    row = _row_for(result, ComparisonField.PRICE)

    assert row.status is ComparisonStatus.DIFFERENT
    assert {cell.value for cell in row.cells} == {"100.00 SAR", "100.00 USD"}


async def test_identical_prices_in_one_currency_are_the_same() -> None:
    result = _ok(await _compare([_row(1, price="100.00"), _row(2, price="100.00")], [1, 2]))

    assert _row_for(result, ComparisonField.PRICE).status is ComparisonStatus.SAME


async def test_subcategory_and_seating_are_compared() -> None:
    result = _ok(
        await _compare(
            [_row(1, subcategory="sofa", capacity=3), _row(2, subcategory="sofa", capacity=4)],
            [1, 2],
        )
    )

    assert (
        _row_for(result, ComparisonField.COMMERCE_SUBCATEGORY).status
        is ComparisonStatus.SAME
    )
    assert (
        _row_for(result, ComparisonField.SEATING_CAPACITY).status
        is ComparisonStatus.DIFFERENT
    )


async def test_styles_compare_as_a_set_not_as_typed_order() -> None:
    """Data-entry order is not a difference between products."""
    result = _ok(
        await _compare(
            [
                _row(1, styles=("Modern", "Minimalist")),
                _row(2, styles=("Minimalist", "Modern")),
            ],
            [1, 2],
        )
    )

    assert _row_for(result, ComparisonField.STYLES).status is ComparisonStatus.SAME


async def test_different_styles_differ() -> None:
    result = _ok(
        await _compare([_row(1, styles=("Modern",)), _row(2, styles=("Japandi",))], [1, 2])
    )

    assert _row_for(result, ComparisonField.STYLES).status is ComparisonStatus.DIFFERENT


async def test_unrecorded_styles_are_unknown() -> None:
    result = _ok(await _compare([_row(1, styles=()), _row(2, styles=())], [1, 2]))

    assert _row_for(result, ComparisonField.STYLES).status is ComparisonStatus.UNKNOWN


# ── measurements ════════════════════════════════════════════════════════════


def _measured(product_id: int, **kwargs: Any) -> ProductRow:
    return _row(
        product_id,
        dimensions=RawDimensions(
            length=Decimal("220"), width=Decimal("95"), height=Decimal("85"), unit="cm"
        ),
        **kwargs,
    )


async def test_a_supported_role_is_compared() -> None:
    """A sofa's along-wall span lives in `length`; the registry knows that."""
    result = _ok(await _compare([_measured(1), _measured(2)], [1, 2]))
    row = _row_for(result, ComparisonField.OVERALL_WIDTH)

    assert row.status is ComparisonStatus.SAME
    assert all(cell.value == "220 cm" for cell in row.cells)


async def test_an_unmapped_role_is_unknown_not_approximated() -> None:
    """A sectional's stored axes were never established for this role."""
    result = _ok(
        await _compare(
            [
                _measured(1, subcategory="sectional-sofa"),
                _measured(2, subcategory="sectional-sofa"),
            ],
            [1, 2],
        )
    )

    assert (
        _row_for(result, ComparisonField.OVERALL_WIDTH).status
        is ComparisonStatus.UNKNOWN
    )


async def test_an_unusable_unit_makes_a_measurement_unknown() -> None:
    result = _ok(
        await _compare(
            [
                _row(1, dimensions=RawDimensions(length=Decimal("220"), unit="cubits")),
                _row(2, dimensions=RawDimensions(length=Decimal("220"), unit="cubits")),
            ],
            [1, 2],
        )
    )

    assert (
        _row_for(result, ComparisonField.OVERALL_WIDTH).status
        is ComparisonStatus.UNKNOWN
    )


async def test_every_supported_field_appears_once() -> None:
    result = _ok(await _compare([_measured(1), _measured(2)], [1, 2]))

    assert [r.field for r in result.rows] == list(ComparisonField)


# ── grounding and neutrality ════════════════════════════════════════════════


async def test_grounding_refs_run_one_to_n_in_requested_order() -> None:
    result = _ok(await _compare([_row(1), _row(2), _row(3)], [3, 1, 2]))

    assert [p.grounding_ref for p in result.products] == [1, 2, 3]
    assert [p.name_english for p in result.products] == ["Sofa 3", "Sofa 1", "Sofa 2"]


async def test_comparison_never_manufactures_search_provenance() -> None:
    """No search returned these, so claiming an exact match would be false."""
    result = _ok(await _compare([_row(1), _row(2)], [1, 2]))

    assert all(p.relaxation_depth is None for p in result.products)
    assert all(p.matched_exactly is None for p in result.products)


async def test_comparison_never_invents_a_presentation_position() -> None:
    result = _ok(await _compare([_row(1), _row(2)], [1, 2]))

    assert all(p.presented_ordinal is None for p in result.products)


def test_the_result_carries_no_winner() -> None:
    for name in (*ProductComparisonResult.model_fields, *ComparisonField):
        rendered = str(name)
        for forbidden in ("best", "winner", "better", "score", "recommend", "rank"):
            assert forbidden not in rendered.lower()


def test_the_service_authors_no_prose() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/services/comparison.py").read_text()
    statements = source.split('"""')[-1]

    for forbidden in ("f\"The ", "recommend", "better", "you should"):
        assert forbidden not in statements
