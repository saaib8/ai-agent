"""Deterministic dimension filtering against real PostgreSQL.

The axis proofs matter most: a customer asking how *wide* a sofa is must be
answered from `length`, and asking how *deep* from `width`. Getting that
backwards returns confidently wrong products.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.core.config import DiscoverySettings, RelaxationSettings
from app.core.exceptions import UnsupportedDimensionRoleError
from app.db.tables import core_product, core_store
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PlanarDimensionConstraint,
    PriceConstraint,
    ProductSearchRequest,
    ProductSearchResult,
    ProductSort,
    SeatingCapacityConstraint,
)
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    ResolvedSearch,
)
from app.schemas.retailer import RetailerContext
from app.services.controlled_search import ControlledRelaxationService
from app.services.discovery import ProductDiscoveryService
from app.services.relaxation import RelaxationPlanner
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import DimensionRole, load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

STORE_A, STORE_B = 1, 2
CONTEXT_A = RetailerContext(store_id=STORE_A)
CONTEXT_B = RetailerContext(store_id=STORE_B)
SAR = "SAR"
MAX = DimensionConstraintKind.MAX
MIN = DimensionConstraintKind.MIN
RANGE = DimensionConstraintKind.RANGE
TARGET = DimensionConstraintKind.TARGET


def _product(
    product_id: int,
    store_id: int,
    *,
    category: str = "seating",
    subcategory: str = "sofa",
    length: str | None = None,
    width: str | None = None,
    height: str | None = None,
    unit: str | None = "cm",
    price: str = "2000.00",
    capacity: int | None = 3,
    color: str | None = "Beige",
    styles: str | None = "Modern",
) -> dict[str, Any]:
    return {
        "id": product_id,
        "uuid": uuid4(),
        "store_id": store_id,
        "name_english": f"Product {product_id}",
        "name_arabic": f"منتج {product_id}",
        "price_amount": Decimal(price),
        "price_unit": SAR,
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": "3-seater-sofa",
        "commerce_category": category,
        "commerce_subcategory": subcategory,
        "seating_capacity": capacity,
        "main_color": color,
        "styles": styles,
        "length": Decimal(length) if length else None,
        "width": Decimal(width) if width else None,
        "height": Decimal(height) if height else None,
        "dimension_unit": unit,
        "is_active": True,
        "detection": False,
    }


@pytest.fixture
async def catalog(writable_engine: AsyncEngine) -> None:
    async with writable_engine.begin() as connection:
        await connection.execute(
            core_store.insert(),
            [
                {
                    "id": store_id,
                    "uuid": uuid4(),
                    "name_english": f"Store {store_id}",
                    "name_arabic": None,
                    "active_status": True,
                }
                for store_id in (STORE_A, STORE_B)
            ],
        )
        await connection.execute(
            core_product.insert(),
            [
                # Sofas. length and width are deliberately crossed in value so a
                # mapping mistake changes the answer.
                _product(1, STORE_A, length="200", width="90", height="80"),
                _product(2, STORE_A, length="240", width="100", height="85"),
                _product(3, STORE_A, length="280", width="110", height="90"),
                # Same product expressed in other units.
                _product(10, STORE_A, length="2.2", width="0.95", height="0.85", unit="m"),
                _product(11, STORE_A, length="2100", width="900", height="800", unit="mm"),
                _product(12, STORE_A, length="86", width="35", height="33", unit="in"),
                _product(13, STORE_A, length="7", width="3", height="2.5", unit="ft"),
                # Unusable or missing dimension data.
                _product(20, STORE_A, length="200", width="90", height=None),
                _product(21, STORE_A, length=None, width="90", height="80"),
                _product(22, STORE_A, length="200", width="90", height="80", unit=None),
                _product(23, STORE_A, length="200", width="90", height="80", unit="cubits"),
                # Other supported families.
                _product(30, STORE_A, category="tables", subcategory="tv-table",
                         length="180", width="45", height="50", capacity=None),
                _product(31, STORE_A, category="tables", subcategory="tv-table",
                         length="220", width="45", height="50", capacity=None),
                _product(32, STORE_A, category="tables", subcategory="console",
                         length="130", width="38", height="80", capacity=None),
                _product(33, STORE_A, category="bedroom", subcategory="wardrobe",
                         length="270", width="60", height="220", capacity=None),
                _product(34, STORE_A, category="tables", subcategory="center-table",
                         length="120", width="70", height="45", capacity=None),
                _product(35, STORE_A, category="tables", subcategory="service-table",
                         length="45", width="45", height="55", capacity=None),
                _product(36, STORE_A, category="storage", subcategory="shelve",
                         length="90", width="38", height="180", capacity=None),
                _product(37, STORE_A, category="office", subcategory="office-table",
                         length="160", width="71", height="83", capacity=None),
                _product(38, STORE_A, category="dining", subcategory="dining-table",
                         length="200", width="100", height="75", capacity=None),
                # Carpets: the same rug stored in both orientations.
                _product(40, STORE_A, category="decor", subcategory="carpet",
                         length="200", width="300", height=None, capacity=None),
                _product(41, STORE_A, category="decor", subcategory="carpet",
                         length="300", width="200", height=None, capacity=None),
                _product(42, STORE_A, category="decor", subcategory="carpet",
                         length="200", width="250", height=None, capacity=None),
                _product(43, STORE_A, category="decor", subcategory="carpet",
                         length=None, width="300", height=None, capacity=None),
                # Another retailer holding an identical sofa.
                _product(50, STORE_B, length="200", width="90", height="80"),
            ],
        )


def _service(session: Any) -> ProductDiscoveryService:
    taxonomy = load_taxonomy()
    return ProductDiscoveryService(
        ProductRepository(session),
        taxonomy,
        DiscoverySettings(),
        load_catalog_attributes(),
        load_dimension_semantics(taxonomy=taxonomy),
    )


async def _search(
    database: Database, request: ProductSearchRequest, context: RetailerContext = CONTEXT_A
) -> ProductSearchResult:
    async with database.session() as session:
        return await _service(session).search(request, context)


def _ids(result: ProductSearchResult) -> list[int]:
    return [c.product_id for c in result.candidates]


def _request(
    *constraints: DimensionConstraint,
    category: str = "seating",
    subcategory: str | None = "sofa",
    **kwargs: Any,
) -> ProductSearchRequest:
    return ProductSearchRequest(
        commerce_category=category,
        commerce_subcategory=subcategory,
        dimensions=constraints,
        **kwargs,
    )


def _c(role: DimensionRole, kind: DimensionConstraintKind, **values: str) -> DimensionConstraint:
    return DimensionConstraint(
        role=role, kind=kind, **{k: Decimal(v) for k, v in values.items()}
    )


# ── the axis proofs ─────────────────────────────────────────────────────────


async def test_sofa_overall_width_reads_the_length_column(
    database: Database, catalog: None
) -> None:
    """Width <= 240 keeps the 200 and 240 sofas and drops the 280."""
    result = await _search(
        database, _request(_c(DimensionRole.OVERALL_WIDTH, MAX, max_cm="240"))
    )

    assert 1 in _ids(result) and 2 in _ids(result)
    assert 3 not in _ids(result)


async def test_sofa_overall_width_does_not_read_the_width_column(
    database: Database, catalog: None
) -> None:
    """Every sofa's `width` is under 240, so reading it would return all three."""
    result = await _search(
        database, _request(_c(DimensionRole.OVERALL_WIDTH, MAX, max_cm="240"))
    )

    assert 3 not in _ids(result), "a 280 cm sofa matched a 240 cm width limit"


async def test_sofa_depth_reads_the_width_column(
    database: Database, catalog: None
) -> None:
    """Depth <= 95 keeps only the 90 cm sofa."""
    result = await _search(
        database, _request(_c(DimensionRole.DEPTH, MAX, max_cm="95"))
    )

    assert 1 in _ids(result)
    assert 2 not in _ids(result) and 3 not in _ids(result)


async def test_sofa_depth_does_not_read_the_length_column(
    database: Database, catalog: None
) -> None:
    """No sofa's `length` is under 95, so reading it would return nothing."""
    result = await _search(
        database, _request(_c(DimensionRole.DEPTH, MAX, max_cm="95"))
    )

    assert _ids(result), "depth read the wrong axis and matched nothing"


async def test_sofa_height_reads_the_height_column(
    database: Database, catalog: None
) -> None:
    result = await _search(
        database, _request(_c(DimensionRole.HEIGHT, MAX, max_cm="82"))
    )

    assert 1 in _ids(result)
    assert 2 not in _ids(result)


@pytest.mark.parametrize(
    ("category", "subcategory", "role", "max_cm", "expected", "excluded"),
    [
        ("tables", "tv-table", DimensionRole.OVERALL_WIDTH, "200", 30, 31),
        ("tables", "console", DimensionRole.DEPTH, "40", 32, None),
        ("bedroom", "wardrobe", DimensionRole.HEIGHT, "225", 33, None),
        ("tables", "center-table", DimensionRole.OVERALL_WIDTH, "130", 34, None),
        ("tables", "service-table", DimensionRole.DEPTH, "50", 35, None),
        ("storage", "shelve", DimensionRole.HEIGHT, "190", 36, None),
        ("office", "office-table", DimensionRole.OVERALL_WIDTH, "170", 37, None),
        ("dining", "dining-table", DimensionRole.LENGTH, "210", 38, None),
        ("dining", "dining-table", DimensionRole.OVERALL_WIDTH, "110", 38, None),
    ],
)
async def test_each_supported_family_maps_its_roles(
    database: Database,
    catalog: None,
    category: str,
    subcategory: str,
    role: DimensionRole,
    max_cm: str,
    expected: int,
    excluded: int | None,
) -> None:
    result = await _search(
        database,
        _request(_c(role, MAX, max_cm=max_cm), category=category, subcategory=subcategory),
    )

    assert expected in _ids(result)
    if excluded is not None:
        assert excluded not in _ids(result)


# ── unsupported roles never reach SQL ───────────────────────────────────────


@pytest.mark.parametrize(
    ("subcategory", "role"),
    [
        ("sofa", DimensionRole.LENGTH),
        ("carpet", DimensionRole.OVERALL_WIDTH),
        ("nightstand", DimensionRole.OVERALL_WIDTH),
    ],
)
async def test_an_unsupported_role_is_refused(
    database: Database, catalog: None, subcategory: str, role: DimensionRole
) -> None:
    category = {"sofa": "seating", "carpet": "decor", "nightstand": "tables"}[subcategory]
    with pytest.raises(UnsupportedDimensionRoleError):
        await _search(
            database,
            _request(_c(role, MAX, max_cm="200"), category=category, subcategory=subcategory),
        )


async def test_a_planar_pair_is_refused_where_sides_have_an_order(
    database: Database, catalog: None
) -> None:
    with pytest.raises(UnsupportedDimensionRoleError):
        await _search(
            database,
            ProductSearchRequest(
                commerce_category="seating",
                commerce_subcategory="sofa",
                planar_dimensions=PlanarDimensionConstraint(
                    first_cm=Decimal("200"), second_cm=Decimal("300")
                ),
            ),
        )


# ── missing and unusable data ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("role", "max_cm", "missing_id"),
    [
        (DimensionRole.HEIGHT, "100", 20),
        (DimensionRole.OVERALL_WIDTH, "300", 21),
    ],
)
async def test_a_missing_dimension_never_satisfies_a_constraint(
    database: Database, catalog: None, role: DimensionRole, max_cm: str, missing_id: int
) -> None:
    result = await _search(database, _request(_c(role, MAX, max_cm=max_cm)))

    assert missing_id not in _ids(result)


@pytest.mark.parametrize("unusable_id", [22, 23])
async def test_a_missing_or_unknown_unit_never_satisfies_a_constraint(
    database: Database, catalog: None, unusable_id: int
) -> None:
    """Without a usable unit, 200 could be anything. It is not assumed to be cm."""
    result = await _search(
        database, _request(_c(DimensionRole.OVERALL_WIDTH, MAX, max_cm="300"))
    )

    assert unusable_id not in _ids(result)


# ── unit conversion in SQL ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("product_id", "max_cm", "should_match"),
    [
        (10, "220", True), (10, "219", False),    # 2.2 m  = 220 cm
        (11, "210", True), (11, "209", False),    # 2100 mm = 210 cm
        (12, "218.44", True), (12, "218", False),  # 86 in  = 218.44 cm
        (13, "213.36", True), (13, "213", False),  # 7 ft   = 213.36 cm
    ],
)
async def test_stored_units_convert_in_sql(
    database: Database, catalog: None, product_id: int, max_cm: str, should_match: bool
) -> None:
    result = await _search(
        database, _request(_c(DimensionRole.OVERALL_WIDTH, MAX, max_cm=max_cm))
    )

    assert (product_id in _ids(result)) is should_match


# ── operators ───────────────────────────────────────────────────────────────


async def test_min_operator(database: Database, catalog: None) -> None:
    result = await _search(
        database, _request(_c(DimensionRole.OVERALL_WIDTH, MIN, min_cm="240"))
    )

    assert 2 in _ids(result) and 3 in _ids(result)
    assert 1 not in _ids(result)


async def test_range_operator(database: Database, catalog: None) -> None:
    """240 cm, plus the metre, millimetre, inch and foot rows that convert into
    the band (220, 210, 218.44, 213.36). The 200 and 280 sofas fall outside."""
    result = await _search(
        database,
        _request(_c(DimensionRole.OVERALL_WIDTH, RANGE, min_cm="210", max_cm="250")),
    )

    assert _ids(result) == [2, 10, 11, 12, 13]
    assert 1 not in _ids(result) and 3 not in _ids(result)


async def test_target_seeds_with_exact_equality(database: Database, catalog: None) -> None:
    """The exact attempt only. Widening a target is a later milestone."""
    result = await _search(
        database, _request(_c(DimensionRole.OVERALL_WIDTH, TARGET, target_cm="240"))
    )

    assert _ids(result) == [2]
    assert 3 not in _ids(result)


async def test_several_dimensions_are_all_required(
    database: Database, catalog: None
) -> None:
    result = await _search(
        database,
        _request(
            _c(DimensionRole.OVERALL_WIDTH, MAX, max_cm="250"),
            _c(DimensionRole.DEPTH, MAX, max_cm="95"),
        ),
    )

    assert 1 in _ids(result)
    assert 2 not in _ids(result)  # within width, too deep
    assert 3 not in _ids(result)


# ── combination with the other filters ──────────────────────────────────────


async def test_dimensions_combine_with_price_capacity_colour_and_style(
    database: Database, catalog: None
) -> None:
    result = await _search(
        database,
        _request(
            _c(DimensionRole.OVERALL_WIDTH, MAX, max_cm="250"),
            price=PriceConstraint.at_most(Decimal("2500"), SAR),
            seating_capacity=SeatingCapacityConstraint.exactly(3),
            colors_any_of=("Beige",),
            styles_all_of=("Modern",),
        ),
    )

    assert 1 in _ids(result) and 2 in _ids(result)
    assert 3 not in _ids(result)


async def test_dimension_filtering_respects_retailer_scope(
    database: Database, catalog: None
) -> None:
    from_a = await _search(
        database, _request(_c(DimensionRole.OVERALL_WIDTH, MAX, max_cm="300"))
    )
    from_b = await _search(
        database, _request(_c(DimensionRole.OVERALL_WIDTH, MAX, max_cm="300")), CONTEXT_B
    )

    assert 50 not in _ids(from_a)
    assert _ids(from_b) == [50]


async def test_filtering_happens_before_the_limit(
    database: Database, catalog: None
) -> None:
    """A limit of 1 must return the narrowest eligible product, not an
    arbitrary row that the dimension filter would then have removed."""
    result = await _search(
        database,
        _request(
            _c(DimensionRole.OVERALL_WIDTH, MIN, min_cm="240"),
            limit=1,
            sort=ProductSort.PRICE_ASC,
        ),
    )

    assert len(result.candidates) == 1
    assert _ids(result)[0] in {2, 3}


async def test_price_sorting_still_holds_with_dimension_filters(
    database: Database, catalog: None
) -> None:
    result = await _search(
        database,
        _request(
            _c(DimensionRole.OVERALL_WIDTH, MAX, max_cm="300"), sort=ProductSort.PRICE_DESC
        ),
    )

    prices = [c.price_amount for c in result.candidates]
    assert prices == sorted(prices, reverse=True)


# ── carpet: an unordered pair ───────────────────────────────────────────────


def _rug(first: str, second: str) -> ProductSearchRequest:
    return ProductSearchRequest(
        commerce_category="decor",
        commerce_subcategory="carpet",
        planar_dimensions=PlanarDimensionConstraint(
            first_cm=Decimal(first), second_cm=Decimal(second)
        ),
    )


@pytest.mark.parametrize(("first", "second"), [("200", "300"), ("300", "200")])
async def test_a_rug_matches_in_either_orientation(
    database: Database, catalog: None, first: str, second: str
) -> None:
    result = await _search(database, _rug(first, second))

    assert sorted(_ids(result)) == [40, 41]


async def test_a_rug_of_a_different_size_does_not_match(
    database: Database, catalog: None
) -> None:
    result = await _search(database, _rug("200", "300"))

    assert 42 not in _ids(result)


async def test_a_rug_missing_a_side_does_not_match(
    database: Database, catalog: None
) -> None:
    result = await _search(database, _rug("200", "300"))

    assert 43 not in _ids(result)


# ── relaxation preserves every dimension ────────────────────────────────────


async def _relaxed(database: Database, resolved: ResolvedSearch) -> Any:
    async with database.session() as session:
        policy = RelaxationSettings()
        service = ControlledRelaxationService(
            _service(session), RelaxationPlanner(policy), policy
        )
        return await service.search(resolved, CONTEXT_A)


async def test_a_dimension_survives_every_price_widening(
    database: Database, catalog: None
) -> None:
    constraint = _c(DimensionRole.OVERALL_WIDTH, MAX, max_cm="240")
    result = await _relaxed(
        database,
        ResolvedSearch(
            request=_request(
                constraint, price=PriceConstraint.at_most(Decimal("500"), SAR)
            ),
            semantics=ConstraintSemantics(
                price_max=ConstraintStrength.APPROXIMATE,
                dimensions=(
                    DimensionConstraintSemantics(
                        role=DimensionRole.OVERALL_WIDTH,
                        strength=ConstraintStrength.LOCKED,
                    ),
                ),
            ),
        ),
    )

    assert result.relaxation_attempt_count > 0
    assert all(a.request.dimensions == (constraint,) for a in result.attempts)


async def test_a_locked_target_is_never_widened(
    database: Database, catalog: None
) -> None:
    """A locked TARGET executes as equality at every attempt, unchanged."""
    constraint = _c(DimensionRole.OVERALL_WIDTH, TARGET, target_cm="240")
    result = await _relaxed(
        database,
        ResolvedSearch(
            request=_request(
                constraint, seating_capacity=SeatingCapacityConstraint.exactly(3)
            ),
            semantics=ConstraintSemantics(
                seating_min=ConstraintStrength.PREFERRED,
                seating_max=ConstraintStrength.PREFERRED,
                dimensions=(
                    DimensionConstraintSemantics(
                        role=DimensionRole.OVERALL_WIDTH,
                        strength=ConstraintStrength.LOCKED,
                    ),
                ),
            ),
        ),
    )

    for attempt in result.attempts:
        assert len(attempt.request.dimensions) == 1
        carried = attempt.request.dimensions[0]
        assert carried.kind is TARGET
        assert carried.target_cm == Decimal("240")
        assert carried.max_cm is None


async def test_a_soft_target_executes_as_a_symmetric_band(
    database: Database, catalog: None
) -> None:
    """The executable request becomes a band; the provenance stays a TARGET."""
    constraint = _c(DimensionRole.OVERALL_WIDTH, TARGET, target_cm="240")
    result = await _relaxed(
        database,
        ResolvedSearch(
            request=_request(constraint),
            semantics=ConstraintSemantics(
                dimensions=(
                    DimensionConstraintSemantics(
                        role=DimensionRole.OVERALL_WIDTH,
                        strength=ConstraintStrength.APPROXIMATE,
                    ),
                ),
            ),
        ),
    )

    widened = [a for a in result.attempts if a.depth > 0]
    assert [a.request.dimensions[0].kind for a in widened] == [RANGE, RANGE]
    # 240 ±5% then ±10%, both centred on the original figure.
    assert [
        (a.request.dimensions[0].min_cm, a.request.dimensions[0].max_cm) for a in widened
    ] == [(Decimal("228"), Decimal("252")), (Decimal("216"), Decimal("264"))]
    assert all(a.request.dimensions[0].target_cm is None for a in widened)

    changes = [c for a in widened for c in a.changes]
    assert all(c.kind is TARGET for c in changes), "the customer's kind is preserved"
    assert all(c.original_target_cm == Decimal("240") for c in changes)


async def test_relaxation_never_adds_a_dimension(
    database: Database, catalog: None
) -> None:
    result = await _relaxed(
        database,
        ResolvedSearch(
            request=_request(price=PriceConstraint.at_most(Decimal("500"), SAR)),
            semantics=ConstraintSemantics(price_max=ConstraintStrength.APPROXIMATE),
        ),
    )

    assert all(a.request.dimensions == () for a in result.attempts)
    assert all(a.request.planar_dimensions is None for a in result.attempts)


# ── M8C-B: controlled dimension relaxation against real SQL ─────────────────


async def test_a_soft_width_widens_the_pool_in_two_stages(
    database: Database, catalog: None
) -> None:
    """The widened bands must actually execute, not merely be planned."""
    result = await _relaxed(
        database,
        ResolvedSearch(
            request=_request(_c(DimensionRole.OVERALL_WIDTH, MAX, max_cm="150")),
            semantics=ConstraintSemantics(
                dimensions=(
                    DimensionConstraintSemantics(
                        role=DimensionRole.OVERALL_WIDTH,
                        strength=ConstraintStrength.APPROXIMATE,
                    ),
                ),
            ),
        ),
    )

    widened = [a for a in result.attempts if a.depth > 0]
    assert [a.request.dimensions[0].max_cm for a in widened] == [
        Decimal("157.5"),
        Decimal("165"),
    ]
    assert all(a.request.dimensions[0].kind is MAX for a in widened)


async def test_a_non_relaxable_role_never_widens_against_real_sql(
    database: Database, catalog: None
) -> None:
    depth = _c(DimensionRole.DEPTH, MAX, max_cm="95")
    result = await _relaxed(
        database,
        ResolvedSearch(
            request=_request(depth),
            semantics=ConstraintSemantics(
                dimensions=(
                    DimensionConstraintSemantics(
                        role=DimensionRole.DEPTH,
                        strength=ConstraintStrength.APPROXIMATE,
                    ),
                ),
            ),
        ),
    )

    assert result.relaxation_attempt_count == 0
    assert all(a.request.dimensions == (depth,) for a in result.attempts)


async def test_relaxation_never_admits_a_product_with_no_dimension(
    database: Database, catalog: None
) -> None:
    """A NULL axis stays unknown in SQL however wide the band gets."""
    result = await _relaxed(
        database,
        ResolvedSearch(
            request=_request(_c(DimensionRole.OVERALL_WIDTH, MAX, max_cm="150")),
            semantics=ConstraintSemantics(
                dimensions=(
                    DimensionConstraintSemantics(
                        role=DimensionRole.OVERALL_WIDTH,
                        strength=ConstraintStrength.APPROXIMATE,
                    ),
                ),
            ),
        ),
    )

    # 21 has no length, 22 no unit, 23 an unrecognised one.
    found = {c.product.product_id for c in result.candidates}
    assert not {21, 22, 23} & found
