"""Sizes nothing in the store met are answered with the closest, said as such.

A customer's own search whose sizes find nothing, after every permitted
widening, shows the closest sizes instead of an empty screen: their sizes
widen band by band - nearest first - while every other requirement stands, and
the change is recorded so the reply says none is their size (CLAUDE.md 13.5).
A room piece's options never do this: their sizes are the designer's floor.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from app.agents.facts import searched_for
from app.core.config import RelaxationSettings
from app.schemas.agent_state import ActiveSearchState
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PlanarDimensionConstraint,
    PriceConstraint,
    ProductSearchRequest,
)
from app.schemas.product import EligibleProduct
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    PlanarDimensionSemantics,
    ResolvedSearch,
)
from app.schemas.relaxation import (
    ClosestSizeChange,
    DimensionRelaxationChange,
    RelaxableField,
    StopReason,
)
from app.schemas.retailer import RetailerContext
from app.services.controlled_search import ControlledRelaxationService
from app.services.dimension_policy import (
    EVERY_KIND_DIMENSION_RELAXATION_POLICY,
    V1_DIMENSION_RELAXATION_POLICY,
)
from app.services.discovery import ProductDiscoveryService
from app.services.relaxation import RelaxationPlanner
from app.services.search_pipeline import ProductSearchPipeline
from app.taxonomy.dimensions import DimensionRole, FloorSide

from tests.unit.test_search_pipeline import FakeHydration, FakeRanking

CONTEXT = RetailerContext(store_id=145)
LOCKED = ConstraintStrength.LOCKED
WIDTH = DimensionRole.OVERALL_WIDTH


class _Catalog:
    """Products as (longer side, shorter side, price), filtered the way
    PostgreSQL reads sides: a pair within 5%, a single size by its side."""

    def __init__(self, rows: dict[int, tuple[int, int, int]]) -> None:
        self.rows = rows
        self.requests: list[ProductSearchRequest] = []

    async def eligible_pool(
        self, request: ProductSearchRequest, context: RetailerContext
    ) -> tuple[EligibleProduct, ...]:
        self.requests.append(request)
        return tuple(
            EligibleProduct(product_id=n, price_amount=Decimal(price))
            for n, (longer, shorter, price) in self.rows.items()
            if _matches(request, Decimal(longer), Decimal(shorter), Decimal(price))
        )


def _matches(
    request: ProductSearchRequest, longer: Decimal, shorter: Decimal, price: Decimal
) -> bool:
    ceiling = request.price.max_amount if request.price is not None else None
    if ceiling is not None and price > ceiling:
        return False
    for c in request.dimensions:
        side = shorter if c.side is FloorSide.SHORTER or c.role is DimensionRole.DEPTH else longer
        low = c.min_cm if c.kind is not DimensionConstraintKind.TARGET else c.target_cm
        high = c.max_cm if c.kind is not DimensionConstraintKind.TARGET else c.target_cm
        if (low is not None and side < low) or (high is not None and side > high):
            return False
    pair = request.planar_dimensions
    if pair is not None:
        low, high = pair.sides
        share = Decimal("0.05")
        if not (low * (1 - share) <= shorter <= low * (1 + share)):
            return False
        if not (high * (1 - share) <= longer <= high * (1 + share)):
            return False
    return True


def _service(catalog: _Catalog) -> ControlledRelaxationService:
    settings = RelaxationSettings()
    return ControlledRelaxationService(
        cast(ProductDiscoveryService, catalog), RelaxationPlanner(settings), settings
    )


def _pair(first: str, second: str, *, price: str | None = None) -> ResolvedSearch:
    request = ProductSearchRequest(
        commerce_category="tables",
        commerce_subcategory="console",
        planar_dimensions=PlanarDimensionConstraint(
            first_cm=Decimal(first), second_cm=Decimal(second), source_unit="cm"
        ),
        **({"price": PriceConstraint(currency="SAR", max_amount=Decimal(price))} if price else {}),
    )
    return ResolvedSearch(
        request=request,
        semantics=ConstraintSemantics(
            subcategory=LOCKED,
            price_max=LOCKED if price else None,
            planar_dimension=PlanarDimensionSemantics(strength=LOCKED),
        ),
    )


def _narrower_than(cm: str, strength: ConstraintStrength = LOCKED) -> ResolvedSearch:
    request = ProductSearchRequest(
        commerce_category="tables",
        commerce_subcategory="nightstand",
        dimensions=(
            DimensionConstraint(
                role=WIDTH,
                side=FloorSide.LONGER,
                kind=DimensionConstraintKind.MAX,
                max_cm=Decimal(cm),
            ),
        ),
    )
    return ResolvedSearch(
        request=request,
        semantics=ConstraintSemantics(
            subcategory=LOCKED,
            dimensions=(DimensionConstraintSemantics(role=WIDTH, strength=strength),),
        ),
    )


def _depths(result: Any) -> dict[int, int]:
    return {c.product.product_id: c.relaxation_depth for c in result.candidates}


# A console 120 x 40: none within 5%, the nearest 120 x 35, then 140 x 42.
CONSOLES = {
    1: (120, 35, 900),  # 12.5% narrower: the first band
    2: (140, 42, 1100),  # 17% longer: the first band
    3: (165, 40, 1200),  # 37.5% longer: the third band
    4: (300, 90, 2500),  # far from it: never shown while nearer sizes are
}


async def test_a_size_nothing_meets_shows_the_closest_nearest_first() -> None:
    result = await _service(_Catalog(CONSOLES)).search(
        _pair("120", "40"), CONTEXT, explain_empty=True, closest_sizes=True
    )

    depths = _depths(result)
    assert set(depths) == {1, 2, 3}
    assert depths[1] == depths[2] < depths[3]
    assert result.stop_reason is StopReason.CLOSEST_SIZES
    assert result.exact_candidate_count == 0
    assert result.set_aside == ()


async def test_the_closest_are_recorded_with_what_was_asked() -> None:
    result = await _service(_Catalog(CONSOLES)).search(
        _pair("40", "120"), CONTEXT, closest_sizes=True
    )

    changes = [c for a in result.attempts for c in a.changes if isinstance(c, ClosestSizeChange)]
    assert changes and {c.asked for c in changes} == {"120 x 40 cm"}
    # Fewer than enough within the nearer bands, so the next band is tried too.
    assert [c.within_share for c in changes] == [Decimal("0.20"), Decimal("0.35"), Decimal("0.50")]
    assert all(c.field is RelaxableField.SIZE for c in changes)


async def test_every_other_requirement_still_stands() -> None:
    result = await _service(_Catalog(CONSOLES)).search(
        _pair("120", "40", price="1000"), CONTEXT, closest_sizes=True
    )

    assert set(_depths(result)) == {1}


async def test_any_size_only_when_even_the_widest_band_finds_none() -> None:
    far = {4: (300, 90, 2500), 5: (20, 10, 100)}

    result = await _service(_Catalog(far)).search(_pair("120", "40"), CONTEXT, closest_sizes=True)

    assert set(_depths(result)) == {4, 5}
    last = result.attempts[-1].changes[0]
    assert isinstance(last, ClosestSizeChange) and last.within_share is None


async def test_a_size_that_is_met_never_falls_back() -> None:
    exact = {**CONSOLES, 9: (121, 40, 800)}

    result = await _service(_Catalog(exact)).search(_pair("120", "40"), CONTEXT, closest_sizes=True)

    assert set(_depths(result)) == {9}
    assert result.stop_reason is not StopReason.CLOSEST_SIZES


async def test_without_the_switch_nothing_is_shown_in_place_of_the_size() -> None:
    """A room piece's options: the size is the designer's floor."""
    result = await _service(_Catalog(CONSOLES)).search(_pair("120", "40"), CONTEXT)

    assert result.candidates == ()


async def test_a_single_limit_widens_outward_from_its_own_figure() -> None:
    nightstands = {1: (55, 40, 400), 2: (70, 45, 500), 3: (90, 50, 600)}

    result = await _service(_Catalog(nightstands)).search(
        _narrower_than("50"), CONTEXT, closest_sizes=True
    )

    depths = _depths(result)
    assert set(depths) == {1, 2}  # 60 then 67.5: the 90 cm one is past 50% wider
    assert depths[1] < depths[2]


# ── the reply is told, and the search says what ran ─────────────────────────


async def test_a_customer_search_tells_the_reply_these_are_the_closest() -> None:
    pipeline = ProductSearchPipeline(
        _service(_Catalog(CONSOLES)),
        cast(Any, FakeRanking()),
        cast(Any, FakeHydration()),
        presentation_limit=3,
    )

    execution = await pipeline.execute(_pair("120", "40"), CONTEXT, closest_sizes=True)

    (item,) = execution.grounding.relaxations
    assert item.field is RelaxableField.SIZE
    assert item.original_value == "120 x 40 cm"
    assert "closest" in item.applied_value


def test_the_search_summary_says_the_size_and_unit_that_ran() -> None:
    def ran(resolved: ResolvedSearch) -> Any:
        active = ActiveSearchState(
            request=resolved.request, semantics=resolved.semantics, revision=1
        )
        return searched_for(active)["sizes"]

    assert ran(_pair("120", "40")) == "120 x 40 cm"
    assert ran(_narrower_than("50")) == "width at most 50 cm"


# ── a loose size widens for every kind ──────────────────────────────────────


def test_a_loose_size_of_any_kind_widens_its_shorter_side_too() -> None:
    loose = ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="tables",
            commerce_subcategory="console",
            dimensions=(
                DimensionConstraint(
                    role=DimensionRole.DEPTH,
                    side=FloorSide.SHORTER,
                    kind=DimensionConstraintKind.TARGET,
                    target_cm=Decimal("40"),
                ),
            ),
        ),
        semantics=ConstraintSemantics(
            subcategory=LOCKED,
            dimensions=(
                DimensionConstraintSemantics(
                    role=DimensionRole.DEPTH, strength=ConstraintStrength.APPROXIMATE
                ),
            ),
        ),
    )
    settings = RelaxationSettings()

    every_kind = RelaxationPlanner(
        settings, dimension_policy=EVERY_KIND_DIMENSION_RELAXATION_POLICY
    ).plan(loose)
    v1 = RelaxationPlanner(settings, dimension_policy=V1_DIMENSION_RELAXATION_POLICY).plan(loose)

    widened = [c for step in every_kind for c in step.changes]
    assert [c.applied_max_cm for c in widened if isinstance(c, DimensionRelaxationChange)] == [
        Decimal("42.0000"),
        Decimal("44.0000"),
    ]
    assert v1 == ()


def test_a_locked_size_is_never_widened_step_by_step() -> None:
    plan = RelaxationPlanner(
        RelaxationSettings(), dimension_policy=EVERY_KIND_DIMENSION_RELAXATION_POLICY
    ).plan(_narrower_than("50"))

    assert plan == ()
