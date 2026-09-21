"""Verified facts the design specialist may reason with.

Two pure projections and one comparison, sharing a principle: a design model
may know *about* a product without being able to name one. An anchor carries
what a room is designed around - kind, size, colour, style - and nothing that
identifies it, because a specialist that could name a product could recommend
one, and choosing products belongs to the catalog (CLAUDE.md 3.3).

Everything here is pure. No I/O, no clock, no model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal

from app.schemas.design import AnchorDimension, AnchorProduct, FitAssessment, FitVerdict
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.geometry import RoomGeometry, RoomMeasurementRole
from app.schemas.product import ProductCandidate
from app.taxonomy.dimensions import DimensionRole, DimensionSemantics, SourceAxis

_AXIS_FIELD: dict[SourceAxis, str] = {
    SourceAxis.LENGTH: "length_cm",
    SourceAxis.WIDTH: "width_cm",
    SourceAxis.HEIGHT: "height_cm",
}

_FIT_BASIS: dict[DimensionRole, RoomMeasurementRole] = {
    DimensionRole.HEIGHT: RoomMeasurementRole.CEILING_HEIGHT,
}
"""Which stated limit a product measurement may be compared against.

One entry, because only one relationship holds without knowing where the
product goes. An object taller than the room does not go in - whatever it is,
however it is turned, wherever it stands. Height needs no placement.

**Every horizontal comparison is absent, and the reason is worth stating.**
`dimension_semantics_v1` establishes what a dimension *means* for a product
type - that a sofa's along-wall span is stored in `length`, that a dining
table's long side is its `length` and not a wall span. It does not establish
where the product is *placed*, and nothing in this service does. A dining
table, a centre table, a service table and an office table all have an
`overall_width`, and none of them is inherently against a wall. Comparing any
of them to a usable wall would answer a question of applicability that nobody
has answered.

So `OVERALL_WIDTH`, `LENGTH` and `DEPTH` map to nothing, and neither do the
room's own length and width: a room being five metres long does not establish
that a sofa fits along any particular wall.

**The later M12 path, when whole-room candidate execution needs it:** the
customer says which limit applies to which piece - "this wall is for the sofa"
- and that explicit applicability, plus the verified product dimension, makes
the comparison deterministic. It arrives as a targeted-measurement contract
designed against that requirement, not as a placement table guessed at here.

Absence means no comparison, never a guess.
"""


def project_anchor(
    product: ProductCandidate,
    *,
    dimensions: DimensionSemantics,
    locked: bool = False,
    quantity: int = 1,
) -> AnchorProduct | None:
    """One verified product as design facts, or None if it has no classification.

    An unclassified product cannot anchor a design: there is nothing to say
    about what it is, and deriving a type from its name is exactly what this
    service must not do (CLAUDE.md 6.1).

    `quantity` is supplied by the caller, never read off the product: the
    catalog knows what something is and not how many of it a room holds.
    """
    category = product.commerce.category
    if category is None:
        return None
    return AnchorProduct(
        commerce_category=category,
        commerce_subcategory=product.commerce.subcategory,
        seating_capacity=product.commerce.seating_capacity,
        main_color=product.main_color,
        styles=product.styles,
        dimensions=_anchor_dimensions(product, dimensions),
        locked=locked,
        quantity=quantity,
    )


def project_anchors(
    products: Sequence[ProductCandidate],
    *,
    dimensions: DimensionSemantics,
    locked_product_ids: Iterable[int] = (),
    quantities: Mapping[int, int] | None = None,
) -> tuple[AnchorProduct, ...]:
    """Anchors for a freshly hydrated bundle, in the order given.

    Built from a live read, never from state: a product deactivated since the
    customer chose it is simply absent from `products` and produces no anchor,
    rather than an assertion made from a remembered fact.

    `quantities` maps a product id to how many of it the room holds, summed by
    the caller across however many bundle lines carry it. The anchor keeps the
    physical count and loses the lines, which is exactly the boundary: the
    specialist needs to know there are two, and must never learn which records
    said so.
    """
    locked = set(locked_product_ids)
    counts = quantities or {}
    projected = (
        project_anchor(
            product,
            dimensions=dimensions,
            locked=product.product_id in locked,
            quantity=counts.get(product.product_id, 1),
        )
        for product in products
    )
    return tuple(anchor for anchor in projected if anchor is not None)


def _anchor_dimensions(
    product: ProductCandidate, semantics: DimensionSemantics
) -> tuple[AnchorDimension, ...]:
    """Measurements by customer-facing role, never by stored column.

    A sofa's along-wall span lives in `length` and its depth in `width`, with
    no global correspondence, so the registry resolves each role for this
    product type and nothing is read positionally (CLAUDE.md 15.1).

    Only normalised measurements are projected. An unresolved unit yields no
    dimension at all rather than a number whose unit nobody knows.
    """
    measurements = product.dimensions
    if measurements.status is not DimensionStatus.NORMALISED:
        return ()

    subcategory = product.commerce.subcategory
    projected: list[AnchorDimension] = []
    for role in sorted(semantics.supported_roles(subcategory)):
        axis = semantics.source_axis(subcategory, role)
        if axis is None:
            continue
        value = _read_axis(measurements, axis)
        if value is not None and value > 0:
            projected.append(AnchorDimension(role=role, centimetres=value))
    return tuple(projected)


def _read_axis(
    measurements: NormalisedDimensions, axis: SourceAxis
) -> Decimal | None:
    value: Decimal | None = getattr(measurements, _AXIS_FIELD[axis])
    return value


def assess_fit(
    *, role: DimensionRole, product_cm: Decimal | None, geometry: RoomGeometry | None
) -> FitAssessment:
    """One product measurement against one limit the customer stated.

    Arithmetic only, and the result says only what it checked. A piece within
    the wall you described is *within a known limit* - not proof that it fits
    your room, which would need to account for the doorway it comes through,
    what is already along that wall, and how much floor you want left. The
    verdict names the limit rather than the room for exactly that reason.

    Refuses far more often than it decides, and deliberately: without an
    applicable stated measurement the honest answer is that we cannot tell.
    Several usable walls with no indication which one was meant is also
    undecidable, because picking the longest would answer a different question.
    """
    basis = _FIT_BASIS.get(role)
    if basis is None or product_cm is None or geometry is None:
        return FitAssessment(verdict=FitVerdict.INSUFFICIENT_GEOMETRY, role=role)

    available = geometry.one(basis)
    if available is None:
        return FitAssessment(verdict=FitVerdict.INSUFFICIENT_GEOMETRY, role=role)

    within = product_cm <= available.centimetres
    return FitAssessment(
        verdict=(
            FitVerdict.WITHIN_KNOWN_LIMIT if within else FitVerdict.EXCEEDS_KNOWN_LIMIT
        ),
        role=role,
        product_cm=product_cm,
        available_cm=available.centimetres,
    )
