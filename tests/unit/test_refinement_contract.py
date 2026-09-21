"""SET, CLEAR and preserve-by-omission.

The distinction the contract has to carry: "I didn't mention style" and "I
don't care about style" must not look the same. Without an explicit clear,
the second is unrepresentable, and a customer default would silently survive
a request to drop it.
"""

from __future__ import annotations

import pytest
from app.schemas.discovery import DimensionConstraintKind, ProductSort
from app.schemas.query import ConstraintStrength
from app.schemas.refinement import (
    AttributeRefinement,
    AttributeRefinementOp,
    CapacityRefinement,
    DimensionRefinement,
    PlanarRefinement,
    PriceRefinement,
    ProposedAttributeValue,
    RefinementOp,
    SearchRefinementDelta,
    SemanticIntentOp,
    SemanticIntentRefinement,
    SortRefinement,
)
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.dimensions import DimensionRole
from pydantic import ValidationError

SET = RefinementOp.SET
CLEAR = RefinementOp.CLEAR
WIDTH = DimensionRole.OVERALL_WIDTH
DEPTH = DimensionRole.DEPTH


def _width(op: RefinementOp = SET, **kwargs: object) -> DimensionRefinement:
    if op is CLEAR:
        return DimensionRefinement(op=CLEAR, role=WIDTH)
    payload: dict[str, object] = {
        "kind": DimensionConstraintKind.MAX,
        "max_value": "220",
        "unit": "cm",
    }
    payload.update(kwargs)
    return DimensionRefinement(op=SET, role=WIDTH, **payload)


# ── omission preserves ──────────────────────────────────────────────────────


def test_an_empty_delta_changes_nothing() -> None:
    delta = SearchRefinementDelta()

    assert delta.is_empty()
    assert all(
        getattr(delta, f) in (None, ()) for f in SearchRefinementDelta.model_fields
    )


def test_omitting_an_axis_is_structurally_distinct_from_clearing_it() -> None:
    omitted = SearchRefinementDelta(
        price=PriceRefinement(op=SET, max_amount="3000", currency="SAR")
    )
    cleared = SearchRefinementDelta(
        price=PriceRefinement(op=SET, max_amount="3000", currency="SAR"),
        attributes=(
            AttributeRefinement(op=AttributeRefinementOp.CLEAR, family=AttributeFamily.STYLE),
        ),
    )

    assert omitted.attributes == ()
    assert cleared.attributes[0].op is AttributeRefinementOp.CLEAR
    assert omitted != cleared


def test_a_delta_that_only_clears_is_not_empty() -> None:
    """"Any style is fine" changes the search even though it adds nothing."""
    delta = SearchRefinementDelta(
        attributes=(
            AttributeRefinement(op=AttributeRefinementOp.CLEAR, family=AttributeFamily.STYLE),
        )
    )

    assert not delta.is_empty()


# ── price ───────────────────────────────────────────────────────────────────


def test_a_price_set_needs_a_bound() -> None:
    with pytest.raises(ValidationError, match="at least one bound"):
        PriceRefinement(op=SET, currency="SAR")


def test_a_price_clear_carries_no_payload() -> None:
    with pytest.raises(ValidationError, match="must not carry"):
        PriceRefinement(op=CLEAR, max_amount="3000")


def test_an_absent_currency_means_inherit_not_invent() -> None:
    refinement = PriceRefinement(op=SET, max_amount="3000")

    assert refinement.currency is None


def test_exclusivity_needs_the_bound_it_excludes() -> None:
    with pytest.raises(ValidationError, match="min_exclusive"):
        PriceRefinement(op=SET, max_amount="3000", min_exclusive=True)


def test_strict_cheaper_is_expressible() -> None:
    refinement = PriceRefinement(op=SET, max_amount="5000", max_exclusive=True)

    assert refinement.max_exclusive is True


def test_amounts_stay_strings() -> None:
    assert str(PriceRefinement.model_fields["max_amount"].annotation) == "str | None"


# ── capacity and sort ───────────────────────────────────────────────────────


def test_a_capacity_set_needs_a_bound() -> None:
    with pytest.raises(ValidationError, match="at least one bound"):
        CapacityRefinement(op=SET)


def test_a_capacity_clear_carries_no_payload() -> None:
    with pytest.raises(ValidationError, match="must not carry"):
        CapacityRefinement(op=CLEAR, min_capacity=3)


def test_a_sort_set_needs_a_value() -> None:
    with pytest.raises(ValidationError, match="needs a value"):
        SortRefinement(op=SET)


def test_a_sort_clear_carries_no_value() -> None:
    with pytest.raises(ValidationError, match="must not carry"):
        SortRefinement(op=CLEAR, value=ProductSort.PRICE_ASC)


# ── dimensions ──────────────────────────────────────────────────────────────


def test_one_operation_per_role_is_accepted() -> None:
    delta = SearchRefinementDelta(
        dimensions=(_width(), DimensionRefinement(op=CLEAR, role=DEPTH))
    )

    assert {d.role for d in delta.dimensions} == {WIDTH, DEPTH}


def test_setting_and_clearing_the_same_role_is_refused() -> None:
    """There is no correct reading, and tuple order must decide nothing."""
    with pytest.raises(ValidationError, match="one operation per dimension role"):
        SearchRefinementDelta(dimensions=(_width(), _width(CLEAR)))


def test_two_sets_for_the_same_role_are_refused() -> None:
    with pytest.raises(ValidationError, match="one operation per dimension role"):
        SearchRefinementDelta(dimensions=(_width(), _width(max_value="200")))


def test_a_clear_still_names_its_measurement() -> None:
    with pytest.raises(ValidationError):
        DimensionRefinement(op=CLEAR)  # type: ignore[call-arg]


def test_a_dimension_set_needs_a_kind() -> None:
    with pytest.raises(ValidationError, match="needs a kind"):
        DimensionRefinement(op=SET, role=WIDTH, max_value="220")


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        (DimensionConstraintKind.MIN, "min_value"),
        (DimensionConstraintKind.MAX, "max_value"),
        (DimensionConstraintKind.TARGET, "target_value"),
    ],
)
def test_each_kind_carries_exactly_its_own_value(
    kind: DimensionConstraintKind, field: str
) -> None:
    accepted = DimensionRefinement(op=SET, role=WIDTH, kind=kind, **{field: "220"})

    assert getattr(accepted, field) == "220"
    with pytest.raises(ValidationError, match="must not set"):
        DimensionRefinement(
            op=SET, role=WIDTH, kind=kind, **{field: "220"}, **_other(field)
        )


def _other(field: str) -> dict[str, str]:
    remaining = {"min_value", "max_value", "target_value"} - {field}
    return {sorted(remaining)[0]: "100"}


def test_a_range_needs_both_bounds() -> None:
    with pytest.raises(ValidationError, match="needs max_value"):
        DimensionRefinement(
            op=SET, role=WIDTH, kind=DimensionConstraintKind.RANGE, min_value="180"
        )


def test_an_absent_unit_means_inherit_not_centimetres() -> None:
    refinement = DimensionRefinement(
        op=SET, role=WIDTH, kind=DimensionConstraintKind.MAX, max_value="210"
    )

    assert refinement.unit is None


def test_a_planar_set_needs_both_sides() -> None:
    with pytest.raises(ValidationError, match="both sides"):
        PlanarRefinement(op=SET, first_value="200")


# ── colour and style ────────────────────────────────────────────────────────


def test_an_attribute_operation_records_requirement_or_preference() -> None:
    """Ordinary wanting is a leaning; only strict wording is a filter."""
    assert {o.value for o in AttributeRefinementOp} == {
        "set_requirement",
        "set_preference",
        "clear",
    }


def test_one_operation_per_family_is_accepted() -> None:
    delta = SearchRefinementDelta(
        attributes=(
            AttributeRefinement(
                op=AttributeRefinementOp.SET_PREFERENCE,
                family=AttributeFamily.COLOR,
                values=(ProposedAttributeValue(raw_value="beige"),),
            ),
            AttributeRefinement(
                op=AttributeRefinementOp.CLEAR, family=AttributeFamily.STYLE
            ),
        )
    )

    assert len(delta.attributes) == 2


def test_two_operations_on_one_family_are_refused() -> None:
    with pytest.raises(ValidationError, match="one operation per attribute family"):
        SearchRefinementDelta(
            attributes=(
                AttributeRefinement(
                    op=AttributeRefinementOp.SET_PREFERENCE,
                    family=AttributeFamily.COLOR,
                    values=(ProposedAttributeValue(raw_value="beige"),),
                ),
                AttributeRefinement(
                    op=AttributeRefinementOp.CLEAR, family=AttributeFamily.COLOR
                ),
            )
        )


def test_a_clear_carries_no_values() -> None:
    with pytest.raises(ValidationError, match="must not carry values"):
        AttributeRefinement(
            op=AttributeRefinementOp.CLEAR,
            family=AttributeFamily.STYLE,
            values=(ProposedAttributeValue(raw_value="Modern"),),
        )


def test_a_set_needs_a_value() -> None:
    with pytest.raises(ValidationError, match="at least one value"):
        AttributeRefinement(
            op=AttributeRefinementOp.SET_REQUIREMENT, family=AttributeFamily.COLOR
        )


def test_an_out_of_vocabulary_value_is_preserved_verbatim() -> None:
    """"warm neutral" is not Beige; the canonical value stays unset."""
    value = ProposedAttributeValue(raw_value="warm neutral")

    assert value.canonical_value is None


def test_several_values_in_one_operation_replace_the_axis() -> None:
    refinement = AttributeRefinement(
        op=AttributeRefinementOp.SET_REQUIREMENT,
        family=AttributeFamily.STYLE,
        values=(
            ProposedAttributeValue(raw_value="Modern", canonical_value="Modern"),
            ProposedAttributeValue(raw_value="Japandi", canonical_value="Japandi"),
        ),
    )

    assert len(refinement.values) == 2


def test_a_repeated_value_is_refused() -> None:
    with pytest.raises(ValidationError, match="repeat a value"):
        AttributeRefinement(
            op=AttributeRefinementOp.SET_PREFERENCE,
            family=AttributeFamily.COLOR,
            values=(
                ProposedAttributeValue(raw_value="Beige"),
                ProposedAttributeValue(raw_value="beige"),
            ),
        )


# ── semantic intent ─────────────────────────────────────────────────────────


def test_semantic_intent_can_be_set_cleared_or_omitted() -> None:
    assert SemanticIntentRefinement(op=SemanticIntentOp.SET, value="cosy").value == "cosy"
    assert SemanticIntentRefinement(op=SemanticIntentOp.CLEAR).value is None
    assert SearchRefinementDelta().semantic_intent is None


def test_a_set_intent_needs_words() -> None:
    with pytest.raises(ValidationError, match="needs a semantic intent"):
        SemanticIntentRefinement(op=SemanticIntentOp.SET, value="   ")


def test_a_cleared_intent_carries_no_words() -> None:
    with pytest.raises(ValidationError, match="must not carry a value"):
        SemanticIntentRefinement(op=SemanticIntentOp.CLEAR, value="cosy")


def test_intent_is_bounded_so_a_transcript_cannot_be_smuggled() -> None:
    with pytest.raises(ValidationError):
        SemanticIntentRefinement(op=SemanticIntentOp.SET, value="x" * 201)


def test_strength_travels_with_the_operation() -> None:
    refinement = PriceRefinement(
        op=SET,
        max_amount="3000",
        currency="SAR",
        max_strength=ConstraintStrength.APPROXIMATE,
    )

    assert refinement.max_strength is ConstraintStrength.APPROXIMATE
