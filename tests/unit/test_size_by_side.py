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
from app.repositories.products import _LONG_SIDE, _SHORT_SIDE, _measured
from app.schemas.discovery import DimensionConstraint, DimensionConstraintKind
from app.taxonomy.dimensions import (
    DimensionRole,
    FloorSide,
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


@pytest.mark.parametrize(
    ("subcategory", "role"),
    [
        ("sectional-sofa", DimensionRole.OVERALL_WIDTH),
        ("carpet", DimensionRole.OVERALL_WIDTH),
        ("bed", DimensionRole.HEIGHT),
    ],
)
def test_what_the_shape_or_the_data_cannot_answer_is_still_refused(
    subcategory: str, role: DimensionRole
) -> None:
    """A corner piece has no single width, a rug's sides are a pair, and an
    unreliable height column is no better read by side."""
    assert BY_SIDE.refusal(subcategory, role) is not None
    assert BY_SIDE.measured_by(subcategory, role, None) is None


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


def test_a_kind_the_registry_does_not_record_is_not_opened_by_side() -> None:
    """A dining set, a mirror or a canvas has no floor sides to read."""
    assert BY_SIDE.refusal("dining-set", DimensionRole.OVERALL_WIDTH) is not None
    assert BY_SIDE.measured_by("dining-set", DimensionRole.OVERALL_WIDTH, None) is None


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
    [("bed", True), ("office-table", True), ("carpet", True), ("sectional-sofa", False)],
)
def test_a_pair_of_sides_reads_for_any_piece_with_one_footprint(
    subcategory: str, reads: bool
) -> None:
    """ "A bed 160 x 200", "a desk 120 x 60": the smaller figure is the short
    side, the larger the long one - not for a corner set, which has no single
    footprint."""
    assert BY_SIDE.reads_pair(subcategory) is reads
    assert not BY_SIDE.reads_pair("dining-set")
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
