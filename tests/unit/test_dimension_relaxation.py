"""Controlled dimension relaxation: what may widen, by how much, and how it reads.

Three things this file defends. The allowlist fails closed, so a measurement
nobody validated is never widened. Every stage is computed from the customer's
original figure, so two stages widen further rather than widening each other.
And a TARGET keeps its meaning: it executes as a band because SQL has no other
way to say "near 220", but the provenance still records that they named a
figure, not an interval.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.core.config import RelaxationSettings
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PriceConstraint,
    ProductSearchRequest,
)
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    ResolvedSearch,
)
from app.schemas.relaxation import (
    DimensionRelaxationChange,
    RelaxableField,
    RelaxationChange,
)
from app.services.dimension_policy import (
    V1_DIMENSION_RELAXATION_POLICY as POLICY,
)
from app.services.dimension_policy import (
    DimensionRelaxationPolicy,
)
from app.services.relaxation import PlannedRelaxation, RelaxationPlanner
from app.taxonomy.dimensions import DimensionRole, load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from pydantic import ValidationError

WIDTH = DimensionRole.OVERALL_WIDTH
DEPTH = DimensionRole.DEPTH
HEIGHT = DimensionRole.HEIGHT
LENGTH = DimensionRole.LENGTH
MIN, MAX = DimensionConstraintKind.MIN, DimensionConstraintKind.MAX
RANGE, TARGET = DimensionConstraintKind.RANGE, DimensionConstraintKind.TARGET
LOCKED = ConstraintStrength.LOCKED
PREFERRED = ConstraintStrength.PREFERRED
APPROXIMATE = ConstraintStrength.APPROXIMATE
SAR = "SAR"

PLANNER = RelaxationPlanner(RelaxationSettings())


def _constraint(
    kind: DimensionConstraintKind, role: DimensionRole = WIDTH, **v: str
) -> DimensionConstraint:
    return DimensionConstraint(
        role=role, kind=kind, **{k: Decimal(x) for k, x in v.items()}
    )


def _resolved(
    *pairs: tuple[DimensionConstraint, ConstraintStrength],
    subcategory: str = "sofa",
    category: str = "seating",
    price: PriceConstraint | None = None,
    price_strength: ConstraintStrength | None = None,
) -> ResolvedSearch:
    return ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category=category,
            commerce_subcategory=subcategory,
            price=price,
            dimensions=tuple(c for c, _ in pairs),
        ),
        semantics=ConstraintSemantics(
            price_max=price_strength,
            dimensions=tuple(
                DimensionConstraintSemantics(role=c.role, strength=s) for c, s in pairs
            ),
        ),
    )


def _dimension_changes(
    steps: tuple[PlannedRelaxation, ...],
) -> list[DimensionRelaxationChange]:
    return [
        change
        for step in steps
        for change in step.changes
        if isinstance(change, DimensionRelaxationChange)
    ]


def _applied(steps: tuple[PlannedRelaxation, ...]) -> list[tuple[Any, Any]]:
    return [(c.applied_min_cm, c.applied_max_cm) for c in _dimension_changes(steps)]


# ── the allowlist ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("subcategory", "role"),
    [("sofa", WIDTH), ("service-table", WIDTH)],
)
def test_the_approved_pairs_are_relaxable(subcategory: str, role: DimensionRole) -> None:
    assert POLICY.is_relaxable(subcategory, role)


@pytest.mark.parametrize(
    ("subcategory", "role"),
    [
        ("sofa", DEPTH),
        ("sofa", HEIGHT),
        ("dining-table", LENGTH),
        ("dining-table", WIDTH),
        ("tv-table", WIDTH),
        ("console", WIDTH),
        ("wardrobe", WIDTH),
        ("center-table", WIDTH),
        ("shelve", WIDTH),
        ("office-table", WIDTH),
        ("service-table", DEPTH),
        ("service-table", HEIGHT),
    ],
)
def test_everything_else_is_not(subcategory: str, role: DimensionRole) -> None:
    assert not POLICY.is_relaxable(subcategory, role)


def test_an_unknown_combination_fails_closed() -> None:
    assert not POLICY.is_relaxable("chesterfield", WIDTH)
    assert not POLICY.is_relaxable("sofa", LENGTH)


def test_a_request_with_no_subcategory_is_never_relaxable() -> None:
    """The same role means different things for different product types."""
    assert not POLICY.is_relaxable(None, WIDTH)


def test_every_allowlisted_pair_is_one_the_registry_supports() -> None:
    """A typo here would fail closed silently; this is what catches it."""
    # The registry rejects an unapproved subcategory at load, so a resolved
    # axis also proves the subcategory itself is approved.
    semantics = load_dimension_semantics(taxonomy=load_taxonomy())
    for subcategory, role in POLICY.relaxable:
        assert semantics.source_axis(subcategory, role) is not None, (subcategory, role)


def test_the_policy_is_immutable() -> None:
    with pytest.raises(ValidationError):
        POLICY.relaxable = frozenset()  # type: ignore[misc]


def test_a_non_relaxable_subcategory_produces_no_dimension_steps() -> None:
    for subcategory, category, role in (
        ("tv-table", "tables", WIDTH),
        ("console", "tables", WIDTH),
        ("wardrobe", "bedroom", WIDTH),
    ):
        steps = PLANNER.plan(
            _resolved(
                (_constraint(MAX, role, max_cm="220"), APPROXIMATE),
                subcategory=subcategory,
                category=category,
            )
        )
        assert _dimension_changes(steps) == [], subcategory


@pytest.mark.parametrize("role", [DEPTH, HEIGHT])
def test_a_non_relaxable_role_on_a_relaxable_subcategory_produces_no_steps(
    role: DimensionRole,
) -> None:
    steps = PLANNER.plan(
        _resolved((_constraint(MAX, role, max_cm="95"), PREFERRED))
    )

    assert _dimension_changes(steps) == []


def test_a_dining_table_length_is_not_relaxable() -> None:
    steps = PLANNER.plan(
        _resolved(
            (_constraint(MIN, LENGTH, min_cm="180"), APPROXIMATE),
            subcategory="dining-table",
            category="dining",
        )
    )

    assert _dimension_changes(steps) == []


# ── strength ────────────────────────────────────────────────────────────────


def test_a_locked_measurement_never_widens() -> None:
    steps = PLANNER.plan(_resolved((_constraint(MAX, max_cm="220"), LOCKED)))

    assert steps == ()


@pytest.mark.parametrize("strength", [APPROXIMATE, PREFERRED])
def test_a_soft_measurement_produces_exactly_two_stages(
    strength: ConstraintStrength,
) -> None:
    changes = _dimension_changes(
        PLANNER.plan(_resolved((_constraint(MAX, max_cm="220"), strength)))
    )

    assert [c.stage for c in changes] == [1, 2]
    assert all(c.strength is strength for c in changes)


def test_approximate_is_offered_before_preferred() -> None:
    steps = PLANNER.plan(
        _resolved(
            (_constraint(MAX, WIDTH, max_cm="220"), PREFERRED),
            price=PriceConstraint.at_most(Decimal("5000"), SAR),
            price_strength=APPROXIMATE,
        )
    )

    fields = [
        change.field for step in steps for change in step.changes
    ]
    first_dimension = fields.index(RelaxableField.DIMENSION)
    assert fields[0] is RelaxableField.PRICE_MAX
    assert first_dimension > 0, "the approximate price widens before the preferred width"


def test_measurements_come_after_price_and_seating_within_a_tier() -> None:
    """Existing price and seating order is untouched; DIMENSION is appended."""
    steps = PLANNER.plan(
        _resolved(
            (_constraint(MAX, WIDTH, max_cm="220"), APPROXIMATE),
            price=PriceConstraint.at_most(Decimal("5000"), SAR),
            price_strength=APPROXIMATE,
        )
    )

    assert [str(c.field) for s in steps for c in s.changes] == [
        "price_max",
        "price_max",
        "dimension",
        "dimension",
    ]


# ── directional arithmetic, always from the original ────────────────────────


def test_a_maximum_only_rises() -> None:
    steps = PLANNER.plan(_resolved((_constraint(MAX, max_cm="220"), APPROXIMATE)))

    assert _applied(steps) == [(None, Decimal("231")), (None, Decimal("242"))]


def test_a_minimum_only_drops() -> None:
    steps = PLANNER.plan(_resolved((_constraint(MIN, min_cm="180"), APPROXIMATE)))

    assert _applied(steps) == [(Decimal("171"), None), (Decimal("162"), None)]


def test_a_range_expands_each_side_from_its_own_bound() -> None:
    steps = PLANNER.plan(
        _resolved((_constraint(RANGE, min_cm="180", max_cm="220"), APPROXIMATE))
    )

    assert _applied(steps) == [
        (Decimal("171"), Decimal("231")),
        (Decimal("162"), Decimal("242")),
    ]


def test_a_target_widens_symmetrically_about_the_original() -> None:
    steps = PLANNER.plan(_resolved((_constraint(TARGET, target_cm="220"), APPROXIMATE)))

    assert _applied(steps) == [
        (Decimal("209"), Decimal("231")),
        (Decimal("198"), Decimal("242")),
    ]


@pytest.mark.parametrize(
    ("kind", "values", "expected"),
    [
        (MAX, {"max_cm": "220"}, Decimal("242")),
        (MIN, {"min_cm": "180"}, Decimal("162")),
    ],
)
def test_stage_two_derives_from_the_original_not_from_stage_one(
    kind: DimensionConstraintKind, values: dict[str, str], expected: Decimal
) -> None:
    """Compounding 5% twice would give 220->231->242.55, not 242."""
    steps = PLANNER.plan(_resolved((_constraint(kind, WIDTH, **values), APPROXIMATE)))
    second = _dimension_changes(steps)[1]

    applied = second.applied_max_cm if kind is MAX else second.applied_min_cm
    assert applied == expected


def test_a_target_stage_two_is_not_compounded() -> None:
    """220 ±22, never stage one's band widened again."""
    steps = PLANNER.plan(_resolved((_constraint(TARGET, target_cm="220"), APPROXIMATE)))
    second = _dimension_changes(steps)[1]

    assert second.applied_min_cm == Decimal("198")
    assert second.applied_max_cm == Decimal("242")
    assert second.original_target_cm == Decimal("220")


def test_a_widened_floor_never_reaches_zero() -> None:
    steps = PLANNER.plan(_resolved((_constraint(MIN, min_cm="1"), APPROXIMATE)))

    assert all(low is not None and low > 0 for low, _ in _applied(steps))


# ── the executable request ──────────────────────────────────────────────────


def test_a_widened_target_executes_as_a_band_and_carries_no_target() -> None:
    steps = PLANNER.plan(_resolved((_constraint(TARGET, target_cm="220"), APPROXIMATE)))

    for step in steps:
        carried = step.request.dimensions[0]
        assert carried.kind is RANGE
        assert carried.target_cm is None
        assert carried.min_cm is not None and carried.max_cm is not None


def test_a_widened_target_still_records_the_customers_kind() -> None:
    """The request says how to execute it; the provenance says what they meant."""
    steps = PLANNER.plan(_resolved((_constraint(TARGET, target_cm="220"), APPROXIMATE)))

    for change in _dimension_changes(steps):
        assert change.kind is TARGET
        assert change.original_target_cm == Decimal("220")


@pytest.mark.parametrize("kind", [MIN, MAX, RANGE])
def test_every_other_kind_keeps_its_kind_when_widened(
    kind: DimensionConstraintKind,
) -> None:
    values: dict[str, str] = {
        MIN: {"min_cm": "180"},
        MAX: {"max_cm": "220"},
        RANGE: {"min_cm": "180", "max_cm": "220"},
    }[kind]
    steps = PLANNER.plan(_resolved((_constraint(kind, WIDTH, **values), APPROXIMATE)))

    assert all(s.request.dimensions[0].kind is kind for s in steps)


def test_the_customers_units_survive_widening() -> None:
    original = DimensionConstraint(
        role=WIDTH, kind=MAX, max_cm=Decimal("213.36"),
        source_value="7", source_unit="ft",
    )
    steps = PLANNER.plan(_resolved((original, APPROXIMATE)))

    for step in steps:
        assert step.request.dimensions[0].source_value == "7"
        assert step.request.dimensions[0].source_unit == "ft"


# ── mixed strengths ─────────────────────────────────────────────────────────


def test_only_the_soft_measurement_moves() -> None:
    """"around 220 wide but it must be under 100 deep"."""
    width = _constraint(TARGET, WIDTH, target_cm="220")
    depth = _constraint(MAX, DEPTH, max_cm="100")
    steps = PLANNER.plan(_resolved((width, APPROXIMATE), (depth, LOCKED)))

    assert steps
    for step in steps:
        carried = {c.role: c for c in step.request.dimensions}
        assert carried[DEPTH] == depth, "the locked depth is the identical object"
        assert carried[WIDTH].kind is RANGE
    assert all(c.role is WIDTH for c in _dimension_changes(steps))


def test_a_locked_measurement_beside_a_soft_price_is_still_carried() -> None:
    depth = _constraint(MAX, DEPTH, max_cm="100")
    steps = PLANNER.plan(
        _resolved(
            (depth, LOCKED),
            price=PriceConstraint.at_most(Decimal("5000"), SAR),
            price_strength=APPROXIMATE,
        )
    )

    assert steps
    assert all(s.request.dimensions == (depth,) for s in steps)


# ── planar stays exact ──────────────────────────────────────────────────────


def test_no_relaxable_field_exists_for_a_planar_pair() -> None:
    assert "planar_dimension" not in {f.value for f in RelaxableField}


# ── provenance shape ────────────────────────────────────────────────────────


def test_a_generic_change_may_not_claim_to_be_a_dimension() -> None:
    with pytest.raises(ValidationError):
        RelaxationChange(
            field=RelaxableField.DIMENSION,
            from_value=Decimal("220"),
            to_value=Decimal("231"),
            strength=APPROXIMATE,
        )


def test_a_dimension_change_must_match_its_kind() -> None:
    with pytest.raises(ValidationError):
        DimensionRelaxationChange(
            role=WIDTH, kind=MAX, strength=APPROXIMATE, stage=1,
            original_target_cm=Decimal("220"), applied_max_cm=Decimal("231"),
        )


def test_a_ceiling_may_not_be_recorded_as_moving_inward() -> None:
    with pytest.raises(ValidationError):
        DimensionRelaxationChange(
            role=WIDTH, kind=MAX, strength=APPROXIMATE, stage=1,
            original_max_cm=Decimal("220"), applied_max_cm=Decimal("210"),
        )


def test_a_floor_may_not_be_recorded_as_moving_inward() -> None:
    with pytest.raises(ValidationError):
        DimensionRelaxationChange(
            role=WIDTH, kind=MIN, strength=APPROXIMATE, stage=1,
            original_min_cm=Decimal("180"), applied_min_cm=Decimal("190"),
        )


def test_an_asymmetric_target_band_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DimensionRelaxationChange(
            role=WIDTH, kind=TARGET, strength=APPROXIMATE, stage=1,
            original_target_cm=Decimal("220"),
            applied_min_cm=Decimal("209"), applied_max_cm=Decimal("240"),
        )


def test_a_dimension_change_carries_its_field_without_being_told() -> None:
    change = DimensionRelaxationChange(
        role=WIDTH, kind=MAX, strength=PREFERRED, stage=2,
        original_max_cm=Decimal("220"), applied_max_cm=Decimal("242"),
    )

    assert change.field is RelaxableField.DIMENSION
    assert change.role is WIDTH


def test_a_stage_is_never_zero() -> None:
    """Stage 0 is the exact search, which is not a relaxation."""
    with pytest.raises(ValidationError):
        DimensionRelaxationChange(
            role=WIDTH, kind=MAX, strength=APPROXIMATE, stage=0,
            original_max_cm=Decimal("220"), applied_max_cm=Decimal("231"),
        )


# ── configuration, not constants ────────────────────────────────────────────


def test_the_stages_come_from_settings() -> None:
    planner = RelaxationPlanner(
        RelaxationSettings(dimension_steps=(Decimal("0.02"),))
    )
    steps = planner.plan(_resolved((_constraint(MAX, max_cm="200"), APPROXIMATE)))

    assert _applied(steps) == [(None, Decimal("204"))]


def test_the_allowlist_is_injectable() -> None:
    planner = RelaxationPlanner(
        RelaxationSettings(),
        DimensionRelaxationPolicy(relaxable=frozenset({("tv-table", WIDTH)})),
    )
    steps = planner.plan(
        _resolved(
            (_constraint(MAX, max_cm="200"), APPROXIMATE),
            subcategory="tv-table",
            category="tables",
        )
    )

    assert [c.stage for c in _dimension_changes(steps)] == [1, 2]


@pytest.mark.parametrize("steps", [(), (Decimal("0.2"),), (Decimal("0.1"), Decimal("0.05"))])
def test_unsafe_dimension_steps_are_rejected(steps: tuple[Decimal, ...]) -> None:
    with pytest.raises(ValidationError):
        RelaxationSettings(dimension_steps=steps)
