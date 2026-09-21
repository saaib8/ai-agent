"""Deterministic conversion of catalog dimensions to centimetres.

The unit vocabulary and its conversion factors live in
:mod:`app.schemas.dimensions`, so this normalizer and the SQL layer that filters
on dimensions share one source of truth. No conversion constant is written
twice.

Scope is deliberately narrow: **unit normalisation only**. Each axis is
converted independently and keeps its own identity. This module does not
decide what an axis *means*.

Different product types use the three columns differently - a carpet's third
axis is pile thickness, an artwork's is frame depth, a sofa's is height - so
there is no universal ``length = depth`` mapping, and none is applied here.
Axes are never reordered or ranked by magnitude, and no footprint major/minor
concept exists yet. Category-aware dimension semantics are designed later.

Unknown or missing units are never assumed to be centimetres. A product whose
unit cannot be resolved reports :attr:`DimensionStatus.UNKNOWN_UNIT` and has no
centimetre values at all, so a wrong number can never be produced from a guess
(CLAUDE.md 3.3).
"""

from __future__ import annotations

from app.schemas.dimensions import (
    DimensionStatus,
    NormalisedDimensions,
    RawDimensions,
    parse_unit,
    to_centimetres,
)


def normalise_dimensions(raw: RawDimensions) -> NormalisedDimensions:
    """Convert a product's raw dimensions to centimetres, axis by axis."""
    axes = (raw.length, raw.width, raw.height)
    if all(axis is None for axis in axes):
        return NormalisedDimensions(status=DimensionStatus.ABSENT)

    unit = parse_unit(raw.unit)
    if unit is None:
        return NormalisedDimensions(status=DimensionStatus.UNKNOWN_UNIT)

    length, width, height = (
        to_centimetres(axis, unit) if axis is not None else None for axis in axes
    )
    return NormalisedDimensions(
        length_cm=length,
        width_cm=width,
        height_cm=height,
        unit=unit,
        status=DimensionStatus.NORMALISED,
    )
