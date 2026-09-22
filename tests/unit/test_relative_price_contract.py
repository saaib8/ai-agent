"""Relative prices, and the bound the model must not compute.

"20% cheaper than the second one" names no amount. The figure depends on what
that product costs right now, which is a catalog fact - so the contract lets
the model state the *relation* and point at the product, and nothing else.
The application resolves the reference, re-reads the price and does the
arithmetic (CLAUDE.md 3.3).

The two things that must stay impossible: the model naming a product, and the
model naming the reference price.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.agent_decision import (
    MAX_COMPARISON_REFERENCES,
    MIN_COMPARISON_REFERENCES,
    AgentAction,
    CustomerAgentDecision,
)
from app.schemas.product_reference import (
    ExtremumDirection,
    FocusedProduct,
    PresentedExtremum,
    PresentedOrdinal,
    ProductReferenceSelector,
    SoleSelectedProduct,
)
from app.schemas.refinement import (
    MAX_PRICE_PERCENT,
    PriceRefinement,
    PriceRefinementOp,
    PriceRelation,
    RelativePriceRefinement,
    SearchRefinementDelta,
)
from pydantic import ValidationError

BARE = (PriceRelation.CHEAPER_THAN, PriceRelation.MORE_EXPENSIVE_THAN)
PERCENTAGE = (
    PriceRelation.PERCENT_CHEAPER,
    PriceRelation.MORE_THAN_PERCENT_CHEAPER,
    PriceRelation.PERCENT_MORE_EXPENSIVE,
    PriceRelation.MORE_THAN_PERCENT_MORE_EXPENSIVE,
)


def _relative(
    relation: PriceRelation,
    *,
    reference: ProductReferenceSelector | None = None,
    percent: str | None = None,
) -> PriceRefinement:
    return PriceRefinement(
        op=PriceRefinementOp.SET_RELATIVE,
        relative=RelativePriceRefinement(
            relation=relation,
            reference=reference or PresentedOrdinal(position=2),
            percent=percent,
        ),
    )


def _decision(price: PriceRefinement) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.REFINE_SEARCH,
        refinement=SearchRefinementDelta(price=price),
    )


# ── every locked relation is representable ──────────────────────────────────


@pytest.mark.parametrize("relation", BARE)
def test_a_bare_comparative_needs_no_figure(relation: PriceRelation) -> None:
    """"show me cheaper ones than this" / "more expensive than this"."""
    decision = _decision(_relative(relation, reference=FocusedProduct()))

    price = decision.refinement.price  # type: ignore[union-attr]
    assert price is not None and price.relative is not None
    assert price.relative.relation is relation
    assert price.relative.percent is None


@pytest.mark.parametrize("relation", PERCENTAGE)
def test_a_percentage_relation_carries_the_stated_figure(
    relation: PriceRelation,
) -> None:
    refinement = _relative(relation, percent="20")

    assert refinement.relative is not None
    assert refinement.relative.percent_value == Decimal("20")


def test_the_worked_example_is_representable() -> None:
    """"20% cheaper than the second one." """
    decision = _decision(
        _relative(
            PriceRelation.PERCENT_CHEAPER,
            reference=PresentedOrdinal(position=2),
            percent="20",
        )
    )

    assert decision.model_dump(mode="json", exclude_none=True, exclude_defaults=True) == {
        "action": "refine_search",
        "refinement": {
            "price": {
                "op": "set_relative",
                "relative": {
                    "relation": "percent_cheaper",
                    "reference": {"position": 2},
                    "percent": "20",
                },
            }
        },
    }


def test_strictness_is_carried_by_the_relation_not_a_flag() -> None:
    """A bare comparative is strict; a stated percentage is a threshold."""
    assert PriceRelation.CHEAPER_THAN.needs_percent is False
    assert PriceRelation.MORE_THAN_PERCENT_CHEAPER.needs_percent is True
    assert PriceRelation.PERCENT_CHEAPER.is_cheaper is True
    assert PriceRelation.PERCENT_MORE_EXPENSIVE.is_cheaper is False


@pytest.mark.parametrize(
    "reference",
    [
        PresentedOrdinal(position=2),
        FocusedProduct(),
        SoleSelectedProduct(),
        PresentedExtremum(direction=ExtremumDirection.LOWEST),
    ],
)
def test_any_safe_selector_can_anchor_a_comparison(
    reference: ProductReferenceSelector,
) -> None:
    refinement = _relative(PriceRelation.CHEAPER_THAN, reference=reference)

    assert refinement.relative is not None
    assert refinement.relative.reference == reference


# ── what the model may not supply ───────────────────────────────────────────


def test_the_reference_is_a_selector_never_a_product_id() -> None:
    assert "product_id" not in RelativePriceRefinement.model_fields
    with pytest.raises(ValidationError):
        RelativePriceRefinement(
            relation=PriceRelation.CHEAPER_THAN, reference={"product_id": 165645}
        )


def test_no_authoritative_reference_price_can_be_stated() -> None:
    """The bound depends on a fact only PostgreSQL holds."""
    for name in RelativePriceRefinement.model_fields:
        assert "price" not in name and "amount" not in name


def test_a_relative_operation_cannot_also_state_a_bound() -> None:
    """Stating one would be the model doing the arithmetic it must not do."""
    with pytest.raises(ValidationError, match="must not carry max_amount"):
        PriceRefinement(
            op=PriceRefinementOp.SET_RELATIVE,
            relative=RelativePriceRefinement(
                relation=PriceRelation.CHEAPER_THAN,
                reference=PresentedOrdinal(position=2),
            ),
            max_amount="4000",
        )


def test_a_relative_operation_ignores_exclusivity_rather_than_failing() -> None:
    """Strictness comes from the relation, so a flag beside it says nothing.

    It used to raise. The provider's strict schema sends both booleans on every
    price refinement, and a relative operation carries no amount for either to
    qualify - so the flag could never have changed anything, and refusing it
    only cost the customer the turn.
    """
    refinement = PriceRefinement(
        op=PriceRefinementOp.SET_RELATIVE,
        relative=RelativePriceRefinement(
            relation=PriceRelation.CHEAPER_THAN,
            reference=PresentedOrdinal(position=2),
        ),
        max_exclusive=True,
    )

    assert refinement.max_exclusive is False
    assert refinement.relative is not None


def test_a_relative_operation_still_refuses_an_amount() -> None:
    """The check that matters is untouched: a bound stated here would be the
    model doing arithmetic that belongs to services with real prices."""
    with pytest.raises(ValidationError, match="relative price"):
        PriceRefinement(
            op=PriceRefinementOp.SET_RELATIVE,
            relative=RelativePriceRefinement(
                relation=PriceRelation.CHEAPER_THAN,
                reference=PresentedOrdinal(position=2),
            ),
            max_amount="4000",
        )


def test_there_is_only_one_reference_for_a_relative_price() -> None:
    """A second one elsewhere on the decision could disagree with this."""
    with pytest.raises(ValidationError, match="may not carry a product reference"):
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            refinement=SearchRefinementDelta(
                price=_relative(PriceRelation.CHEAPER_THAN)
            ),
            reference=PresentedOrdinal(position=3),
        )


# ── percentage validation ───────────────────────────────────────────────────


@pytest.mark.parametrize("relation", PERCENTAGE)
def test_a_percentage_relation_requires_the_percentage(
    relation: PriceRelation,
) -> None:
    with pytest.raises(ValidationError, match="needs the percentage"):
        _relative(relation)


@pytest.mark.parametrize("relation", BARE)
def test_a_bare_relation_rejects_a_percentage(relation: PriceRelation) -> None:
    """"cheaper" is not 20% cheaper; inventing a figure is forbidden."""
    with pytest.raises(ValidationError, match="takes no percentage"):
        _relative(relation, percent="20")


@pytest.mark.parametrize("percent", ["0", "-5"])
def test_a_percentage_must_be_positive(percent: str) -> None:
    with pytest.raises(ValidationError, match="greater than zero"):
        _relative(PriceRelation.PERCENT_CHEAPER, percent=percent)


@pytest.mark.parametrize("percent", ["100", "150"])
def test_nothing_can_be_a_hundred_percent_cheaper(percent: str) -> None:
    """That is free, and beyond it is a negative price."""
    with pytest.raises(ValidationError, match="100% or more cheaper"):
        _relative(PriceRelation.PERCENT_CHEAPER, percent=percent)


def test_more_expensive_may_exceed_a_hundred_percent() -> None:
    refinement = _relative(PriceRelation.PERCENT_MORE_EXPENSIVE, percent="150")

    assert refinement.relative is not None
    assert refinement.relative.percent_value == Decimal("150")


def test_a_percentage_is_bounded() -> None:
    with pytest.raises(ValidationError, match="may not exceed"):
        _relative(
            PriceRelation.PERCENT_MORE_EXPENSIVE,
            percent=str(MAX_PRICE_PERCENT + 1),
        )


def test_a_percentage_that_is_not_a_figure_is_refused() -> None:
    with pytest.raises(ValidationError, match="decimal figure"):
        _relative(PriceRelation.PERCENT_CHEAPER, percent="twenty")


def test_a_fractional_percentage_survives_as_a_decimal() -> None:
    """A string, so it never passes through a binary float."""
    refinement = _relative(PriceRelation.PERCENT_CHEAPER, percent="12.5")

    assert refinement.relative is not None
    assert refinement.relative.percent_value == Decimal("12.5")


# ── the absolute and clear branches still behave ────────────────────────────


def test_an_absolute_operation_carries_no_relation() -> None:
    with pytest.raises(ValidationError, match="carries no relation"):
        PriceRefinement(
            op=PriceRefinementOp.SET,
            max_amount="4000",
            currency="SAR",
            relative=RelativePriceRefinement(
                relation=PriceRelation.CHEAPER_THAN,
                reference=PresentedOrdinal(position=2),
            ),
        )


def test_a_clear_carries_no_relation() -> None:
    with pytest.raises(ValidationError, match="carries no relation"):
        PriceRefinement(
            op=PriceRefinementOp.CLEAR,
            relative=RelativePriceRefinement(
                relation=PriceRelation.CHEAPER_THAN,
                reference=PresentedOrdinal(position=2),
            ),
        )


def test_an_absolute_bound_is_still_expressible() -> None:
    refinement = PriceRefinement(
        op=PriceRefinementOp.SET, max_amount="3000", currency="SAR"
    )

    assert refinement.relative is None
    assert refinement.max_amount == "3000"


# ── comparison hard cap ─────────────────────────────────────────────────────


def _ordinals(count: int) -> tuple[ProductReferenceSelector, ...]:
    return tuple(PresentedOrdinal(position=i) for i in range(1, count + 1))


@pytest.mark.parametrize("count", [2, 3, 4])
def test_a_comparison_of_up_to_four_is_accepted(count: int) -> None:
    decision = CustomerAgentDecision(
        action=AgentAction.COMPARE, comparison_references=_ordinals(count)
    )

    assert len(decision.comparison_references) == count


@pytest.mark.parametrize("count", [5, 9])
def test_a_longer_comparison_is_refused_by_the_schema(count: int) -> None:
    """Configuration may choose a smaller maximum; nothing may exceed this."""
    with pytest.raises(ValidationError, match="at most 4 products"):
        CustomerAgentDecision(
            action=AgentAction.COMPARE, comparison_references=_ordinals(count)
        )


def test_a_comparison_of_one_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least 2"):
        CustomerAgentDecision(
            action=AgentAction.COMPARE, comparison_references=_ordinals(1)
        )


def test_duplicate_comparison_targets_are_refused() -> None:
    with pytest.raises(ValidationError, match="repeat a reference"):
        CustomerAgentDecision(
            action=AgentAction.COMPARE,
            comparison_references=(
                PresentedOrdinal(position=1),
                PresentedOrdinal(position=1),
            ),
        )


def test_the_bounds_are_named_not_scattered() -> None:
    assert MIN_COMPARISON_REFERENCES == 2
    assert MAX_COMPARISON_REFERENCES == 4
