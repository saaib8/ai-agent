"""Per-measurement constraint strength, from the model's words to M8.

The contract these tests defend: a measurement's numbers live on the request,
its firmness lives on the semantics beside it, and the two correspond by role
rather than by position. A single strength shared by every measurement in a
query would be unable to express "around 220 wide but it must be under 100
deep", which is the case this file exists for.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from app.core.config import RelaxationSettings
from app.core.exceptions import DimensionSemanticsMissingError
from app.integrations.llm import StructuredLLMClient
from app.schemas.discovery import (
    DimensionConstraint,
    PlanarDimensionConstraint,
    PriceConstraint,
    ProductSearchRequest,
)
from app.schemas.discovery import DimensionConstraintKind as Kind
from app.schemas.query import (
    ClarificationRequired,
    CommerceInterpretation,
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    DimensionInterpretation,
    PlanarDimensionInterpretation,
    PlanarDimensionSemantics,
    ResolvedSearch,
    UnsupportedDimensionRequirement,
)
from app.schemas.relaxation import DimensionRelaxationChange, RelaxableField
from app.services.query_understanding import QueryUnderstandingService
from app.services.relaxation import RelaxationPlanner
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import DimensionRole, load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from pydantic import ValidationError

TAXONOMY = load_taxonomy()
ATTRIBUTES = load_catalog_attributes()
SEMANTICS = load_dimension_semantics(taxonomy=TAXONOMY)

WIDTH = DimensionRole.OVERALL_WIDTH
DEPTH = DimensionRole.DEPTH
HEIGHT = DimensionRole.HEIGHT
LOCKED = ConstraintStrength.LOCKED
PREFERRED = ConstraintStrength.PREFERRED
APPROXIMATE = ConstraintStrength.APPROXIMATE
SAR = "SAR"


class FakeLLMClient:
    def __init__(self, interpretation: CommerceInterpretation) -> None:
        self._interpretation = interpretation
        self.model = "fake"

    async def parse(self, **_: Any) -> CommerceInterpretation:
        return self._interpretation


async def _interpret(
    *dimensions: DimensionInterpretation,
    subcategory: str | None = "sofa",
    category: str = "seating",
    planar: PlanarDimensionInterpretation | None = None,
) -> Any:
    service = QueryUnderstandingService(
        cast(
            StructuredLLMClient,
            FakeLLMClient(
                CommerceInterpretation(
                    commerce_category=category,
                    commerce_subcategory=subcategory,
                    dimensions=list(dimensions),
                    planar_dimensions=planar,
                )
            ),
        ),
        TAXONOMY,
        ATTRIBUTES,
        SEMANTICS,
    )
    return await service.interpret("a message")


def _dim(
    role: DimensionRole | None,
    kind: Kind,
    strength: ConstraintStrength = LOCKED,
    unit: str | None = "cm",
    **values: str,
) -> DimensionInterpretation:
    return DimensionInterpretation(
        role=role, kind=kind, unit=unit, strength=strength, **values
    )


# ── scalar: the strength the model stated is the strength recorded ──────────


@pytest.mark.parametrize("strength", list(ConstraintStrength))
async def test_every_strength_survives_interpretation(
    strength: ConstraintStrength,
) -> None:
    outcome = await _interpret(_dim(WIDTH, Kind.MAX, strength, max_value="220"))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.semantics.strength_for_dimension(WIDTH) is strength


@pytest.mark.parametrize(
    ("kind", "values"),
    [
        (Kind.MIN, {"min_value": "90"}),
        (Kind.MAX, {"max_value": "220"}),
        (Kind.RANGE, {"min_value": "180", "max_value": "220"}),
        (Kind.TARGET, {"target_value": "220"}),
    ],
)
async def test_strength_survives_for_every_kind(
    kind: Kind, values: dict[str, str]
) -> None:
    outcome = await _interpret(_dim(WIDTH, kind, PREFERRED, **values))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.dimensions[0].kind is kind
    assert outcome.semantics.strength_for_dimension(WIDTH) is PREFERRED


async def test_a_target_keeps_both_its_kind_and_its_looseness() -> None:
    """"around 220 cm wide": TARGET on the request, APPROXIMATE on the semantics."""
    outcome = await _interpret(
        _dim(WIDTH, Kind.TARGET, APPROXIMATE, target_value="220")
    )

    assert isinstance(outcome, ResolvedSearch)
    constraint = outcome.request.dimensions[0]
    assert constraint.kind is Kind.TARGET
    assert constraint.target_cm == Decimal("220")
    assert constraint.max_cm is None
    assert outcome.semantics.strength_for_dimension(WIDTH) is APPROXIMATE


async def test_two_measurements_can_hold_different_strengths() -> None:
    """The case a single global dimension strength could not express."""
    outcome = await _interpret(
        _dim(WIDTH, Kind.TARGET, APPROXIMATE, target_value="220"),
        _dim(DEPTH, Kind.MAX, LOCKED, max_value="100"),
    )

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.semantics.strength_for_dimension(WIDTH) is APPROXIMATE
    assert outcome.semantics.strength_for_dimension(DEPTH) is LOCKED


async def test_lookup_is_by_role_not_by_position() -> None:
    """Reordering the entries cannot change what any role means."""
    forwards = ConstraintSemantics(
        dimensions=(
            DimensionConstraintSemantics(role=WIDTH, strength=APPROXIMATE),
            DimensionConstraintSemantics(role=DEPTH, strength=LOCKED),
        )
    )
    backwards = ConstraintSemantics(dimensions=tuple(reversed(forwards.dimensions)))

    for semantics in (forwards, backwards):
        assert semantics.strength_for_dimension(WIDTH) is APPROXIMATE
        assert semantics.strength_for_dimension(DEPTH) is LOCKED


def test_an_unrecorded_role_raises_rather_than_defaulting() -> None:
    """A silent default would be consent nobody gave."""
    semantics = ConstraintSemantics(
        dimensions=(DimensionConstraintSemantics(role=WIDTH, strength=LOCKED),)
    )

    with pytest.raises(DimensionSemanticsMissingError):
        semantics.strength_for_dimension(DEPTH)


def test_a_role_cannot_carry_two_strengths() -> None:
    with pytest.raises(ValidationError):
        ConstraintSemantics(
            dimensions=(
                DimensionConstraintSemantics(role=WIDTH, strength=LOCKED),
                DimensionConstraintSemantics(role=WIDTH, strength=APPROXIMATE),
            )
        )


# ── request / semantics correspondence ──────────────────────────────────────


def _constraint(role: DimensionRole = WIDTH) -> DimensionConstraint:
    return DimensionConstraint(role=role, kind=Kind.MAX, max_cm=Decimal("220"))


def _request(*dimensions: DimensionConstraint, **kwargs: Any) -> ProductSearchRequest:
    return ProductSearchRequest(
        commerce_category="seating",
        commerce_subcategory="sofa",
        dimensions=dimensions,
        **kwargs,
    )


def test_a_dimension_without_a_recorded_strength_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResolvedSearch(request=_request(_constraint()), semantics=ConstraintSemantics())


def test_a_strength_for_an_absent_dimension_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResolvedSearch(
            request=_request(),
            semantics=ConstraintSemantics(
                dimensions=(DimensionConstraintSemantics(role=WIDTH, strength=LOCKED),)
            ),
        )


def test_a_strength_recorded_for_the_wrong_role_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResolvedSearch(
            request=_request(_constraint(WIDTH)),
            semantics=ConstraintSemantics(
                dimensions=(DimensionConstraintSemantics(role=DEPTH, strength=LOCKED),)
            ),
        )


def test_a_request_carries_at_most_one_constraint_per_role() -> None:
    with pytest.raises(ValidationError):
        _request(_constraint(WIDTH), _constraint(WIDTH))


def test_two_roles_in_one_request_are_fine() -> None:
    request = _request(_constraint(WIDTH), _constraint(DEPTH))

    assert len(request.dimensions) == 2


# ── planar ──────────────────────────────────────────────────────────────────


def _planar(strength: ConstraintStrength) -> PlanarDimensionInterpretation:
    return PlanarDimensionInterpretation(
        first_value="200", second_value="300", unit="cm", strength=strength
    )


@pytest.mark.parametrize("strength", list(ConstraintStrength))
async def test_planar_strength_survives(strength: ConstraintStrength) -> None:
    outcome = await _interpret(
        subcategory="carpet", category="decor", planar=_planar(strength)
    )

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.semantics.planar_dimension_strength() is strength


async def test_a_pair_and_its_strength_arrive_together() -> None:
    outcome = await _interpret(
        subcategory="carpet", category="decor", planar=_planar(APPROXIMATE)
    )

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.planar_dimensions is not None
    assert outcome.semantics.planar_dimension is not None


async def test_no_pair_means_no_planar_strength() -> None:
    outcome = await _interpret(_dim(WIDTH, Kind.MAX, LOCKED, max_value="220"))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.planar_dimensions is None
    assert outcome.semantics.planar_dimension_strength() is None


def test_a_pair_without_its_strength_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResolvedSearch(
            request=_request(
                planar_dimensions=PlanarDimensionConstraint(
                    first_cm=Decimal("200"), second_cm=Decimal("300")
                )
            ),
            semantics=ConstraintSemantics(),
        )


def test_a_planar_strength_without_a_pair_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResolvedSearch(
            request=_request(),
            semantics=ConstraintSemantics(
                planar_dimension=PlanarDimensionSemantics(strength=LOCKED)
            ),
        )


async def test_scalar_and_planar_strengths_are_independent() -> None:
    """A carpet may take a height constraint and a pair at different strengths."""
    outcome = await _interpret(
        _dim(HEIGHT, Kind.MAX, LOCKED, max_value="3"),
        subcategory="carpet",
        category="decor",
        planar=_planar(APPROXIMATE),
    )

    # Carpet height is refused by the registry, so this is the refusal path -
    # what matters is that the pair's strength is untouched by it.
    assert isinstance(outcome, UnsupportedDimensionRequirement)
    assert outcome.semantics.planar_dimension_strength() is APPROXIMATE
    assert outcome.unsupported_dimensions[0].strength is LOCKED


# ── the request stays executable facts only ─────────────────────────────────


def test_a_dimension_constraint_has_no_strength() -> None:
    assert "strength" not in DimensionConstraint.model_fields


def test_a_planar_constraint_has_no_strength() -> None:
    assert "strength" not in PlanarDimensionConstraint.model_fields


def test_no_request_field_carries_relaxation_metadata() -> None:
    """The request is what PostgreSQL executes, and nothing more."""
    for name in ProductSearchRequest.model_fields:
        assert "strength" not in name
        assert "semantic" not in name


def test_a_constraint_rejects_a_strength_argument() -> None:
    with pytest.raises(ValidationError):
        DimensionConstraint(
            role=WIDTH,
            kind=Kind.MAX,
            max_cm=Decimal("220"),
            strength=LOCKED,  # type: ignore[call-arg]
        )


# ── a refused measurement keeps its strength as provenance ──────────────────


@pytest.mark.parametrize("strength", list(ConstraintStrength))
async def test_a_refused_measurement_preserves_its_strength(
    strength: ConstraintStrength,
) -> None:
    outcome = await _interpret(
        _dim(DimensionRole.LENGTH, Kind.MAX, strength, max_value="200"),
        subcategory="bed",
        category="bedroom",
    )

    assert isinstance(outcome, UnsupportedDimensionRequirement)
    refused = outcome.unsupported_dimensions[0]
    assert refused.strength is strength
    assert refused.max_cm == Decimal("200")
    # It was never applied, so it is not in the request and not in the semantics.
    assert outcome.request.dimensions == ()
    assert outcome.semantics.dimensions == ()


# ── an incomplete measurement records nothing ───────────────────────────────


@pytest.mark.parametrize(
    ("dimension", "reason"),
    [
        (_dim(None, Kind.MAX, LOCKED, max_value="200"), "missing_dimension_role"),
        (_dim(WIDTH, Kind.MAX, LOCKED, unit=None, max_value="220"), "missing_dimension_unit"),
    ],
)
async def test_an_incomplete_measurement_asks_and_records_nothing(
    dimension: DimensionInterpretation, reason: str
) -> None:
    outcome = await _interpret(dimension)

    assert isinstance(outcome, ClarificationRequired)
    assert str(outcome.reason) == reason


# ── M8 can read it, and still does nothing with it ──────────────────────────


def _resolved(
    strength: ConstraintStrength, *, price: PriceConstraint | None = None
) -> ResolvedSearch:
    return ResolvedSearch(
        request=_request(_constraint(WIDTH), price=price),
        semantics=ConstraintSemantics(
            price_max=APPROXIMATE if price else None,
            dimensions=(
                DimensionConstraintSemantics(role=WIDTH, strength=strength),
            ),
        ),
    )


def test_m8_can_read_a_dimension_strength() -> None:
    resolved = _resolved(APPROXIMATE)

    assert resolved.semantics.strength_for_dimension(WIDTH) is APPROXIMATE


def test_the_relaxable_fields_are_the_v1_set() -> None:
    """One generic DIMENSION member; the role travels on the provenance.

    No PLANAR_DIMENSION: carpet pairs are not relaxable in V1, and a member for
    a widening that cannot happen would suggest it can.
    """
    assert {f.value for f in RelaxableField} == {
        "price_min",
        "price_max",
        "seating_min",
        "seating_max",
        "dimension",
        "color",
        "style",
    }


def test_a_locked_dimension_still_moves_for_no_reason() -> None:
    """M8C-B relaxes measurements, but never a locked one."""
    planner = RelaxationPlanner(RelaxationSettings())
    resolved = _resolved(LOCKED, price=PriceConstraint.at_most(Decimal("5000"), SAR))

    steps = planner.plan(resolved)

    assert steps, "the approximate price should still widen"
    for step in steps:
        assert step.request.dimensions == resolved.request.dimensions
        assert all(
            "dimension" not in str(change.field) for change in step.changes
        )


@pytest.mark.parametrize("strength", [APPROXIMATE, PREFERRED])
def test_a_soft_dimension_now_widens(strength: ConstraintStrength) -> None:
    """The strength M8C-A preserved is what M8C-B finally acts on."""
    planner = RelaxationPlanner(RelaxationSettings())
    resolved = _resolved(strength)

    steps = planner.plan(resolved)

    widened = [
        change
        for step in steps
        for change in step.changes
        if isinstance(change, DimensionRelaxationChange)
    ]
    assert [c.stage for c in widened] == [1, 2]
    assert all(c.role is WIDTH and c.strength is strength for c in widened)
    # 220 +5% then +10%, both from the original.
    assert [c.applied_max_cm for c in widened] == [Decimal("231"), Decimal("242")]
