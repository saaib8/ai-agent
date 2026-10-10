"""Product sizes read with no convention: the longer and shorter floor side.

A merchant may store a sofa's width in `length` or in `width`; the longer floor
side is the longer whatever its column, so ranking by it needs no convention
(docs/designer-led-shopping-plan.md, 4.2a). For a kind the store's own data
shows to be long and shallow (`SubcategoryShelf.long_and_shallow`) that side is
the width - which is what lets a designer's "about two-thirds of the sofa"
compare like with like in any store.

Sizes here only order products. Nothing is filtered on them and nothing is
said about a product's size from them: what a customer reads is the card.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.config import SizeSettings
from app.schemas.dimensions import NormalisedDimensions
from app.taxonomy.dimensions import FloorSide, SourceAxis


@dataclass(frozen=True, slots=True)
class FloorSides:
    long_cm: Decimal
    short_cm: Decimal


def floor_sides(dimensions: NormalisedDimensions, size: SizeSettings) -> FloorSides | None:
    """A product's floor sides, longer first, or None when either is missing
    or outside a plausible range - a slip is not read as a size."""
    if not dimensions.is_usable:
        return None
    first, second = dimensions.length_cm, dimensions.width_cm
    if first is None or second is None:
        return None
    return sides_of(first, second, size)


def sides_of(first: Decimal, second: Decimal, size: SizeSettings) -> FloorSides | None:
    """Two floor sides in centimetres, ordered, if both are plausible."""
    if not all(size.min_side_cm <= side <= size.max_side_cm for side in (first, second)):
        return None
    return FloorSides(long_cm=max(first, second), short_cm=min(first, second))


def made_to_fit(short_cm: Decimal | None, fit_cm: Decimal, size: SizeSettings) -> bool:
    """Whether a piece's shorter floor side is the width that goes in the pick
    - a 180 mattress for a frame that takes 180 - within `fit_share`. One rule
    for the order and for what the reply may say of it."""
    return short_cm is not None and abs(short_cm - fit_cm) <= fit_cm * size.fit_share


def size_distance(long_cm: Decimal | None, target_cm: Decimal | None) -> Decimal | None:
    """How far a product's longer side is from the size asked for, as a share
    of it - None when there is no target or no usable size, which ranks after
    every product that has one."""
    if target_cm is None or long_cm is None or target_cm <= 0:
        return None
    return abs(long_cm - target_cm) / target_cm


_COLUMN_VALUES: dict[SourceAxis, str] = {
    SourceAxis.LENGTH: "length_cm",
    SourceAxis.WIDTH: "width_cm",
    SourceAxis.HEIGHT: "height_cm",
}


def measurement(dimensions: NormalisedDimensions, by: SourceAxis | FloorSide) -> Decimal | None:
    """One stated size of a piece, read the way search reads it: a column, or
    its longer or shorter floor side whatever column holds it. None when the
    sizes are unusable or a floor side is missing."""
    if not dimensions.is_usable:
        return None
    if isinstance(by, FloorSide):
        first, second = dimensions.length_cm, dimensions.width_cm
        if first is None or second is None:
            return None
        return max(first, second) if by is FloorSide.LONGER else min(first, second)
    value: Decimal | None = getattr(dimensions, _COLUMN_VALUES[by])
    return value
