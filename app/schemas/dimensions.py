"""Dimension value types.

The contracts live here; the conversion logic and its constants live in
:mod:`app.services.dimensions`. Keeping the types in the schema layer lets
product contracts reference them without a service-layer import.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict


class DimensionUnit(StrEnum):
    MILLIMETRE = "mm"
    CENTIMETRE = "cm"
    METRE = "m"
    INCH = "in"
    FOOT = "ft"


# Exact factors: Decimal keeps 10 in -> 25.4 cm exact, with no float drift.
CENTIMETRES_PER_UNIT: Final[dict[DimensionUnit, Decimal]] = {
    DimensionUnit.MILLIMETRE: Decimal("0.1"),
    DimensionUnit.CENTIMETRE: Decimal("1"),
    DimensionUnit.METRE: Decimal("100"),
    DimensionUnit.INCH: Decimal("2.54"),
    DimensionUnit.FOOT: Decimal("30.48"),
}

# Accepted spellings. Merchant data reaches `dimension_unit` unvalidated, so the
# real catalog carries several of these (verified: cm, in, inch, mm, m), and a
# customer may phrase a unit any of these ways too.
UNIT_ALIASES: Final[dict[str, DimensionUnit]] = {
    "mm": DimensionUnit.MILLIMETRE,
    "millimeter": DimensionUnit.MILLIMETRE,
    "millimeters": DimensionUnit.MILLIMETRE,
    "millimetre": DimensionUnit.MILLIMETRE,
    "millimetres": DimensionUnit.MILLIMETRE,
    "cm": DimensionUnit.CENTIMETRE,
    "centimeter": DimensionUnit.CENTIMETRE,
    "centimeters": DimensionUnit.CENTIMETRE,
    "centimetre": DimensionUnit.CENTIMETRE,
    "centimetres": DimensionUnit.CENTIMETRE,
    "m": DimensionUnit.METRE,
    "meter": DimensionUnit.METRE,
    "meters": DimensionUnit.METRE,
    "metre": DimensionUnit.METRE,
    "metres": DimensionUnit.METRE,
    "in": DimensionUnit.INCH,
    "inch": DimensionUnit.INCH,
    "inches": DimensionUnit.INCH,
    "ft": DimensionUnit.FOOT,
    "foot": DimensionUnit.FOOT,
    "feet": DimensionUnit.FOOT,
}


def parse_unit(raw: str | None) -> DimensionUnit | None:
    """Resolve a unit string, or ``None`` when it is not recognised.

    Never falls back to a default: an unrecognised unit stays unresolved, so a
    dimension can never be compared as though it were centimetres.
    """
    if raw is None:
        return None
    return UNIT_ALIASES.get(raw.strip().lower())


def to_centimetres(value: Decimal, unit: DimensionUnit) -> Decimal:
    return value * CENTIMETRES_PER_UNIT[unit]


class DimensionStatus(StrEnum):
    NORMALISED = "normalised"
    """The unit resolved; every present axis has a centimetre value."""

    UNKNOWN_UNIT = "unknown_unit"
    """Axes exist but the unit is missing or unsupported. No cm values."""

    ABSENT = "absent"
    """The product carries no dimensions at all."""


class RawDimensions(BaseModel):
    """Dimensions exactly as the merchant supplied them.

    These values are NOT comparable as they stand: ``unit`` varies row to row
    (cm, mm, m, in, ft), is written unvalidated by the upstream Salla import,
    and may be absent. Normalise before filtering, ranking or reasoning.
    """

    model_config = ConfigDict(frozen=True)

    length: Decimal | None = None
    width: Decimal | None = None
    height: Decimal | None = None
    unit: str | None = None


class NormalisedDimensions(BaseModel):
    """Centimetre values, or an explicit statement that there are none.

    Centimetre fields are populated only when :attr:`status` is
    :attr:`DimensionStatus.NORMALISED`. Each field corresponds to the catalog
    column of the same name; no semantic re-labelling is applied.
    """

    model_config = ConfigDict(frozen=True)

    length_cm: Decimal | None = None
    width_cm: Decimal | None = None
    height_cm: Decimal | None = None
    unit: DimensionUnit | None = None
    status: DimensionStatus

    @property
    def is_usable(self) -> bool:
        """True when these values may be compared, filtered or ranked on."""
        return self.status is DimensionStatus.NORMALISED
