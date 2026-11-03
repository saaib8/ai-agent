"""Sizes read by the piece's floor sides, never by a store's column habits.

A customer means a side of the piece - a sofa's width is its long side, a bed's
width its short side. The model says which; code reads that side as the larger
or smaller of the two floor measurements, whatever column a merchant put each
in. So a store that keeps a sofa's width in `width` gets the same answer as one
that keeps it in `length`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.repositories.products import _LONG_FACE, _LONG_SIDE, _SHORT_FACE, _SHORT_SIDE, _measured
from app.schemas.dimensions import RawDimensions
from app.schemas.discovery import DimensionConstraint, DimensionConstraintKind
from app.services.dimensions import normalise_dimensions
from app.services.product_size import measurement
from app.taxonomy.dimensions import (
    DimensionRole,
    FaceSide,
    FloorSide,
    Measure,
    SourceAxis,
    UnsupportedDimensionReason,
    load_dimension_semantics,
    side_of,
)
from app.taxonomy.registry import load_taxonomy

TAXONOMY = load_taxonomy()
BY_SIDE = load_dimension_semantics(taxonomy=TAXONOMY, by_side=True)
BY_COLUMN = load_dimension_semantics(taxonomy=TAXONOMY)


@pytest.mark.parametrize(
    ("role", "said", "read"),
    [
        (DimensionRole.OVERALL_WIDTH, None, None),
        (DimensionRole.LENGTH, FloorSide.SHORTER, FloorSide.LONGER),
        (DimensionRole.OVERALL_WIDTH, FloorSide.SHORTER, FloorSide.SHORTER),
        (DimensionRole.LENGTH, None, FloorSide.LONGER),
        (DimensionRole.DEPTH, FloorSide.LONGER, FloorSide.SHORTER),
        (DimensionRole.HEIGHT, FloorSide.LONGER, None),
    ],
)
def test_depth_is_always_the_short_side_and_height_no_floor_side(
    role: DimensionRole, said: FloorSide | None, read: FloorSide | None
) -> None:
    """Whatever a model says, depth is the short side and height is height."""
    assert side_of(role, said) == read


def test_a_floor_measurement_is_read_by_its_side_whatever_the_store_calls_it() -> None:
    assert BY_SIDE.measured_by("sofa", DimensionRole.OVERALL_WIDTH, None) is FloorSide.LONGER
    assert BY_SIDE.measured_by("sofa", DimensionRole.DEPTH, None) is FloorSide.SHORTER
    assert (
        BY_SIDE.measured_by("bed", DimensionRole.OVERALL_WIDTH, FloorSide.SHORTER)
        is FloorSide.SHORTER
    )
    # Height still reads its own column.
    assert BY_SIDE.measured_by("sofa", DimensionRole.HEIGHT, None) is SourceAxis.HEIGHT


def test_switched_off_each_role_reads_the_reviewed_column() -> None:
    assert BY_COLUMN.measured_by("sofa", DimensionRole.OVERALL_WIDTH, None) is SourceAxis.LENGTH
    assert BY_COLUMN.measured_by("bed", DimensionRole.LENGTH, FloorSide.LONGER) is None


def test_a_refusal_about_which_column_holds_which_side_is_answered() -> None:
    """Beds were refused because their sides sit in either column; read by
    side, that no longer matters."""
    assert (
        BY_COLUMN.refusal("bed", DimensionRole.LENGTH)
        is UnsupportedDimensionReason.UNRELIABLE_AXIS_MAPPING
    )
    assert BY_SIDE.refusal("bed", DimensionRole.LENGTH) is None


@pytest.mark.parametrize("subcategory", ["carpet", "art-canvas", "mirror"])
def test_a_flat_thing_has_no_depth(subcategory: str) -> None:
    """A rug, a painting, a mirror: two sides and a thickness - never a depth."""
    assert BY_SIDE.refusal(subcategory, DimensionRole.DEPTH) is not None
    assert BY_SIDE.measured_by(subcategory, DimensionRole.DEPTH, None) is None


@pytest.mark.parametrize(
    ("role", "side", "read"),
    [
        (DimensionRole.HEIGHT, None, FaceSide.LONGER),
        (DimensionRole.LENGTH, None, FaceSide.LONGER),
        (DimensionRole.OVERALL_WIDTH, None, FaceSide.SHORTER),
        (DimensionRole.OVERALL_WIDTH, FloorSide.LONGER, FaceSide.LONGER),
    ],
)
def test_a_flat_thing_is_read_by_its_two_largest_sizes(
    role: DimensionRole, side: FloorSide | None, read: FaceSide
) -> None:
    """ "Art 100 cm tall" is its longer side, its width the shorter unless the
    customer meant the longer - whichever columns the store used."""
    assert BY_SIDE.measured_by("art-canvas", role, side) is read
    assert BY_SIDE.shown_by("mirror") == {
        DimensionRole.LENGTH: FaceSide.LONGER,
        DimensionRole.OVERALL_WIDTH: FaceSide.SHORTER,
    }
    assert BY_COLUMN.measured_by("art-canvas", role, side) is None


@pytest.mark.parametrize(
    ("length", "width", "height", "longer", "shorter"),
    [
        ("120", "2", "90", "120", "90"),  # the thickness in width, the height listed
        (None, "66", "55", "66", "55"),  # one floor column empty
        ("300", "200", None, "300", "200"),  # a rug as most stores list it
        ("300", "200", "1", "300", "200"),
        ("60", None, None, None, None),  # one size: no sides
    ],
)
def test_a_flat_things_sides_are_its_two_largest_listed_sizes(
    length: str | None,
    width: str | None,
    height: str | None,
    longer: str | None,
    shorter: str | None,
) -> None:
    sizes = normalise_dimensions(
        RawDimensions(
            length=None if length is None else Decimal(length),
            width=None if width is None else Decimal(width),
            height=None if height is None else Decimal(height),
            unit="cm",
        )
    )

    def expected(figure: str | None) -> Decimal | None:
        return None if figure is None else Decimal(figure)

    assert measurement(sizes, FaceSide.LONGER) == expected(longer)
    assert measurement(sizes, FaceSide.SHORTER) == expected(shorter)


def test_search_reads_a_flat_thing_by_its_face_sides() -> None:
    assert _measured(FaceSide.LONGER) is _LONG_FACE
    assert _measured(FaceSide.SHORTER) is _SHORT_FACE
    assert _measured(FloorSide.LONGER) is _LONG_SIDE


@pytest.mark.parametrize(
    ("subcategory", "role", "read"),
    [
        ("sectional-sofa", DimensionRole.OVERALL_WIDTH, FloorSide.LONGER),
        ("sofa-set", DimensionRole.DEPTH, FloorSide.SHORTER),
        ("carpet", DimensionRole.OVERALL_WIDTH, FaceSide.SHORTER),
        ("bed", DimensionRole.HEIGHT, SourceAxis.HEIGHT),
        ("nightstand", DimensionRole.HEIGHT, SourceAxis.HEIGHT),
    ],
)
def test_every_kind_is_searched_by_its_listed_size(
    subcategory: str, role: DimensionRole, read: Measure
) -> None:
    """The store's listed size is trusted like its price, in every store: a
    corner sofa's or a set's longer side is its width as listed."""
    assert BY_SIDE.refusal(subcategory, role) is None
    assert BY_SIDE.measured_by(subcategory, role, None) is read


def test_the_sides_are_the_larger_and_smaller_of_the_two_floor_columns() -> None:
    """Symmetric by construction: swapping a merchant's length and width
    columns cannot change which is the longer side."""
    assert _measured(FloorSide.LONGER) is _LONG_SIDE
    assert _measured(FloorSide.SHORTER) is _SHORT_SIDE
    longer = str(_LONG_SIDE.compile(compile_kwargs={"literal_binds": True}))
    shorter = str(_SHORT_SIDE.compile(compile_kwargs={"literal_binds": True}))
    assert "greatest(" in longer and "least(" in shorter
    for side in (longer, shorter):
        assert "core_product.length" in side and "core_product.width" in side


def test_a_constraint_keeps_its_side_and_old_sessions_still_read() -> None:
    stated = DimensionConstraint(
        role=DimensionRole.OVERALL_WIDTH,
        side=FloorSide.SHORTER,
        kind=DimensionConstraintKind.MAX,
        max_cm=Decimal("170"),
    )
    saved = DimensionConstraint.model_validate(
        {"role": "overall_width", "kind": "max", "max_cm": "220"}
    )

    assert DimensionConstraint.model_validate(stated.model_dump(mode="json")) == stated
    assert saved.side is None
    assert "side" not in saved.model_dump(mode="json")


async def test_a_bed_width_said_in_words_is_searched_on_its_short_side() -> None:
    from typing import cast

    from app.integrations.llm import StructuredLLMClient
    from app.schemas.query import CommerceInterpretation, DimensionInterpretation, ResolvedSearch
    from app.services.query_understanding import QueryUnderstandingService
    from app.taxonomy.attributes import load_catalog_attributes

    from tests.unit.test_query_understanding import FakeLLMClient

    client = FakeLLMClient(
        CommerceInterpretation(
            commerce_category="bedroom",
            commerce_subcategory="bed",
            dimensions=[
                DimensionInterpretation(
                    role=DimensionRole.OVERALL_WIDTH,
                    side=FloorSide.SHORTER,
                    kind=DimensionConstraintKind.MAX,
                    max_value="170",
                    unit="cm",
                )
            ],
        )
    )
    service = QueryUnderstandingService(
        cast(StructuredLLMClient, client), TAXONOMY, load_catalog_attributes(), BY_SIDE
    )

    outcome = await service.interpret("a bed no wider than 170 cm")

    assert isinstance(outcome, ResolvedSearch)
    (width,) = outcome.request.dimensions
    assert width.side is FloorSide.SHORTER and width.max_cm == Decimal("170")


@pytest.mark.parametrize(
    ("subcategory", "read"),
    [("sofa", FloorSide.LONGER), ("dining-table", FloorSide.SHORTER), ("bed", FloorSide.SHORTER)],
)
def test_a_width_nobody_placed_reads_the_side_the_registry_reads_it_on(
    subcategory: str, read: FloorSide
) -> None:
    """A dining table's or a bed's width is its short side, a sofa's its long
    one - also for a size saved before sides were recorded."""
    assert BY_SIDE.measured_by(subcategory, DimensionRole.OVERALL_WIDTH, None) is read


def test_a_kind_the_registry_does_not_record_is_read_by_side_too() -> None:
    """A dining set or a floor lamp: width its longer side, depth its shorter,
    height its height - one rule for every kind and every store."""
    assert BY_SIDE.shown_by("floor-lamp") == {
        DimensionRole.OVERALL_WIDTH: FloorSide.LONGER,
        DimensionRole.DEPTH: FloorSide.SHORTER,
        DimensionRole.HEIGHT: SourceAxis.HEIGHT,
    }
    assert BY_SIDE.measured_by("dining-set", DimensionRole.OVERALL_WIDTH, None) is FloorSide.LONGER
    assert BY_COLUMN.refusal("dining-set", DimensionRole.OVERALL_WIDTH) is not None, (
        "off: as before"
    )


def test_stated_sizes_are_read_the_way_search_reads_them() -> None:
    """A comparison row or the designer's facts: a bed stored either way round
    states the same length and width."""
    from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
    from app.services.product_size import measurement

    shown = BY_SIDE.shown_by("bed")
    stored_one_way = NormalisedDimensions(
        status=DimensionStatus.NORMALISED,
        length_cm=Decimal("200"),
        width_cm=Decimal("160"),
        height_cm=None,
    )
    stored_the_other = stored_one_way.model_copy(
        update={"length_cm": Decimal("160"), "width_cm": Decimal("200")}
    )

    for stored in (stored_one_way, stored_the_other):
        assert measurement(stored, shown[DimensionRole.LENGTH]) == Decimal("200")
        assert measurement(stored, shown[DimensionRole.OVERALL_WIDTH]) == Decimal("160")


@pytest.mark.parametrize(
    ("subcategory", "reads"),
    [("bed", True), ("office-table", True), ("carpet", True), ("sectional-sofa", True)],
)
def test_a_pair_of_sides_reads_for_every_kind(subcategory: str, reads: bool) -> None:
    """ "A bed 160 x 200", "a desk 120 x 60", "a corner sofa 300 x 160": the
    smaller figure is the short side, the larger the long one, as listed."""
    assert BY_SIDE.reads_pair(subcategory) is reads
    assert BY_SIDE.reads_pair("dining-set")
    assert not BY_COLUMN.reads_pair("bed")


def test_a_pair_matches_the_short_and_long_side_within_its_tolerance() -> None:
    from app.repositories.products import _planar_clause
    from app.schemas.discovery import PairMatch, PlanarDimensionConstraint

    pair = PlanarDimensionConstraint(first_cm=Decimal("200"), second_cm=Decimal("160"))
    clause = str(
        _planar_clause(
            PairMatch(
                constraint=pair,
                axes=(FloorSide.SHORTER, FloorSide.LONGER),
                tolerance=Decimal("0.05"),
            )
        ).compile(compile_kwargs={"literal_binds": True})
    )

    assert "least(" in clause and "greatest(" in clause
    assert "152.00" in clause and "168.00" in clause
    assert "190.00" in clause and "210.00" in clause
