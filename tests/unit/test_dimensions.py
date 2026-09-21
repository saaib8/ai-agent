"""Catalog dimension normalisation: deterministic, per-axis, never guessed."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.dimensions import (
    DimensionStatus,
    DimensionUnit,
    RawDimensions,
    parse_unit,
    to_centimetres,
)
from app.services.dimensions import normalise_dimensions
from pydantic import ValidationError


def _raw(unit: str | None, **axes: str) -> RawDimensions:
    return RawDimensions(
        length=Decimal(axes["length"]) if "length" in axes else None,
        width=Decimal(axes["width"]) if "width" in axes else None,
        height=Decimal(axes["height"]) if "height" in axes else None,
        unit=unit,
    )


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        ("10", "mm", "1.0"),
        ("250", "cm", "250"),
        ("2", "m", "200"),
        ("10", "in", "25.40"),
        ("6", "ft", "182.88"),
        ("1", "ft", "30.48"),
        ("0", "cm", "0"),
    ],
)
def test_conversions_are_exact(value: str, unit: str, expected: str) -> None:
    """Decimal throughout: 10 in is exactly 25.4 cm, with no float drift."""
    resolved = parse_unit(unit)
    assert resolved is not None
    assert to_centimetres(Decimal(value), resolved) == Decimal(expected)


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("mm", DimensionUnit.MILLIMETRE),
        ("millimeter", DimensionUnit.MILLIMETRE),
        ("millimetres", DimensionUnit.MILLIMETRE),
        ("cm", DimensionUnit.CENTIMETRE),
        ("centimeter", DimensionUnit.CENTIMETRE),
        ("centimetres", DimensionUnit.CENTIMETRE),
        ("m", DimensionUnit.METRE),
        ("meter", DimensionUnit.METRE),
        ("metres", DimensionUnit.METRE),
        ("in", DimensionUnit.INCH),
        ("inch", DimensionUnit.INCH),
        ("inches", DimensionUnit.INCH),
        ("ft", DimensionUnit.FOOT),
        ("foot", DimensionUnit.FOOT),
        ("feet", DimensionUnit.FOOT),
        # Casing and padding: `dimension_unit` is unvalidated merchant text.
        ("  CM  ", DimensionUnit.CENTIMETRE),
        ("Inches", DimensionUnit.INCH),
    ],
)
def test_accepted_unit_spellings(spelling: str, expected: DimensionUnit) -> None:
    assert parse_unit(spelling) is expected


@pytest.mark.parametrize("unrecognised", ["", "  ", "yards", "cm.", "c m", "1", "inchs"])
def test_unrecognised_units_do_not_resolve(unrecognised: str) -> None:
    assert parse_unit(unrecognised) is None


def test_a_missing_unit_does_not_resolve() -> None:
    assert parse_unit(None) is None


def test_all_three_axes_convert_independently() -> None:
    result = normalise_dimensions(_raw("m", length="1", width="2", height="3"))

    assert result.status is DimensionStatus.NORMALISED
    assert result.unit is DimensionUnit.METRE
    assert (result.length_cm, result.width_cm, result.height_cm) == (
        Decimal("100"),
        Decimal("200"),
        Decimal("300"),
    )


def test_axes_keep_their_identity_and_order() -> None:
    """No reordering by magnitude, and no footprint major/minor concept yet.

    A carpet's third axis is pile thickness and an artwork's is frame depth, so
    there is no universal axis semantics to apply here.
    """
    result = normalise_dimensions(_raw("cm", length="300", width="5", height="1"))

    assert result.length_cm == Decimal("300")
    assert result.width_cm == Decimal("5")
    assert result.height_cm == Decimal("1")


@pytest.mark.parametrize(
    "axes",
    [{"length": "90"}, {"width": "220"}, {"height": "85"}, {"length": "90", "height": "85"}],
)
def test_partial_dimensions_convert_what_is_present(axes: dict[str, str]) -> None:
    result = normalise_dimensions(_raw("cm", **axes))

    assert result.status is DimensionStatus.NORMALISED
    present = {
        "length": result.length_cm,
        "width": result.width_cm,
        "height": result.height_cm,
    }
    for axis, value in present.items():
        assert (value is not None) is (axis in axes), axis


def test_no_dimensions_at_all_is_absent() -> None:
    result = normalise_dimensions(_raw("cm"))

    assert result.status is DimensionStatus.ABSENT
    assert result.unit is None
    assert not result.is_usable


@pytest.mark.parametrize("unit", [None, "", "   ", "yards", "unknown"])
def test_dimensions_without_a_usable_unit_never_default_to_centimetres(
    unit: str | None,
) -> None:
    """The single most important rule here: no guessing, ever (CLAUDE.md 3.3)."""
    result = normalise_dimensions(_raw(unit, length="90", width="220", height="85"))

    assert result.status is DimensionStatus.UNKNOWN_UNIT
    assert result.length_cm is None
    assert result.width_cm is None
    assert result.height_cm is None
    assert result.unit is None
    assert not result.is_usable


def test_an_unknown_unit_is_not_inferred_from_plausibility() -> None:
    """220 "looks like" centimetres. That is not evidence, and is not used."""
    assert normalise_dimensions(_raw(None, width="220")).width_cm is None
    assert normalise_dimensions(_raw(None, width="2.2")).width_cm is None


def test_normalised_dimensions_are_immutable() -> None:
    result = normalise_dimensions(_raw("cm", length="10"))
    with pytest.raises(ValidationError):
        result.length_cm = Decimal("99")  # type: ignore[misc]


def test_only_normalised_results_are_usable() -> None:
    assert normalise_dimensions(_raw("cm", length="10")).is_usable
    assert not normalise_dimensions(_raw(None, length="10")).is_usable
    assert not normalise_dimensions(_raw("cm")).is_usable


def test_conversion_factors_live_in_one_module() -> None:
    """One source of truth, shared by the normalizer and the SQL layer.

    The SQL dimension filter builds its conversion from these same tables, so a
    second copy of a factor anywhere would let the two paths disagree.
    """
    from pathlib import Path

    app_dir = Path(__file__).parents[2] / "app"
    offenders = [
        module.relative_to(app_dir).as_posix()
        for module in app_dir.rglob("*.py")
        if module.as_posix().endswith("schemas/dimensions.py") is False
        and "2.54" in module.read_text()
    ]
    assert offenders == []
