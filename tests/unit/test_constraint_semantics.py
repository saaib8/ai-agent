"""Constraint semantics: what the customer committed to, kept out of execution."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from app.integrations.llm import StructuredLLMClient
from app.schemas.discovery import ProductSearchRequest
from app.schemas.query import (
    ClarificationReason,
    ClarificationRequired,
    CommerceInterpretation,
    ConstraintSemantics,
    ConstraintStrength,
    RequirementFamily,
    ResolvedSearch,
    UnsupportedRequirement,
)
from app.services.query_understanding import QueryUnderstandingService
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy

TAXONOMY = load_taxonomy()
ATTRIBUTES = load_catalog_attributes()
DIMENSIONS = load_dimension_semantics(taxonomy=TAXONOMY)
LOCKED = ConstraintStrength.LOCKED
PREFERRED = ConstraintStrength.PREFERRED
APPROXIMATE = ConstraintStrength.APPROXIMATE


class FakeLLMClient:
    def __init__(self, interpretation: CommerceInterpretation) -> None:
        self._interpretation = interpretation

    @property
    def model(self) -> str:
        return "fake-model"

    async def parse(self, **_: Any) -> Any:
        return self._interpretation


async def _interpret(interpretation: CommerceInterpretation) -> Any:
    service = QueryUnderstandingService(
        cast(StructuredLLMClient, FakeLLMClient(interpretation)), TAXONOMY, ATTRIBUTES, DIMENSIONS
    )
    return await service.interpret("a message")


def _sofa(**extra: Any) -> CommerceInterpretation:
    return CommerceInterpretation(
        commerce_category="seating", commerce_subcategory="sofa", **extra
    )


# ── default strength ────────────────────────────────────────────────────────


async def test_an_unqualified_constraint_defaults_to_locked() -> None:
    """"under 5000 SAR" is a requirement until the customer softens it."""
    outcome = await _interpret(_sofa(price_max="5000", price_currency="SAR"))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.semantics.price_max is LOCKED
    assert outcome.semantics.subcategory is LOCKED


async def test_an_absent_constraint_carries_no_strength() -> None:
    outcome = await _interpret(CommerceInterpretation(commerce_category="tables"))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.semantics == ConstraintSemantics()


@pytest.mark.parametrize("strength", [PREFERRED, APPROXIMATE])
async def test_a_stated_strength_is_preserved(strength: ConstraintStrength) -> None:
    outcome = await _interpret(
        _sofa(price_max="5000", price_currency="SAR", price_max_strength=strength)
    )

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.semantics.price_max is strength


# ── every relaxable field ───────────────────────────────────────────────────


async def test_subcategory_strength() -> None:
    outcome = await _interpret(_sofa(commerce_subcategory_strength=PREFERRED))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.semantics.subcategory is PREFERRED


async def test_price_bounds_carry_independent_strengths() -> None:
    outcome = await _interpret(
        _sofa(
            price_min="2500",
            price_max="6000",
            price_currency="SAR",
            price_min_strength=LOCKED,
            price_max_strength=APPROXIMATE,
        )
    )

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.semantics.price_min is LOCKED
    assert outcome.semantics.price_max is APPROXIMATE


async def test_seating_bounds_carry_independent_strengths() -> None:
    outcome = await _interpret(
        _sofa(
            seating_capacity_min=3,
            seating_capacity_max=5,
            seating_capacity_min_strength=LOCKED,
            seating_capacity_max_strength=PREFERRED,
        )
    )

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.semantics.seating_min is LOCKED
    assert outcome.semantics.seating_max is PREFERRED


async def test_an_approximate_capacity_keeps_the_exact_executable_value() -> None:
    """M7 records that "around four" was loose. It does not decide a tolerance."""
    outcome = await _interpret(
        _sofa(
            seating_capacity_min=4,
            seating_capacity_max=4,
            seating_capacity_min_strength=APPROXIMATE,
            seating_capacity_max_strength=APPROXIMATE,
        )
    )

    assert isinstance(outcome, ResolvedSearch)
    capacity = outcome.request.seating_capacity
    assert capacity is not None
    assert (capacity.min_capacity, capacity.max_capacity) == (4, 4)
    assert outcome.semantics.seating_min is APPROXIMATE


# ── separation from execution ───────────────────────────────────────────────


def test_the_executable_request_carries_no_strength() -> None:
    """Discovery executes exactly what was asked; strength is not its business."""
    assert not any(
        "strength" in field for field in ProductSearchRequest.model_fields
    )
    assert "semantics" not in ProductSearchRequest.model_fields


def test_discovery_has_no_dependency_on_constraint_strength() -> None:
    from pathlib import Path

    root = Path(__file__).parents[2] / "app"
    for module in (
        root / "services/discovery.py",
        root / "repositories/products.py",
        root / "schemas/discovery.py",
    ):
        source = module.read_text()
        assert "ConstraintStrength" not in source, module.name
        assert "ConstraintSemantics" not in source, module.name


async def test_the_same_request_is_produced_whatever_the_strength() -> None:
    """Semantics annotate the request; they never alter it."""
    locked = await _interpret(
        _sofa(price_max="5000", price_currency="SAR", price_max_strength=LOCKED)
    )
    approximate = await _interpret(
        _sofa(price_max="5000", price_currency="SAR", price_max_strength=APPROXIMATE)
    )

    assert isinstance(locked, ResolvedSearch)
    assert isinstance(approximate, ResolvedSearch)
    assert locked.request == approximate.request
    assert locked.semantics != approximate.semantics


def test_category_is_not_relaxable() -> None:
    """A customer asking for lighting has not asked for tables."""
    assert "category" not in ConstraintSemantics.model_fields
    assert "commerce_category" not in ConstraintSemantics.model_fields


async def test_the_single_seater_rule_is_unchanged() -> None:
    outcome = await _interpret(
        CommerceInterpretation(
            commerce_category="seating", commerce_subcategory="single-seater-sofa"
        )
    )

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.seating_capacity is None
    assert outcome.semantics.seating_min is None


# ── unsupported requirements ────────────────────────────────────────────────


@pytest.mark.parametrize("family", list(RequirementFamily))
async def test_an_unsupported_requirement_is_not_silently_dropped(
    family: RequirementFamily,
) -> None:
    outcome = await _interpret(_sofa(unsupported_requirements=[family]))

    assert isinstance(outcome, UnsupportedRequirement)
    assert outcome.unsupported == (family,)


async def test_an_unsupported_requirement_is_a_distinct_type() -> None:
    """A caller checking only for ResolvedSearch cannot mistake it for success."""
    outcome = await _interpret(
        _sofa(unsupported_requirements=[RequirementFamily.COLOR])
    )

    assert not isinstance(outcome, ResolvedSearch)


async def test_an_unsupported_requirement_still_carries_what_was_understood() -> None:
    outcome = await _interpret(
        _sofa(
            price_max="5000",
            price_currency="SAR",
            unsupported_requirements=[RequirementFamily.COLOR],
        )
    )

    assert isinstance(outcome, UnsupportedRequirement)
    assert outcome.request.commerce_subcategory == "sofa"
    assert outcome.request.price is not None
    assert outcome.request.price.max_amount == Decimal("5000")
    assert outcome.semantics.price_max is LOCKED


async def test_several_unsupported_families_are_all_reported() -> None:
    outcome = await _interpret(
        _sofa(
            unsupported_requirements=[
                RequirementFamily.COLOR,
                RequirementFamily.STYLE,
                RequirementFamily.COLOR,
            ]
        )
    )

    assert isinstance(outcome, UnsupportedRequirement)
    assert outcome.unsupported == (RequirementFamily.COLOR, RequirementFamily.STYLE)


async def test_no_unsupported_requirement_resolves_normally() -> None:
    outcome = await _interpret(_sofa())

    assert isinstance(outcome, ResolvedSearch)


# ── multi-product and missing context ───────────────────────────────────────


async def test_several_product_types_ask_rather_than_choosing_one() -> None:
    outcome = await _interpret(
        _sofa(multiple_product_types=True)
    )

    assert isinstance(outcome, ClarificationRequired)
    assert outcome.reason is ClarificationReason.MULTIPLE_PRODUCT_TYPES


async def test_multiple_product_types_wins_over_a_guessed_category() -> None:
    """Even with one category filled in, the other intent is not dropped."""
    outcome = await _interpret(
        CommerceInterpretation(
            commerce_category="seating",
            commerce_subcategory="sofa",
            multiple_product_types=True,
        )
    )

    assert isinstance(outcome, ClarificationRequired)


async def test_a_reference_to_missing_context_does_not_invent_one() -> None:
    """"show me cheaper ones" with no prior message has no product type."""
    outcome = await _interpret(CommerceInterpretation(sort=None))

    assert isinstance(outcome, ClarificationRequired)
    assert outcome.reason is ClarificationReason.NO_COMMERCE_CATEGORY
