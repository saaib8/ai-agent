"""The dimension registry, and how query understanding routes measurements."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.integrations.llm import StructuredLLMClient
from app.schemas.discovery import DimensionConstraintKind as Kind
from app.schemas.query import (
    ClarificationReason,
    ClarificationRequired,
    CommerceInterpretation,
    DimensionInterpretation,
    PlanarDimensionInterpretation,
    ResolvedSearch,
    UnsupportedDimensionRequirement,
)
from app.services.query_understanding import QueryUnderstandingService
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import (
    DimensionRole,
    SourceAxis,
    UnsupportedDimensionReason,
    load_dimension_semantics,
)
from app.taxonomy.registry import load_taxonomy

TAXONOMY = load_taxonomy()
ATTRIBUTES = load_catalog_attributes()
SEMANTICS = load_dimension_semantics(taxonomy=TAXONOMY)
WIDTH = DimensionRole.OVERALL_WIDTH
DEPTH = DimensionRole.DEPTH
HEIGHT = DimensionRole.HEIGHT
LENGTH = DimensionRole.LENGTH

APPROVED = {
    "sofa": {WIDTH: SourceAxis.LENGTH, DEPTH: SourceAxis.WIDTH, HEIGHT: SourceAxis.HEIGHT},
    "tv-table": {WIDTH: SourceAxis.LENGTH, DEPTH: SourceAxis.WIDTH, HEIGHT: SourceAxis.HEIGHT},
    "console": {WIDTH: SourceAxis.LENGTH, DEPTH: SourceAxis.WIDTH, HEIGHT: SourceAxis.HEIGHT},
    "wardrobe": {WIDTH: SourceAxis.LENGTH, DEPTH: SourceAxis.WIDTH, HEIGHT: SourceAxis.HEIGHT},
    "center-table": {WIDTH: SourceAxis.LENGTH, DEPTH: SourceAxis.WIDTH, HEIGHT: SourceAxis.HEIGHT},
    "service-table": {WIDTH: SourceAxis.LENGTH, DEPTH: SourceAxis.WIDTH, HEIGHT: SourceAxis.HEIGHT},
    "shelve": {WIDTH: SourceAxis.LENGTH, DEPTH: SourceAxis.WIDTH, HEIGHT: SourceAxis.HEIGHT},
    "office-table": {WIDTH: SourceAxis.LENGTH, DEPTH: SourceAxis.WIDTH, HEIGHT: SourceAxis.HEIGHT},
    "dining-table": {LENGTH: SourceAxis.LENGTH, WIDTH: SourceAxis.WIDTH, HEIGHT: SourceAxis.HEIGHT},
}
UNSAFE = ["bed", "sectional-sofa", "single-seater-sofa", "chair", "sofa-set", "mattresses"]


# ── registry ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("subcategory", sorted(APPROVED))
def test_every_approved_family_maps_its_roles(subcategory: str) -> None:
    for role, axis in APPROVED[subcategory].items():
        assert SEMANTICS.source_axis(subcategory, role) is axis, (subcategory, role)


def test_a_sofa_has_no_length_role() -> None:
    """A sofa is described by how wide it is, not how long."""
    assert SEMANTICS.source_axis("sofa", LENGTH) is None


@pytest.mark.parametrize("subcategory", UNSAFE)
@pytest.mark.parametrize("role", [LENGTH, WIDTH, DEPTH])
def test_unreliable_families_support_no_planar_role(
    subcategory: str, role: DimensionRole
) -> None:
    assert SEMANTICS.source_axis(subcategory, role) is None
    assert SEMANTICS.unsupported_reason(subcategory, role) is not (
        UnsupportedDimensionReason.ROLE_NOT_DEFINED
    )


@pytest.mark.parametrize("subcategory", ["sofa-bed", "nightstand"])
@pytest.mark.parametrize("role", list(DimensionRole))
def test_families_the_audit_left_uncertain_are_not_promoted(
    subcategory: str, role: DimensionRole
) -> None:
    """Populated columns are not evidence that the axes mean what we hope."""
    assert SEMANTICS.source_axis(subcategory, role) is None


def test_bed_reports_why_it_is_unsupported() -> None:
    assert SEMANTICS.unsupported_reason("bed", LENGTH) is (
        UnsupportedDimensionReason.UNRELIABLE_AXIS_MAPPING
    )


def test_an_unknown_subcategory_supports_nothing() -> None:
    assert SEMANTICS.source_axis("spaceship", WIDTH) is None
    assert SEMANTICS.unsupported_reason("spaceship", WIDTH) is (
        UnsupportedDimensionReason.ROLE_NOT_DEFINED
    )
    assert SEMANTICS.supported_roles(None) == frozenset()


def test_only_carpet_is_planar() -> None:
    pair = SEMANTICS.planar_pair("carpet")
    assert pair is not None
    assert pair.axes == (SourceAxis.LENGTH, SourceAxis.WIDTH)
    assert pair.unordered
    for subcategory in ("sofa", "dining-table", "bed", "mattresses"):
        assert not SEMANTICS.supports_planar(subcategory)


def test_carpet_height_is_refused_for_lack_of_data() -> None:
    assert SEMANTICS.unsupported_reason("carpet", HEIGHT) is (
        UnsupportedDimensionReason.INSUFFICIENT_COVERAGE
    )


def test_the_registry_names_only_approved_subcategories() -> None:
    known = {s for c in TAXONOMY.categories for s in TAXONOMY.subcategories(c)}
    assert SEMANTICS.subcategories <= known


def _data_strings(module: Path) -> set[str]:
    """String literals a module uses as data, excluding all documentation.

    Excludes bare string expressions - module, class and function docstrings,
    and the attribute docstrings this codebase writes under constants. Those
    explain *why* a rule exists and legitimately name roles and product types;
    a mapping is what would be a duplicated registry.
    """
    tree = ast.parse(module.read_text())
    prose = {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in prose
    }


def test_no_module_hardcodes_a_role_to_axis_mapping() -> None:
    """The registry is the only place the correspondence is written.

    Checked on data rather than prose. A module that *calls*
    `semantics.source_axis(role)` is asking the registry, which is the correct
    use; a module that writes a role name beside an axis as data would be a
    second copy of the mapping.
    """
    app = Path(__file__).parents[2] / "app"
    roles = {role.value for role in DimensionRole}
    for module in app.rglob("*.py"):
        if module.name == "dimensions.py" and module.parent.name == "taxonomy":
            continue
        data = _data_strings(module)
        # Naming a role as data is ordinary - `ComparisonField` names them all,
        # by customer-facing role rather than by column, which is the point.
        # Writing one *beside* `source_axis` is the duplication this forbids.
        if "source_axis" not in data:
            continue
        named = sorted(roles & data)
        assert named == [], f"{module.name} pairs {named} with source_axis"


def _yaml(body: str) -> str:
    return "version: v1\nsubcategories:\n  " + body + "\n"


MALFORMED = [
    ("version: v1\n", "subcategories"),
    ("version: ''\nsubcategories: {sofa: {}}\n", "version"),
    ("subcategories: {sofa: {}}\n", "version"),
    (_yaml("sofa:\n    roles: {wingspan: {source_axis: length}}"), "unknown value"),
    (_yaml("sofa:\n    roles: {depth: {source_axis: girth}}"), "unknown value"),
    (_yaml("sofa:\n    roles: {depth: {}}"), "source_axis"),
    (
        _yaml(
            "sofa:\n    roles: {depth: {source_axis: width}}\n"
            "    unsupported_roles: {depth: {reason: insufficient_coverage}}"
        ),
        "both supported and unsupported",
    ),
    (_yaml("sofa:\n    unsupported_roles: {depth: {}}"), "reason"),
    (_yaml("sofa:\n    unsupported_roles: {depth: {reason: because}}"), "unknown value"),
    (_yaml("carpet:\n    planar_pair: {axes: [length], mode: unordered}"), "two distinct axes"),
    (
        _yaml("carpet:\n    planar_pair: {axes: [length, length], mode: unordered}"),
        "two distinct axes",
    ),
    (_yaml("carpet:\n    planar_pair: {axes: [length, width], mode: ordered}"), "unordered"),
    ("[]\n", "mapping"),
    ("::: not yaml :::\n", "YAML"),
]


@pytest.mark.parametrize(("document", "reason"), MALFORMED)
def test_a_malformed_registry_is_rejected(
    tmp_path: Path, document: str, reason: str
) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text(document)

    with pytest.raises(TaxonomyConfigurationError, match=reason):
        load_dimension_semantics(path)


def test_a_mapping_for_an_unapproved_subcategory_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "version: v1\nsubcategories:\n  hovercraft:\n    roles: {depth: {source_axis: width}}\n"
    )

    with pytest.raises(TaxonomyConfigurationError, match="approved commerce subcategory"):
        load_dimension_semantics(path, taxonomy=TAXONOMY)


# ── routing in query understanding ──────────────────────────────────────────


class FakeLLMClient:
    def __init__(self, interpretation: CommerceInterpretation) -> None:
        self._interpretation = interpretation

    @property
    def model(self) -> str:
        return "fake-model"

    async def parse(self, **_: Any) -> Any:
        return self._interpretation


async def _interpret(
    *dimensions: DimensionInterpretation,
    subcategory: str | None = "sofa",
    category: str = "seating",
    planar: PlanarDimensionInterpretation | None = None,
) -> Any:
    interpretation = CommerceInterpretation(
        commerce_category=category,
        commerce_subcategory=subcategory,
        dimensions=list(dimensions),
        planar_dimensions=planar,
    )
    service = QueryUnderstandingService(
        cast(StructuredLLMClient, FakeLLMClient(interpretation)),
        TAXONOMY,
        ATTRIBUTES,
        SEMANTICS,
    )
    return await service.interpret("a message")


def _dim(
    role: DimensionRole | None, kind: Kind, unit: str | None = "cm", **values: str
) -> DimensionInterpretation:
    return DimensionInterpretation(role=role, kind=kind, unit=unit, **values)


async def test_a_supported_measurement_becomes_a_constraint() -> None:
    outcome = await _interpret(_dim(WIDTH, Kind.MAX, max_value="220"))

    assert isinstance(outcome, ResolvedSearch)
    constraint = outcome.request.dimensions[0]
    assert constraint.role is WIDTH
    assert constraint.kind is Kind.MAX
    assert constraint.max_cm == Decimal("220")


async def test_source_units_are_preserved_as_provenance() -> None:
    outcome = await _interpret(_dim(WIDTH, Kind.TARGET, unit="ft", target_value="7"))

    assert isinstance(outcome, ResolvedSearch)
    constraint = outcome.request.dimensions[0]
    assert constraint.target_cm == Decimal("213.36")
    assert constraint.source_unit == "ft"
    assert constraint.source_value == "7"


@pytest.mark.parametrize(
    ("unit", "value", "expected"),
    [("cm", "220", "220"), ("mm", "2200", "220.0"), ("m", "2.2", "220.0"),
     ("in", "86", "218.44"), ("inches", "86", "218.44"), ("ft", "7", "213.36")],
)
async def test_every_supported_unit_converts(unit: str, value: str, expected: str) -> None:
    outcome = await _interpret(_dim(WIDTH, Kind.MAX, unit=unit, max_value=value))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.dimensions[0].max_cm == Decimal(expected)


async def test_a_target_is_never_turned_into_a_maximum() -> None:
    outcome = await _interpret(_dim(WIDTH, Kind.TARGET, target_value="220"))

    assert isinstance(outcome, ResolvedSearch)
    constraint = outcome.request.dimensions[0]
    assert constraint.kind is Kind.TARGET
    assert constraint.target_cm == Decimal("220")
    assert constraint.max_cm is None
    assert constraint.min_cm is None


async def test_several_measurements_are_all_carried() -> None:
    outcome = await _interpret(
        _dim(WIDTH, Kind.MAX, max_value="220"), _dim(DEPTH, Kind.MAX, max_value="100")
    )

    assert isinstance(outcome, ResolvedSearch)
    assert {d.role for d in outcome.request.dimensions} == {WIDTH, DEPTH}


# ── the two failure modes are kept apart ────────────────────────────────────


async def test_a_number_with_no_stated_measurement_asks() -> None:
    """"a sofa under 200 cm" - ambiguous intent, one question settles it."""
    outcome = await _interpret(_dim(None, Kind.MAX, max_value="200"))

    assert isinstance(outcome, ClarificationRequired)
    assert outcome.reason is ClarificationReason.MISSING_DIMENSION_ROLE


async def test_a_number_with_no_unit_is_centimetres() -> None:
    """Furniture is discussed in centimetres and the catalog records it in
    centimetres, so "a sofa under 200" has one sensible reading - and asking
    which unit they meant was a question with one possible answer (M23 1).

    Room measurements are not covered by this: "5 by 5" is metres there and
    "400 by 500" is centimetres, so that path still asks.
    """
    outcome = await _interpret(_dim(WIDTH, Kind.MAX, unit=None, max_value="200"))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.dimensions[0].max_cm == Decimal("200")


async def test_a_unit_we_do_not_recognise_still_asks() -> None:
    """A different absence. No unit is a convention we can apply; a word the
    vocabulary does not contain is one we cannot act on, and converting it
    would be converting a number we did not understand."""
    outcome = await _interpret(_dim(WIDTH, Kind.MAX, unit="cubits", max_value="200"))

    assert isinstance(outcome, ClarificationRequired)
    assert outcome.reason is ClarificationReason.MISSING_DIMENSION_UNIT


@pytest.mark.parametrize("subcategory", ["bed", "sectional-sofa", "chair"])
async def test_a_clear_measurement_on_an_unreliable_family_is_unsupported(
    subcategory: str,
) -> None:
    """Their intent was clear. Asking again cannot repair the catalog."""
    category = {"bed": "bedroom", "sectional-sofa": "seating", "chair": "seating"}[subcategory]
    outcome = await _interpret(
        _dim(LENGTH if subcategory == "bed" else WIDTH, Kind.MAX, max_value="200"),
        subcategory=subcategory,
        category=category,
    )

    # Disjoint types: a caller handling only clarifications cannot receive
    # this, which mypy enforces statically.
    assert isinstance(outcome, UnsupportedDimensionRequirement)
    unsupported = outcome.unsupported_dimensions[0]
    assert unsupported.max_cm == Decimal("200")
    assert unsupported.reason is not UnsupportedDimensionReason.ROLE_NOT_DEFINED


async def test_the_unsupported_outcome_keeps_the_rest_of_the_request() -> None:
    outcome = await _interpret(
        _dim(LENGTH, Kind.MAX, max_value="200"), subcategory="bed", category="bedroom"
    )

    assert isinstance(outcome, UnsupportedDimensionRequirement)
    assert outcome.request.commerce_category == "bedroom"
    assert outcome.request.commerce_subcategory == "bed"
    assert outcome.request.dimensions == ()


async def test_no_measurement_resolves_normally() -> None:
    outcome = await _interpret()

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.dimensions == ()
    assert outcome.request.planar_dimensions is None


# ── planar ──────────────────────────────────────────────────────────────────


async def test_a_pair_on_a_planar_family_is_carried() -> None:
    outcome = await _interpret(
        subcategory="carpet",
        category="decor",
        planar=PlanarDimensionInterpretation(first_value="200", second_value="300", unit="cm"),
    )

    assert isinstance(outcome, ResolvedSearch)
    planar = outcome.request.planar_dimensions
    assert planar is not None
    assert planar.sides == (Decimal("200"), Decimal("300"))


async def test_a_pair_given_in_the_other_order_is_the_same_request() -> None:
    both = []
    for first, second in (("200", "300"), ("300", "200")):
        outcome = await _interpret(
            subcategory="carpet",
            category="decor",
            planar=PlanarDimensionInterpretation(
                first_value=first, second_value=second, unit="cm"
            ),
        )
        assert isinstance(outcome, ResolvedSearch)
        assert outcome.request.planar_dimensions is not None
        both.append(outcome.request.planar_dimensions.sides)

    assert both[0] == both[1]


async def test_a_pair_on_a_non_planar_family_asks() -> None:
    outcome = await _interpret(
        planar=PlanarDimensionInterpretation(first_value="200", second_value="90", unit="cm")
    )

    assert isinstance(outcome, ClarificationRequired)
    assert outcome.reason is ClarificationReason.MISSING_DIMENSION_ROLE


def test_there_is_no_three_number_dimension_input() -> None:
    """No family had proven three-number positional semantics."""
    assert "third_value" not in PlanarDimensionInterpretation.model_fields
    assert set(PlanarDimensionInterpretation.model_fields) == {
        "first_value", "second_value", "unit", "strength"
    }


def test_the_agent_is_told_not_to_ask_which_unit() -> None:
    """The clarification the customer saw came from the decision agent, before
    interpretation ever ran - so defaulting the unit downstream was not enough
    on its own (M23 1)."""
    from app.prompts.customer_commerce.v1 import INSTRUCTIONS

    flat = " ".join(INSTRUCTIONS.split())

    assert "A MEASUREMENT WITHOUT A UNIT IS CENTIMETRES" in flat
    assert "do not ask which unit they meant" in flat
    assert "a unit you do not recognise" in flat
    assert "a *room* size has no such convention" in flat
