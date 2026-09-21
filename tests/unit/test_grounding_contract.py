"""Grounding and comparison: verified facts, and honest absences.

Two mistakes the contracts make unrepresentable. A product grounded by an
older route cannot claim a position on this turn's screen. And two products
that both record nothing cannot be reported as "the same".
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.agent_turn import CustomerResponse, TurnGrounding
from app.schemas.comparison import (
    ComparisonCell,
    ComparisonField,
    ComparisonRow,
    ComparisonStatus,
    ProductComparisonResult,
)
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.grounding import (
    DroppedConstraint,
    GroundedProduct,
    RelaxationSummaryItem,
    SearchExecutionGrounding,
    SearchOutcome,
    TurnFailure,
    TurnFailureCode,
)
from app.schemas.product import CommerceClassification
from app.schemas.query import ConstraintStrength
from app.schemas.relaxation import RelaxableField, StopReason
from app.schemas.semantic import SemanticSkipReason
from app.taxonomy.dimensions import DimensionRole, UnsupportedDimensionReason
from pydantic import ValidationError


def _product(
    ref: int, *, ordinal: int | None = None, depth: int | None = None
) -> GroundedProduct:
    return GroundedProduct(
        grounding_ref=ref,
        presented_ordinal=ordinal,
        name_english=f"Sofa {ref}",
        price_amount=Decimal("2450.00"),
        price_unit="SAR",
        image_url=f"https://example.test/{ref}.jpg",
        product_url=f"https://example.test/{ref}",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
        relaxation_depth=depth,
    )


def _grounding(count: int, **overrides: object) -> SearchExecutionGrounding:
    products = tuple(_product(i, ordinal=i, depth=0) for i in range(1, count + 1))
    values: dict[str, object] = {
        "outcome": SearchOutcome.RESULTS if count else SearchOutcome.ZERO_RESULTS,
        "products": products,
        "eligible_count": max(count, 1),
        "ranked_count": max(count, 1),
        "selected_count": count,
        "presented_count": count,
        "exact_candidate_count": count,
        "stop_reason": StopReason.EXACT_SUFFICIENT,
    }
    values.update(overrides)
    return SearchExecutionGrounding(**values)


# ── grounding handles ───────────────────────────────────────────────────────


def test_a_grounding_ref_is_always_present() -> None:
    assert GroundedProduct.model_fields["grounding_ref"].is_required()


def test_a_presented_ordinal_is_optional() -> None:
    """"What did the one I selected cost?" grounds a product with no position."""
    product = _product(1)

    assert product.grounding_ref == 1
    assert product.presented_ordinal is None


def test_an_older_product_gets_no_invented_position() -> None:
    detail = TurnGrounding(product_detail=_product(1))

    assert detail.product_detail is not None
    assert detail.product_detail.presented_ordinal is None


def test_refs_are_one_to_n_in_order() -> None:
    with pytest.raises(ValidationError, match="in order"):
        SearchExecutionGrounding(
            outcome=SearchOutcome.RESULTS,
            products=(_product(1, depth=0), _product(7, depth=0)),
            eligible_count=2,
            ranked_count=2,
            selected_count=2,
            presented_count=2,
            exact_candidate_count=2,
            stop_reason=StopReason.EXACT_SUFFICIENT,
        )


def test_exact_matching_is_derived_from_the_depth() -> None:
    """One fact, so two fields cannot disagree about it."""
    assert _product(1, depth=0).matched_exactly is True
    assert _product(1, depth=2).matched_exactly is False
    assert "matched_exactly" not in GroundedProduct.model_fields


# ── the counts tell the truth ───────────────────────────────────────────────


def test_presented_count_matches_the_products_carried() -> None:
    with pytest.raises(ValidationError, match="presented_count"):
        _grounding(2, presented_count=5)


def test_a_zero_result_search_carries_no_products() -> None:
    grounding = SearchExecutionGrounding(
        outcome=SearchOutcome.ZERO_RESULTS,
        eligible_count=0,
        ranked_count=0,
        selected_count=0,
        presented_count=0,
        exact_candidate_count=0,
        stop_reason=StopReason.POLICY_EXHAUSTED,
    )

    assert grounding.products == ()


def test_results_and_zero_results_cannot_be_confused() -> None:
    with pytest.raises(ValidationError, match="zero-result outcome"):
        SearchExecutionGrounding(
            outcome=SearchOutcome.ZERO_RESULTS,
            products=(_product(1, ordinal=1, depth=0),),
            eligible_count=1,
            ranked_count=1,
            selected_count=1,
            presented_count=1,
            exact_candidate_count=1,
            stop_reason=StopReason.EXACT_SUFFICIENT,
        )


def test_presentation_truncation_is_not_eligibility_truncation() -> None:
    """Named so the two can never be read as the same number."""
    grounding = _grounding(
        3, eligible_count=173, ranked_count=173
    )

    assert grounding.eligible_count == 173
    assert grounding.ranked_count == 173
    assert grounding.presented_count == 3
    assert grounding.truncated_for_presentation is True


def test_more_cannot_be_presented_than_ranked() -> None:
    with pytest.raises(ValidationError, match="counts must narrow"):
        _grounding(3, ranked_count=2, eligible_count=2, selected_count=3)


def test_ranking_considers_every_eligible_product() -> None:
    """Ranking decides order, never eligibility, so the two counts agree.

    A smaller ranked count would mean candidates were lost between the catalog
    and the ordering - the failure M11B-1 removed, arriving by another route.
    """
    with pytest.raises(ValidationError, match="every eligible product"):
        _grounding(1, ranked_count=2, eligible_count=9)
    with pytest.raises(ValidationError, match="every eligible product"):
        _grounding(1, ranked_count=9, eligible_count=2)


def test_ranking_either_happened_or_was_skipped() -> None:
    with pytest.raises(ValidationError, match="not both"):
        _grounding(
            1,
            semantic_used=True,
            semantic_skip_reason=SemanticSkipReason.NOT_CONFIGURED,
        )


def test_a_degraded_ranking_records_why() -> None:
    grounding = _grounding(
        1, semantic_used=False, semantic_skip_reason=SemanticSkipReason.INDEX_UNAVAILABLE
    )

    assert grounding.semantic_skip_reason is SemanticSkipReason.INDEX_UNAVAILABLE


def test_relaxation_is_recorded_or_not_claimed() -> None:
    with pytest.raises(ValidationError, match="was_relaxed"):
        _grounding(1, was_relaxed=True)


def test_a_widened_bound_explains_itself() -> None:
    item = RelaxationSummaryItem(
        field=RelaxableField.PRICE_MAX,
        strength=ConstraintStrength.APPROXIMATE,
        original_value="5000",
        applied_value="5500",
    )

    assert item.role is None


def test_a_widened_measurement_names_which_one() -> None:
    with pytest.raises(ValidationError, match="which one"):
        RelaxationSummaryItem(
            field=RelaxableField.DIMENSION,
            strength=ConstraintStrength.APPROXIMATE,
            original_value="220",
            applied_value="231",
        )


def test_a_dropped_constraint_carries_its_reason() -> None:
    dropped = DroppedConstraint(
        role=DimensionRole.OVERALL_WIDTH,
        reason=UnsupportedDimensionReason.ROLE_NOT_DEFINED,
    )

    assert dropped.reason is UnsupportedDimensionReason.ROLE_NOT_DEFINED


# ── comparison ──────────────────────────────────────────────────────────────


def _row(field: ComparisonField, *values: str | None) -> ComparisonRow:
    cells = tuple(
        ComparisonCell(known=v is not None, value=v) for v in values
    )
    if any(v is None for v in values):
        status = ComparisonStatus.UNKNOWN
    elif len(set(values)) == 1:
        status = ComparisonStatus.SAME
    else:
        status = ComparisonStatus.DIFFERENT
    return ComparisonRow(field=field, cells=cells, status=status)


def test_identical_known_values_are_the_same() -> None:
    row = _row(ComparisonField.MAIN_COLOR, "Beige", "Beige")

    assert row.status is ComparisonStatus.SAME


def test_differing_known_values_differ() -> None:
    row = _row(ComparisonField.MAIN_COLOR, "Beige", "Taupe")

    assert row.status is ComparisonStatus.DIFFERENT


def test_two_absences_are_unknown_not_the_same() -> None:
    """Neither product records a capacity; nobody has established anything."""
    row = _row(ComparisonField.SEATING_CAPACITY, None, None)

    assert row.status is ComparisonStatus.UNKNOWN


def test_one_absence_makes_the_whole_row_unknown() -> None:
    row = _row(ComparisonField.SEATING_CAPACITY, "3", None)

    assert row.status is ComparisonStatus.UNKNOWN


def test_unknown_cannot_be_claimed_as_same() -> None:
    """The status is checked against the cells, so it cannot be asserted."""
    with pytest.raises(ValidationError, match="status must be"):
        ComparisonRow(
            field=ComparisonField.SEATING_CAPACITY,
            cells=(ComparisonCell(known=False), ComparisonCell(known=False)),
            status=ComparisonStatus.SAME,
        )


def test_difference_cannot_be_claimed_over_equal_values() -> None:
    with pytest.raises(ValidationError, match="status must be"):
        ComparisonRow(
            field=ComparisonField.MAIN_COLOR,
            cells=(
                ComparisonCell(known=True, value="Beige"),
                ComparisonCell(known=True, value="Beige"),
            ),
            status=ComparisonStatus.DIFFERENT,
        )


def test_there_is_no_boolean_shortcut_for_difference() -> None:
    assert "differs" not in ComparisonRow.model_fields
    assert set(ComparisonStatus) == {
        ComparisonStatus.SAME,
        ComparisonStatus.DIFFERENT,
        ComparisonStatus.UNKNOWN,
    }


def test_an_unknown_cell_carries_no_value() -> None:
    with pytest.raises(ValidationError, match="cannot carry a value"):
        ComparisonCell(known=False, value="3")


def test_a_comparison_needs_at_least_two_products() -> None:
    with pytest.raises(ValidationError, match="at least 2 products"):
        ProductComparisonResult(products=(_product(1),))


def test_every_row_has_a_cell_per_product() -> None:
    with pytest.raises(ValidationError, match="one cell per compared product"):
        ProductComparisonResult(
            products=(_product(1), _product(2), _product(3)),
            rows=(_row(ComparisonField.MAIN_COLOR, "Beige", "Taupe"),),
        )


def test_input_order_is_preserved() -> None:
    """"The first two" is how the customer referred to them."""
    result = ProductComparisonResult(products=(_product(2), _product(1)))

    assert [p.grounding_ref for p in result.products] == [2, 1]


def test_material_is_not_a_comparable_field() -> None:
    """The catalog cannot answer it, so the contract does not offer it."""
    assert "material" not in {f.value for f in ComparisonField}


def test_measurements_are_compared_by_role_not_column() -> None:
    fields = {f.value for f in ComparisonField}

    assert "overall_width" in fields
    assert "width" not in fields


# ── the response contract ───────────────────────────────────────────────────


def test_a_response_cites_handles_not_products() -> None:
    response = CustomerResponse(
        message="Both work well in a small room.",
        referenced_grounding_refs=(1, 2),
    )

    assert response.referenced_grounding_refs == (1, 2)
    assert "product_id" not in CustomerResponse.model_fields


def test_a_response_cannot_cite_the_same_product_twice() -> None:
    with pytest.raises(ValidationError, match="same product twice"):
        CustomerResponse(message="hi", referenced_grounding_refs=(1, 1))


def test_a_response_carries_at_most_one_follow_up() -> None:
    annotation = str(CustomerResponse.model_fields["follow_up_question"].annotation)

    assert "tuple" not in annotation and "list" not in annotation


def test_a_response_states_no_price_field() -> None:
    """Facts are rendered from grounding, never authored by the model."""
    for forbidden in ("price", "dimension", "name", "url", "availability"):
        assert not any(forbidden in f for f in CustomerResponse.model_fields)


# ── the turn envelope ───────────────────────────────────────────────────────


def test_a_turn_can_ground_nothing_at_all() -> None:
    grounding = TurnGrounding()

    assert grounding.search is None
    assert grounding.failure is None


def test_a_failure_is_a_code_not_a_message() -> None:
    """An exception's text may echo a prompt, a DSN or a provider's internals."""
    failure = TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE)

    assert set(TurnFailure.model_fields) == {"code"}
    assert failure.code.value == "search_unavailable"


# ── unknown search provenance stays unknown ─────────────────────────────────


def test_a_product_may_have_no_known_relaxation_depth() -> None:
    """A comparison or a detail question grounds a product no search returned."""
    product = GroundedProduct(
        grounding_ref=1,
        name_english="Sofa",
        price_amount=Decimal("2450.00"),
        price_unit="SAR",
        image_url="https://example.test/1.jpg",
        product_url="https://example.test/1",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
    )

    assert product.relaxation_depth is None


def test_unknown_depth_makes_exactness_unknown_not_false() -> None:
    """False would claim it needed widening; True would claim it did not."""
    assert _product(1).matched_exactly is None


def test_a_zero_depth_still_means_an_exact_match() -> None:
    assert _product(1, depth=0).matched_exactly is True


def test_a_widened_depth_still_means_an_inexact_match() -> None:
    assert _product(1, depth=2).matched_exactly is False


def test_exactness_is_never_stored_alongside_the_depth() -> None:
    """One fact, so two fields cannot disagree about it."""
    assert "matched_exactly" not in GroundedProduct.model_fields
    assert isinstance(GroundedProduct.__dict__["matched_exactly"], property)


def test_a_searched_product_must_know_its_provenance() -> None:
    """Losing it would mean the pipeline discarded what M8 established."""
    unknown = GroundedProduct(
        grounding_ref=1,
        presented_ordinal=1,
        name_english="Sofa",
        price_amount=Decimal("2450.00"),
        price_unit="SAR",
        image_url="https://example.test/1.jpg",
        product_url="https://example.test/1",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
    )

    with pytest.raises(ValidationError, match="became eligible"):
        SearchExecutionGrounding(
            outcome=SearchOutcome.RESULTS,
            products=(unknown,),
            eligible_count=1,
            ranked_count=1,
            selected_count=1,
            presented_count=1,
            exact_candidate_count=1,
            stop_reason=StopReason.EXACT_SUFFICIENT,
        )


def test_search_grounding_accepts_verified_depths() -> None:
    grounding = SearchExecutionGrounding(
        outcome=SearchOutcome.RESULTS,
        products=(_product(1, ordinal=1, depth=0), _product(2, ordinal=2, depth=2)),
        eligible_count=2,
        ranked_count=2,
        selected_count=2,
        presented_count=2,
        exact_candidate_count=1,
        stop_reason=StopReason.TARGET_REACHED,
    )

    assert [p.matched_exactly for p in grounding.products] == [True, False]


def test_a_comparison_may_carry_products_of_unknown_provenance() -> None:
    """Re-reading a price cannot reconstruct which widening once found it."""
    result = ProductComparisonResult(products=(_product(1), _product(2)))

    assert all(p.relaxation_depth is None for p in result.products)
    assert all(p.matched_exactly is None for p in result.products)


def test_a_detail_product_may_carry_unknown_provenance() -> None:
    grounding = TurnGrounding(product_detail=_product(1))

    assert grounding.product_detail is not None
    assert grounding.product_detail.matched_exactly is None


def test_provenance_and_presentation_position_stay_independent() -> None:
    """A current result knows both; an old selection knows neither."""
    current = _product(1, ordinal=1, depth=0)
    remembered = _product(1)

    assert (current.presented_ordinal, current.relaxation_depth) == (1, 0)
    assert (remembered.presented_ordinal, remembered.relaxation_depth) == (None, None)


def test_exactness_is_never_inferred_from_anything_else() -> None:
    """Not from focus, selection, an ordinal, or a successful hydration."""

    import inspect

    accessor = GroundedProduct.__dict__["matched_exactly"].fget
    assert accessor is not None
    body = inspect.getsource(accessor)
    statements = "".join(
        line for line in body.splitlines(keepends=True) if '"""' not in line
    ).split('"""')[-1]

    assert "relaxation_depth" in statements
    for forbidden in ("ordinal", "selected", "focus", "commerce", "price"):
        assert forbidden not in statements, forbidden
