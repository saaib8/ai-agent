"""The only place that issues SQL against ``core_product``.

Every statement is built from SQLAlchemy Core expressions — never string
concatenation, and never anything derived from model output (CLAUDE.md 15).

Scope is structural, not conventional: :meth:`ProductRepository._scope_clauses`
supplies the ``store_id`` and ``is_active`` predicates and every public method
must build on it. No method accepts a store id; scope arrives only as an
immutable :class:`RetailerContext` resolved by application code
(CLAUDE.md 8, 20.2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Integer,
    Numeric,
    Select,
    Text,
    case,
    func,
    literal,
    or_,
    select,
    type_coerce,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DEFAULT_RATING_PRIOR_REVIEWS, SizeSettings
from app.db.tables import core_product
from app.schemas.catalog import CatalogFilter
from app.schemas.dimensions import CENTIMETRES_PER_UNIT, UNIT_ALIASES, RawDimensions
from app.schemas.discovery import (
    AxisConstraint,
    DimensionConstraintKind,
    PairMatch,
    ProductSearchRequest,
    ProductSort,
)
from app.schemas.product import (
    STYLE_IGNORED_CHARACTER,
    STYLE_VALUE_SEPARATOR,
    BestSellerRule,
    CommerceClassification,
    EligibleProduct,
    ProductListing,
    ProductRow,
    parse_style_tokens,
)
from app.schemas.retailer import RetailerContext
from app.taxonomy.dimensions import FaceSide, FloorSide, Measure, SourceAxis
from app.taxonomy.name_match import (
    ARABIC_FOLD_FROM,
    ARABIC_FOLD_TO,
    ENGLISH_FOLD_FROM,
    ENGLISH_FOLD_TO,
    NameMatch,
)

_SELECTED_COLUMNS = (
    core_product.c.id,
    core_product.c.uuid,
    core_product.c.store_id,
    core_product.c.name_english,
    core_product.c.name_arabic,
    core_product.c.price_amount,
    core_product.c.price_unit,
    core_product.c.image_url,
    core_product.c.product_url,
    core_product.c.category,
    core_product.c.commerce_category,
    core_product.c.commerce_subcategory,
    core_product.c.seating_capacity,
    core_product.c.length,
    core_product.c.width,
    core_product.c.height,
    core_product.c.dimension_unit,
    core_product.c.main_color,
    core_product.c.styles,
    core_product.c.is_active,
    core_product.c.material,
    core_product.c.regular_price,
    core_product.c.rating,
    core_product.c.review_count,
    core_product.c.weight_kg,
    core_product.c.sku,
    core_product.c.warranty_months,
    core_product.c.stock_quantity,
)


_POOL_COLUMNS = (
    core_product.c.id,
    core_product.c.commerce_subcategory,
    core_product.c.price_amount,
    core_product.c.main_color,
    core_product.c.styles,
    core_product.c.seating_capacity,
)
"""What ranking and seating combinations need, and nothing else. A narrow projection is what makes
reading the COMPLETE eligible pool affordable (CLAUDE.md 16.1)."""


def _store_average_rating(context: RetailerContext) -> ColumnElement[Any]:
    """C in the weighted rating: the average rating of the store's own rated
    products, worked out in the same query so it is never stale."""
    return (
        select(func.avg(core_product.c.rating))
        .where(
            core_product.c.store_id == context.store_id,
            core_product.c.is_active.is_(True),
            core_product.c.review_count > 0,
            core_product.c.rating.isnot(None),
        )
        .scalar_subquery()
    )


def _weighted_rating(context: RetailerContext, prior_reviews: int) -> ColumnElement[Any]:
    """The weighted rating that orders "best rated": (R*v + C*m) / (v + m).

    NULL for a product nobody has reviewed - it follows every rated one, never
    mixed in as if it were. Internal: it orders, and is never shown."""
    reviews = core_product.c.review_count
    return case(
        (
            (reviews > 0) & core_product.c.rating.isnot(None),
            (core_product.c.rating * reviews + _store_average_rating(context) * prior_reviews)
            / (reviews + prior_reviews),
        ),
        else_=None,
    )


_SOLD = case((core_product.c.sold_quantity > 0, core_product.c.sold_quantity), else_=None)
"""Sales, for ordering only: NULL where nothing sold, so unsold follow sold.
The number never leaves this module except as an order or a best-seller label."""


def _order_by(
    sort: ProductSort, context: RetailerContext, prior_reviews: int
) -> tuple[ColumnElement[Any], ...]:
    """Sort key -> ORDER BY. An enum, not a caller-supplied column name, so no
    request can order by an arbitrary database column. Product id is the stable
    tiebreaker, so repeated identical searches return an identical order."""
    match sort:
        case ProductSort.PRICE_ASC:
            return (core_product.c.price_amount.asc(), core_product.c.id.asc())
        case ProductSort.PRICE_DESC:
            return (core_product.c.price_amount.desc(), core_product.c.id.asc())
        case ProductSort.BEST_RATED:
            score = _weighted_rating(context, prior_reviews)
            return (score.desc().nulls_last(), core_product.c.id.asc())
        case ProductSort.BEST_SELLING:
            return (_SOLD.desc().nulls_last(), core_product.c.id.asc())
        case _:
            return (core_product.c.id.asc(),)


_AXIS_COLUMNS: dict[SourceAxis, ColumnElement[Any]] = {
    SourceAxis.LENGTH: core_product.c.length,
    SourceAxis.WIDTH: core_product.c.width,
    SourceAxis.HEIGHT: core_product.c.height,
}


def _centimetres(axis: SourceAxis) -> ColumnElement[Any]:
    """A stored axis expressed in centimetres.

    The catalog records dimensions in whatever unit the merchant supplied, so
    comparing the raw number would put 2.2 metres against 220 centimetres. The
    conversion is built from the same alias and factor tables the runtime
    normalizer uses, so a new unit is added in one place and both paths follow.

    An unrecognised unit produces NULL, and NULL satisfies no comparison - an
    unusable unit therefore excludes the row rather than being assumed to be
    centimetres.
    """
    column = _AXIS_COLUMNS[axis]
    normalised_unit = func.lower(func.btrim(core_product.c.dimension_unit))
    return case(
        {alias: column * CENTIMETRES_PER_UNIT[unit] for alias, unit in UNIT_ALIASES.items()},
        value=normalised_unit,
        else_=None,
    ).cast(Numeric(12, 4))


def _floor_sides() -> tuple[ColumnElement[Any], ColumnElement[Any]]:
    """A product's longer and shorter floor side, in centimetres.

    Whichever stored column the merchant put each in: the longer is the longer
    whatever its name, so no convention is needed to read it. NULL unless both
    are present in a recognised unit.
    """
    first, second = _centimetres(SourceAxis.LENGTH), _centimetres(SourceAxis.WIDTH)
    both = first.isnot(None) & second.isnot(None)
    return (
        case((both, func.greatest(first, second)), else_=None),
        case((both, func.least(first, second)), else_=None),
    )


_LONG_SIDE, _SHORT_SIDE = _floor_sides()


def _face_sides() -> tuple[ColumnElement[Any], ColumnElement[Any]]:
    """A flat thing's longer and shorter side, in centimetres: the two largest
    of its three listed sizes, whichever columns hold them - the smallest is
    its thickness. NULL unless at least two are present in a recognised unit.

    GREATEST and LEAST skip NULLs, so with two sizes they are the two sides,
    and with three the middle one is the total less the largest and smallest.
    """
    sizes = [_centimetres(axis) for axis in SourceAxis]
    present = sum((case((size.isnot(None), 1), else_=0) for size in sizes), literal(0))
    largest, smallest = func.greatest(*sizes), func.least(*sizes)
    middle = sizes[0] + sizes[1] + sizes[2] - largest - smallest
    return (
        case((present >= 2, largest), else_=None),
        case((present == 3, middle), (present == 2, smallest), else_=None),
    )


_LONG_FACE, _SHORT_FACE = _face_sides()

_SIDES: dict[FloorSide | FaceSide, ColumnElement[Any]] = {
    FloorSide.LONGER: _LONG_SIDE,
    FloorSide.SHORTER: _SHORT_SIDE,
    FaceSide.LONGER: _LONG_FACE,
    FaceSide.SHORTER: _SHORT_FACE,
}


def _measured(axis: Measure) -> ColumnElement[Any]:
    """What one constraint compares, in centimetres: a stored column, the
    piece's longer or shorter floor side - the larger or smaller of its two
    floor columns, so a merchant's column habits make no difference - or a
    flat thing's side."""
    if isinstance(axis, SourceAxis):
        return _centimetres(axis)
    return _SIDES[axis]


def _axis_clauses(constraint: AxisConstraint) -> list[ColumnElement[bool]]:
    """Deterministic numeric comparisons for one resolved constraint."""
    value = _measured(constraint.axis)
    if constraint.kind is DimensionConstraintKind.MIN:
        return [value >= constraint.min_cm]
    if constraint.kind is DimensionConstraintKind.MAX:
        return [value <= constraint.max_cm]
    if constraint.kind is DimensionConstraintKind.RANGE:
        return [value >= constraint.min_cm, value <= constraint.max_cm]
    # TARGET seeds the exact attempt with equality. Widening it to a tolerance
    # is the dimension-relaxation milestone's job, not this one.
    return [value == constraint.target_cm]


def _planar_clause(match: PairMatch) -> ColumnElement[bool]:
    """Match a pair of sides in either order.

    A rug described as 200 x 300 is the same rug as 300 x 200, so both stored
    orientations satisfy the request. Read by side, the shorter and longer
    floor side are compared directly - the order is the piece's, not the
    merchant's - each within the match's tolerance.
    """
    low, high = match.constraint.sides
    first_axis, second_axis = match.axes
    if not isinstance(first_axis, SourceAxis):
        share = match.tolerance
        return _measured(first_axis).between(low * (1 - share), low * (1 + share)) & _measured(
            second_axis
        ).between(high * (1 - share), high * (1 + share))
    first, second = _centimetres(first_axis), _centimetres(second_axis)  # type: ignore[arg-type]
    return ((first == low) & (second == high)) | ((first == high) & (second == low))


def _style_tokens() -> ColumnElement[Any]:
    """`core_product.styles` as exact tokens.

    Stored as comma-separated canonical values with merchant-inconsistent
    spacing ("Modern, Minimalist"). Stripping spaces before splitting yields
    exact tokens, which is sound precisely because no approved style contains a
    space - the registry refuses to load one that does.

    Built from the same two constants as :func:`parse_style_tokens`, so the
    matcher and the projection split a stored string the same way.

    Matching tokens rather than substrings is what keeps `Modern` from matching
    `Modern_Classic` or `Rustic_Modern`.
    """
    return type_coerce(
        func.string_to_array(
            func.replace(core_product.c.styles, STYLE_IGNORED_CHARACTER, ""),
            STYLE_VALUE_SEPARATOR,
        ),
        ARRAY(Text),
    )


def _like_escaped(word: str) -> str:
    """A customer's word as a LIKE pattern that matches it literally.

    `%` and `_` are wildcards to LIKE; a search for "50%" must not mean
    "anything starting 50".
    """
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@dataclass(frozen=True, slots=True)
class FacetCounts:
    """Raw facet counts for a store, before the registries have a say."""

    colors: tuple[tuple[str, int], ...]
    styles: tuple[tuple[str, int], ...]
    prices: tuple[tuple[str, Decimal, Decimal, int], ...]
    """(currency, lowest, highest, product count), most products first."""


@dataclass(frozen=True, slots=True)
class BriefFacts:
    """What a card of questions may offer for some product types, as stored.

    Raw values, counted from the active store's live catalog. Which colours
    and styles are approved vocabulary is the registries' decision, made by
    the caller, exactly as for :class:`FacetCounts`.
    """

    kinds: tuple[tuple[str, int | None, int], ...]
    """(subcategory, seating capacity, product count) - NULL capacity kept as
    its own row, since an unverified count satisfies no seat choice."""

    currency: str | None
    """The price unit most of these products carry, or None with no prices."""

    price_quartiles: tuple[Decimal, Decimal, Decimal] | None
    """The lower quartile, median and upper quartile of prices in `currency`."""

    colors: tuple[tuple[str, int], ...]
    styles: tuple[tuple[str, int], ...]


def _to_product_row(row: Row[Any]) -> ProductRow:
    return ProductRow(
        id=row.id,
        uuid=row.uuid,
        store_id=row.store_id,
        name_english=row.name_english,
        name_arabic=row.name_arabic,
        price_amount=row.price_amount,
        price_unit=row.price_unit,
        image_url=row.image_url,
        product_url=row.product_url,
        visual_category=row.category,
        commerce=CommerceClassification(
            category=row.commerce_category,
            subcategory=row.commerce_subcategory,
            seating_capacity=row.seating_capacity,
        ),
        dimensions=RawDimensions(
            length=row.length,
            width=row.width,
            height=row.height,
            unit=row.dimension_unit,
        ),
        main_color=row.main_color,
        styles=parse_style_tokens(row.styles),
        is_active=row.is_active,
        listing=ProductListing(
            material=_text(row.material),
            regular_price=row.regular_price,
            **_rating(row.rating, row.review_count),
            weight_kg=row.weight_kg,
            sku=_text(row.sku),
            warranty_months=row.warranty_months,
            stock_quantity=_in_stock(row.stock_quantity),
        ),
    )


def _in_stock(quantity: int | None) -> int | None:
    """How many the store has, or None when it said nothing usable - a count of
    zero or less limits nothing, the store's own cart check refuses it."""
    return quantity if quantity is not None and quantity > 0 else None


def _rating(rating: Decimal | None, count: int | None) -> dict[str, Any]:
    """The rating with the reviews it rests on, or neither: the import stores
    an unreviewed product as a count of 0, which is no reviews, not a rating."""
    if rating is None or not count or count < 1:
        return {}
    return {"rating": rating, "review_count": count}


def _text(value: str | None) -> str | None:
    """A stored text field, or None where it is blank."""
    text = (value or "").strip()
    return text or None


@dataclass(frozen=True, slots=True)
class DetailsTextRow:
    """One product's long text, as stored - for :meth:`ProductRepository.details_text`."""

    description: str | None
    description_english: str | None
    reviews: Any
    """`reviews_json` as the driver returns it: a list, or text; read by
    `app/services/product_details.py`, which trusts neither."""


@dataclass(frozen=True, slots=True)
class CatalogOverviewRow:
    """One commerce type's row in the catalog-overview aggregation.

    The repository's raw output for :meth:`ProductRepository.catalog_overview`,
    kept apart from the agent-facing :class:`~app.schemas.catalog_overview.CatalogOverview`
    so taxonomy approval and the currency decision stay in the service. Ranges
    and counts, never a product.
    """

    commerce_category: str
    commerce_subcategory: str | None
    active_count: int
    seat_known: int
    seat_minimum: int | None
    seat_maximum: int | None
    price_minimum: Decimal
    price_maximum: Decimal
    colours: tuple[str, ...]
    price_units: tuple[str, ...]
    styles: tuple[str, ...] = ()
    planar_measured: int = 0
    planar_elongated: int = 0
    seat_counts: tuple[int, ...] = ()
    """Every confirmed seat count among the type's pieces."""


def _overview_row(row: Row[Any]) -> CatalogOverviewRow:
    """Map one aggregation row, deduplicating and sorting its arrays.

    ``array_agg`` returns NULL, not an empty array, when its filter removes
    every row, so an absent colour set reads as ``()`` rather than raising.
    """
    return CatalogOverviewRow(
        commerce_category=row.commerce_category,
        commerce_subcategory=row.commerce_subcategory,
        active_count=row.active_count,
        seat_known=row.seat_known,
        seat_minimum=row.seat_minimum,
        seat_maximum=row.seat_maximum,
        price_minimum=row.price_minimum,
        price_maximum=row.price_maximum,
        colours=tuple(sorted(row.colours)) if row.colours else (),
        price_units=tuple(sorted(row.price_units)) if row.price_units else (),
        styles=tuple(
            sorted({token for raw in (row.styles or ()) for token in parse_style_tokens(raw)})
        ),
        planar_measured=row.planar_measured,
        seat_counts=tuple(sorted(row.seat_counts)) if row.seat_counts else (),
        planar_elongated=row.planar_elongated,
    )


@dataclass(frozen=True, slots=True)
class StoreProductIdentity:
    """How the store itself knows one product, for :meth:`ProductRepository.store_identities`."""

    product_id: int
    uuid: str
    salla_product_id: str | None
    product_url: str
    name_english: str
    name_arabic: str | None


class ProductRepository:
    """Read-only, store-scoped access to the Django-owned product catalog."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── scope ───────────────────────────────────────────────────────────────

    @staticmethod
    def _scope_clauses(context: RetailerContext) -> tuple[ColumnElement[bool], ...]:
        """The predicates every query against this table must carry."""
        return (
            core_product.c.store_id == context.store_id,
            core_product.c.is_active.is_(True),
        )

    def _scoped(self, context: RetailerContext) -> Select[Any]:
        return select(*_SELECTED_COLUMNS).where(*self._scope_clauses(context))

    # ── queries ─────────────────────────────────────────────────────────────

    async def get_by_ids(
        self, product_ids: Sequence[int], context: RetailerContext
    ) -> list[ProductRow]:
        """Products the active store owns, for the given ids.

        Ids the store does not own are simply absent from the result — the
        caller learns nothing about another store's catalog.
        """
        unique_ids = sorted(set(product_ids))
        if not unique_ids:
            return []
        statement = (
            self._scoped(context)
            .where(core_product.c.id.in_(unique_ids))
            .order_by(core_product.c.id)
        )
        result = await self._session.execute(statement)
        return [_to_product_row(row) for row in result]

    async def best_sellers(
        self,
        context: RetailerContext,
        rule: BestSellerRule,
        *,
        product_ids: Sequence[int] = (),
        kinds: Sequence[str] = (),
    ) -> list[int]:
        """The best sellers of the given products' kinds, or of the given kinds,
        best first within each kind - ids only, never a sales number.

        Ranked within the store's own products of each kind, so a small store's
        favourite is its own best seller whatever a larger store sells.
        """
        if not product_ids and not kinds:
            return []
        kind = core_product.c.commerce_subcategory
        if product_ids:
            asked_kinds: Any = select(kind).where(
                *self._scope_clauses(context), core_product.c.id.in_(sorted(set(product_ids)))
            )
        else:
            asked_kinds = sorted(set(kinds))
        sellers = (
            select(
                core_product.c.id,
                kind.label("kind"),
                core_product.c.sold_quantity.label("sold"),
                func.row_number()
                .over(
                    partition_by=kind,
                    order_by=(core_product.c.sold_quantity.desc(), core_product.c.id.asc()),
                )
                .label("place"),
                func.count().over(partition_by=kind).label("sellers"),
            )
            .where(
                *self._scope_clauses(context),
                core_product.c.sold_quantity > 0,
                kind.in_(asked_kinds),
            )
            .subquery()
        )
        # The share as an exact decimal, so "the top 7% of 100" is 7, never the
        # 8 a binary float's 7.000000000000001 would round up to.
        share = literal(Decimal(str(rule.top_share)), Numeric(6, 4))
        top = func.least(rule.max_per_kind, func.ceil(sellers.c.sellers * share))
        statement = select(sellers.c.id).where(
            sellers.c.sold >= rule.min_sold, sellers.c.place <= top
        )
        if product_ids:
            statement = statement.where(sellers.c.id.in_(sorted(set(product_ids))))
        statement = statement.order_by(sellers.c.kind, sellers.c.place)
        result = await self._session.execute(statement)
        return [int(row.id) for row in result]

    async def details_text(
        self, product_id: int, context: RetailerContext
    ) -> DetailsTextRow | None:
        """One product's description, English description and stored reviews,
        as stored - None when the active store has no such product.

        Read only for one product's details: the long text never rides along
        with a list of products.
        """
        statement = select(
            core_product.c.description,
            core_product.c.description_english,
            core_product.c.reviews_json,
        ).where(*self._scope_clauses(context), core_product.c.id == product_id)
        row = (await self._session.execute(statement)).first()
        if row is None:
            return None
        return DetailsTextRow(
            description=row.description,
            description_english=row.description_english,
            reviews=row.reviews_json,
        )

    async def store_identities(
        self, product_ids: Sequence[int], context: RetailerContext
    ) -> list[StoreProductIdentity]:
        """How the store itself knows these products - its public id, its
        Salla id and its page - for copying a cart into the store's own.

        Kept apart from `get_by_ids` so nothing that ranks, grounds or prompts
        ever carries a store's own product id: only the cart reads these.
        """
        unique_ids = sorted(set(product_ids))
        if not unique_ids:
            return []
        statement = (
            select(
                core_product.c.id,
                core_product.c.uuid,
                core_product.c.salla_product_id,
                core_product.c.product_url,
                core_product.c.name_english,
                core_product.c.name_arabic,
            )
            .where(*self._scope_clauses(context), core_product.c.id.in_(unique_ids))
            .order_by(core_product.c.id)
        )
        result = await self._session.execute(statement)
        return [
            StoreProductIdentity(
                product_id=row.id,
                uuid=str(row.uuid),
                salla_product_id=(str(row.salla_product_id).strip() or None)
                if row.salla_product_id is not None
                else None,
                product_url=row.product_url,
                name_english=row.name_english,
                name_arabic=row.name_arabic,
            )
            for row in result
        ]

    async def ids_for_salla_products(
        self, salla_product_ids: Sequence[str], context: RetailerContext
    ) -> dict[str, int]:
        """Our product id for each Salla product id the active store lists.

        The reverse of `store_identities`, for reading the store's own cart
        back into ours. A Salla id the store does not list (or lists only on an
        inactive product) is absent from the answer. Where a store lists one
        Salla product twice, the oldest row answers, so the choice never moves.
        """
        wanted = sorted({salla_id for salla_id in salla_product_ids if salla_id})
        if not wanted:
            return {}
        statement = (
            select(core_product.c.id, core_product.c.salla_product_id)
            .where(*self._scope_clauses(context), core_product.c.salla_product_id.in_(wanted))
            .order_by(core_product.c.id)
        )
        result = await self._session.execute(statement)
        ours: dict[str, int] = {}
        for row in result:
            ours.setdefault(str(row.salla_product_id).strip(), int(row.id))
        return ours

    def _eligibility_clauses(
        self,
        request: ProductSearchRequest,
        context: RetailerContext,
        axis_constraints: Sequence[AxisConstraint],
        planar: PairMatch | None,
    ) -> list[ColumnElement[bool]]:
        """Every predicate that decides eligibility, and nothing that decides
        presentation.

        The single definition of "which products match this request". Both the
        full projection and the id-only pool build from it, so the two can
        never drift into answering slightly different questions - which is
        exactly the bug that would make semantic ranking rank the wrong set.

        Ordering and limiting are deliberately absent: they are presentation,
        and applying them here would make the eligible pool depend on how many
        rows a caller wanted to show.

        NULL is never a match: a row with no `commerce_category` cannot satisfy
        a category filter, and a row with no `seating_capacity` cannot satisfy
        a capacity constraint, because SQL comparisons against NULL are unknown
        rather than true. Missing facts are never substituted. The same holds
        for a missing dimension or an unusable unit.

        Taxonomy values arrive pre-validated; this method is not the vocabulary
        authority and holds no category list of its own (CLAUDE.md 14.1).
        """
        clauses: list[ColumnElement[bool]] = [
            *self._scope_clauses(context),
            core_product.c.commerce_category == request.commerce_category,
        ]
        if request.alongside_subcategories:
            # The type asked for and the types shown beside it, under every
            # other constraint alike (`ProductSearchRequest.subcategories`).
            clauses.append(core_product.c.commerce_subcategory.in_(request.subcategories))
        elif request.commerce_subcategory is not None:
            clauses.append(core_product.c.commerce_subcategory == request.commerce_subcategory)

        price = request.price
        if price is not None:
            # Exact currency match: no conversion, and no assumption that a
            # bare amount is comparable across currencies.
            clauses.append(core_product.c.price_unit == price.currency)
            amount = core_product.c.price_amount
            if price.min_amount is not None:
                clauses.append(
                    amount > price.min_amount if price.min_exclusive else amount >= price.min_amount
                )
            if price.max_amount is not None:
                clauses.append(
                    amount < price.max_amount if price.max_exclusive else amount <= price.max_amount
                )

        capacity = request.seating_capacity
        if capacity is not None:
            if capacity.min_capacity is not None:
                clauses.append(core_product.c.seating_capacity >= capacity.min_capacity)
            if capacity.max_capacity is not None:
                clauses.append(core_product.c.seating_capacity <= capacity.max_capacity)

        for axis_constraint in axis_constraints:
            # Part of eligibility, so it is decided before ORDER BY and LIMIT.
            # Filtering afterwards in Python would silently drop products the
            # customer could have had.
            clauses.extend(_axis_clauses(axis_constraint))
        if planar is not None:
            clauses.append(_planar_clause(planar))

        if request.colors_any_of:
            # Exact equality against the authoritative controlled colour
            # column. NULL never matches, so an unclassified colour is never
            # passed off as satisfying the requirement.
            clauses.append(core_product.c.main_color.in_(request.colors_any_of))
        if request.styles_all_of:
            # `@>` on the parsed tokens: every required style must be present.
            # A NULL or empty styles column yields no tokens and so matches
            # nothing.
            clauses.append(_style_tokens().contains(list(request.styles_all_of)))

        if request.exclude_product_ids:
            # Part of eligibility, so an excluded product is gone before
            # ordering, limiting and ranking - never removed from a bounded
            # page afterwards, which would silently shorten it.
            clauses.append(core_product.c.id.notin_(request.exclude_product_ids))
        return clauses

    def _eligible_select(
        self,
        columns: Sequence[ColumnElement[Any]],
        request: ProductSearchRequest,
        context: RetailerContext,
        axis_constraints: Sequence[AxisConstraint],
        planar: PairMatch | None,
    ) -> Select[Any]:
        """The eligible rows, projected to `columns`, with no ordering or bound.

        Every query that decides which products match a request is built here,
        so a projection can never answer a slightly different question from the
        one beside it.
        """
        return select(*columns).where(
            *self._eligibility_clauses(request, context, axis_constraints, planar)
        )

    async def search(
        self,
        request: ProductSearchRequest,
        context: RetailerContext,
        *,
        limit: int,
        axis_constraints: Sequence[AxisConstraint] = (),
        planar: PairMatch | None = None,
        prior_reviews: int = DEFAULT_RATING_PRIOR_REVIEWS,
    ) -> list[ProductRow]:
        """Products matching an already-validated structured request.

        A presentation query: eligibility comes from
        :meth:`_eligibility_clauses`, and this method adds only the ordering
        and the bound a caller asked to display.
        """
        statement = self._eligible_select(
            _SELECTED_COLUMNS, request, context, axis_constraints, planar
        )
        statement = statement.order_by(*_order_by(request.sort, context, prior_reviews)).limit(
            limit
        )
        result = await self._session.execute(statement)
        return [_to_product_row(row) for row in result]

    async def search_eligible_ids(
        self,
        request: ProductSearchRequest,
        context: RetailerContext,
        *,
        axis_constraints: Sequence[AxisConstraint] = (),
        planar: PairMatch | None = None,
    ) -> list[int]:
        """Every eligible product id, with no presentation limit.

        Semantic ranking must see the whole eligible set: reranking the first
        fifty of a hundred and seventy-three would leave the best match
        unreachable. There is deliberately no `limit` parameter, so no caller
        can quietly reintroduce one.

        Identical eligibility to :meth:`search` by construction - both call
        :meth:`_eligibility_clauses` - so the ranked set can never be a
        different set from the searchable one.
        """
        statement = self._eligible_select(
            (core_product.c.id,), request, context, axis_constraints, planar
        ).order_by(core_product.c.id)
        result = await self._session.execute(statement)
        return [int(row.id) for row in result]

    async def search_eligible_pool(
        self,
        request: ProductSearchRequest,
        context: RetailerContext,
        *,
        axis_constraints: Sequence[AxisConstraint] = (),
        planar: PairMatch | None = None,
        prior_reviews: int = DEFAULT_RATING_PRIOR_REVIEWS,
    ) -> list[EligibleProduct]:
        """Every eligible product with the one fact ranking orders on.

        Additive to :meth:`search_eligible_ids`, which keeps its contract: this
        exists because an explicit price sort has to see the whole pool's
        prices, and fetching them separately would mean a second query over the
        same rows. Same eligibility, same absence of a `limit` parameter.

        Ordered by the request's own sort, from the same `_order_by`
        :meth:`search` uses. That matters beyond tidiness: when semantic
        ranking is switched off or degrades, the order this returns is the
        order the customer sees, so "cheapest first" has to already be true
        here (CLAUDE.md 16.1).
        """
        statement = self._eligible_select(
            (
                *_POOL_COLUMNS,
                _LONG_SIDE.label("long_side_cm"),
                _SHORT_SIDE.label("short_side_cm"),
                _weighted_rating(context, prior_reviews).label("weighted_rating"),
                _SOLD.label("sold"),
            ),
            request,
            context,
            axis_constraints,
            planar,
        ).order_by(*_order_by(request.sort, context, prior_reviews))
        result = await self._session.execute(statement)
        return [
            EligibleProduct(
                product_id=int(row.id),
                subcategory=row.commerce_subcategory,
                price_amount=row.price_amount,
                main_color=row.main_color,
                styles=parse_style_tokens(row.styles),
                seating_capacity=row.seating_capacity,
                long_side_cm=row.long_side_cm,
                short_side_cm=row.short_side_cm,
                weighted_rating=row.weighted_rating,
                sold=row.sold,
            )
            for row in result
        ]

    async def supported_commerce_types(
        self, context: RetailerContext
    ) -> tuple[tuple[str, str | None, int], ...]:
        """Commerce classifications this retailer stocks, and how many of each.

        Scoped and active-only, like every other read here: capability is a
        statement about *this* store's live catalog, never about the global
        vocabulary and never about a dataset someone happened to load
        (CLAUDE.md 9.1).

        The count comes from the same grouped scan that establishes the type
        exists, so it costs nothing extra and cannot disagree with it. It is
        what separates a type the retailer genuinely offers from one it has a
        single example of - a distinction a planner needs and a bare list
        cannot express (CLAUDE.md 9).

        Unclassified rows are excluded rather than reported as a gap. A product
        whose commerce fields were never reviewed says nothing about what the
        retailer sells, and reporting it as a capability would be guessing.

        Deterministically ordered, so a capability summary is stable between
        calls and diffable in a log.
        """
        statement = (
            select(
                core_product.c.commerce_category,
                core_product.c.commerce_subcategory,
                func.count().label("active_count"),
            )
            .where(
                *self._scope_clauses(context),
                core_product.c.commerce_category.isnot(None),
            )
            .group_by(
                core_product.c.commerce_category,
                core_product.c.commerce_subcategory,
            )
            .order_by(
                core_product.c.commerce_category,
                core_product.c.commerce_subcategory,
            )
        )
        result = await self._session.execute(statement)
        return tuple((row[0], row[1], row[2]) for row in result.all())

    async def catalog_overview(
        self, context: RetailerContext, *, size: SizeSettings
    ) -> tuple[CatalogOverviewRow, ...]:
        """The shape of this store's shelf, one row per commerce type.

        The same grouped, scoped, active-only scan as
        :meth:`supported_commerce_types`, widened with the aggregates a
        salesperson reasons over before deciding a move: how high one piece's
        seat count reaches, the price band, and the colours a type comes in.
        Counts and ranges only - no product, no id, no individual price
        (CLAUDE.md 9).

        Seat aggregates ignore NULLs, so ``seat_known`` is how many pieces carry
        a confirmed count and ``seat_maximum`` is the true single-piece ceiling;
        an unverified capacity never inflates it (CLAUDE.md 6.2). Colours drop
        NULLs and duplicates. The currency question is left to the service, which
        sees every type's units together.

        Styles come back as the stored strings, split into tokens on the way
        out. For sizes, how many pieces have both floor sides within the
        plausible range, and how many of those are clearly long and shallow -
        one side at least `size.elongation_ratio` times the other - which is
        the evidence the shape rule reads (``SubcategoryShelf.long_and_shallow``).
        """
        plausible = (size.min_side_cm <= _SHORT_SIDE) & (size.max_side_cm >= _LONG_SIDE)
        colours = func.array_agg(core_product.c.main_color.distinct()).filter(
            core_product.c.main_color.isnot(None)
        )
        styles = func.array_agg(core_product.c.styles.distinct()).filter(
            core_product.c.styles.isnot(None)
        )
        statement = (
            select(
                core_product.c.commerce_category,
                core_product.c.commerce_subcategory,
                func.count().label("active_count"),
                func.count(core_product.c.seating_capacity).label("seat_known"),
                func.min(core_product.c.seating_capacity).label("seat_minimum"),
                func.max(core_product.c.seating_capacity).label("seat_maximum"),
                type_coerce(
                    func.array_agg(core_product.c.seating_capacity.distinct()).filter(
                        core_product.c.seating_capacity.isnot(None)
                    ),
                    ARRAY(Integer),
                ).label("seat_counts"),
                func.min(core_product.c.price_amount).label("price_minimum"),
                func.max(core_product.c.price_amount).label("price_maximum"),
                type_coerce(colours, ARRAY(Text)).label("colours"),
                type_coerce(
                    func.array_agg(core_product.c.price_unit.distinct()), ARRAY(Text)
                ).label("price_units"),
                type_coerce(styles, ARRAY(Text)).label("styles"),
                func.count(_LONG_SIDE).filter(plausible).label("planar_measured"),
                func.count(_LONG_SIDE)
                .filter(plausible & (_SHORT_SIDE * size.elongation_ratio <= _LONG_SIDE))
                .label("planar_elongated"),
            )
            .where(
                *self._scope_clauses(context),
                core_product.c.commerce_category.isnot(None),
            )
            .group_by(
                core_product.c.commerce_category,
                core_product.c.commerce_subcategory,
            )
            .order_by(
                core_product.c.commerce_category,
                core_product.c.commerce_subcategory,
            )
        )
        result = await self._session.execute(statement)
        return tuple(_overview_row(row) for row in result.all())

    # ── Furniture Finder ────────────────────────────────────────────────────

    async def visual_categories(self, context: RetailerContext) -> frozenset[str]:
        """The visual categories this store sells something in.

        What a detected object has to be for the finder to offer it: an
        outline the customer can click that the catalog has nothing in the
        category of can only ever come back empty.
        """
        statement = (
            select(func.lower(core_product.c.category))
            .where(*self._scope_clauses(context), core_product.c.category.is_not(None))
            .distinct()
        )
        result = await self._session.execute(statement)
        return frozenset(value for (value,) in result if value)

    async def ids_for_visual_matches(
        self,
        pinecone_ids: Sequence[str],
        product_urls: Sequence[str],
        context: RetailerContext,
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Product ids this store sells, keyed by index vector id and by page.

        Two keys because a product index may have been built from a different
        copy of the catalog: its vector ids then match nothing here, while the
        product page it points at still does. Only products in scope
        are returned, so a vector for another retailer's product resolves to
        nothing whichever key is tried.

        Where two rows share a key the lowest id wins, so the answer does not
        depend on row order.
        """
        ids = sorted({i for i in pinecone_ids if i})
        urls = sorted({u for u in product_urls if u})
        if not ids and not urls:
            return {}, {}
        statement = (
            select(core_product.c.id, core_product.c.pinecone_id, core_product.c.product_url)
            .where(
                *self._scope_clauses(context),
                (core_product.c.pinecone_id.in_(ids) | core_product.c.product_url.in_(urls)),
            )
            .order_by(core_product.c.id)
        )
        result = await self._session.execute(statement)
        by_vector: dict[str, int] = {}
        by_url: dict[str, int] = {}
        for row in result:
            if row.pinecone_id:
                by_vector.setdefault(row.pinecone_id, row.id)
            if row.product_url:
                by_url.setdefault(row.product_url, row.id)
        return by_vector, by_url

    async def click_ids_by_url(
        self, product_urls: Sequence[str], context: RetailerContext
    ) -> dict[str, str]:
        """Each in-scope product page's public id (`core_product.uuid`), keyed
        by the page - what a card's link carries as `zory_click_id`.

        Read only once a response is finished, so the id never travels through
        ranking, grounding or a prompt (as with :meth:`store_identities`).
        Where two rows share a page the lowest id wins.
        """
        urls = sorted({u for u in product_urls if u})
        if not urls:
            return {}
        statement = (
            select(core_product.c.uuid, core_product.c.product_url)
            .where(*self._scope_clauses(context), core_product.c.product_url.in_(urls))
            .order_by(core_product.c.id)
        )
        result = await self._session.execute(statement)
        click_ids: dict[str, str] = {}
        for row in result:
            click_ids.setdefault(row.product_url, str(row.uuid))
        return click_ids

    # ── Browse Catalogue ────────────────────────────────────────────────────

    def _browse_clauses(
        self, filters: CatalogFilter, context: RetailerContext
    ) -> list[ColumnElement[bool]]:
        """The one definition of which products a browse page may show, shared
        by the page and its count so the two can never disagree."""
        clauses: list[ColumnElement[bool]] = [*self._scope_clauses(context)]
        for word in filters.name_words:
            pattern = _like_escaped(word)
            clauses.append(
                core_product.c.name_english.ilike(pattern, escape="\\")
                | core_product.c.name_arabic.ilike(pattern, escape="\\")
            )
        if filters.category is not None:
            clauses.append(core_product.c.commerce_category == filters.category)
        if filters.subcategory is not None:
            clauses.append(core_product.c.commerce_subcategory == filters.subcategory)
        if filters.color is not None:
            clauses.append(core_product.c.main_color == filters.color)
        if filters.style is not None:
            clauses.append(_style_tokens().contains([filters.style]))
        if filters.currency is not None:
            clauses.append(core_product.c.price_unit == filters.currency)
            if filters.min_price is not None:
                clauses.append(core_product.c.price_amount >= filters.min_price)
            if filters.max_price is not None:
                clauses.append(core_product.c.price_amount <= filters.max_price)
        return clauses

    async def browse(
        self,
        filters: CatalogFilter,
        context: RetailerContext,
        *,
        offset: int,
        limit: int,
        prior_reviews: int = DEFAULT_RATING_PRIOR_REVIEWS,
    ) -> list[ProductRow]:
        """One page of the store's catalog, in the requested order."""
        order = list(_order_by(filters.sort, context, prior_reviews))
        if filters.lead_categories and filters.sort is ProductSort.DEFAULT:
            leads = case(
                (core_product.c.commerce_category.in_(filters.lead_categories), 0), else_=1
            )
            order.insert(0, leads)
        statement = (
            select(*_SELECTED_COLUMNS)
            .where(*self._browse_clauses(filters, context))
            .order_by(*order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(statement)
        return [_to_product_row(row) for row in result]

    async def count_browse(self, filters: CatalogFilter, context: RetailerContext) -> int:
        """How many products a browse matches, across every page."""
        statement = (
            select(func.count())
            .select_from(core_product)
            .where(*self._browse_clauses(filters, context))
        )
        result = await self._session.execute(statement)
        return int(result.scalar_one())

    async def facet_counts(self, context: RetailerContext) -> FacetCounts:
        """Colours, style tokens and price ranges the store's catalog holds.

        Raw values, counted as stored. Which of them are approved vocabulary
        is the registries' decision, made by the caller.
        """
        scope = self._scope_clauses(context)
        colors = await self._session.execute(
            select(core_product.c.main_color, func.count())
            .where(*scope, core_product.c.main_color.is_not(None))
            .group_by(core_product.c.main_color)
            .order_by(func.count().desc(), core_product.c.main_color)
        )
        tokens = select(func.unnest(_style_tokens()).label("token")).where(*scope).subquery()
        styles = await self._session.execute(
            select(tokens.c.token, func.count())
            .group_by(tokens.c.token)
            .order_by(func.count().desc(), tokens.c.token)
        )
        prices = await self._session.execute(
            select(
                core_product.c.price_unit,
                func.min(core_product.c.price_amount),
                func.max(core_product.c.price_amount),
                func.count(),
            )
            .where(*scope)
            .group_by(core_product.c.price_unit)
            .order_by(func.count().desc(), core_product.c.price_unit)
        )
        return FacetCounts(
            colors=tuple((row[0], int(row[1])) for row in colors.all()),
            styles=tuple((row[0], int(row[1])) for row in styles.all() if row[0]),
            prices=tuple((row[0], row[1], row[2], int(row[3])) for row in prices.all() if row[0]),
        )

    async def brief_facts(
        self, subcategories: Sequence[str], context: RetailerContext
    ) -> BriefFacts:
        """Kinds, prices, colours and styles among some product types.

        One read per fact, all under the store scope. Prices are summarised in
        a single currency - the one most of these products carry - because a
        quartile across two currencies is not a price anyone could pay.
        """
        types = tuple(dict.fromkeys(subcategories))
        scope = (*self._scope_clauses(context), core_product.c.commerce_subcategory.in_(types))
        kinds = await self._session.execute(
            select(
                core_product.c.commerce_subcategory,
                core_product.c.seating_capacity,
                func.count(),
            )
            .where(*scope)
            .group_by(core_product.c.commerce_subcategory, core_product.c.seating_capacity)
        )
        units = await self._session.execute(
            select(core_product.c.price_unit, func.count())
            .where(*scope, core_product.c.price_unit.is_not(None))
            .group_by(core_product.c.price_unit)
            .order_by(func.count().desc(), core_product.c.price_unit)
            .limit(1)
        )
        unit = units.first()
        currency = unit[0] if unit is not None and unit[0] else None
        quartiles: tuple[Decimal, Decimal, Decimal] | None = None
        if currency is not None:
            price = core_product.c.price_amount
            spread = await self._session.execute(
                select(
                    func.percentile_cont(0.25).within_group(price.asc()),
                    func.percentile_cont(0.5).within_group(price.asc()),
                    func.percentile_cont(0.75).within_group(price.asc()),
                ).where(*scope, core_product.c.price_unit == currency, price.is_not(None))
            )
            row = spread.first()
            if row is not None and all(value is not None for value in row):
                quartiles = (Decimal(str(row[0])), Decimal(str(row[1])), Decimal(str(row[2])))
        colors = await self._session.execute(
            select(core_product.c.main_color, func.count())
            .where(*scope, core_product.c.main_color.is_not(None))
            .group_by(core_product.c.main_color)
            .order_by(func.count().desc(), core_product.c.main_color)
        )
        tokens = select(func.unnest(_style_tokens()).label("token")).where(*scope).subquery()
        styles = await self._session.execute(
            select(tokens.c.token, func.count())
            .group_by(tokens.c.token)
            .order_by(func.count().desc(), tokens.c.token)
        )
        return BriefFacts(
            kinds=tuple((row[0], row[1], int(row[2])) for row in kinds.all() if row[0] is not None),
            currency=currency,
            price_quartiles=quartiles,
            colors=tuple((row[0], int(row[1])) for row in colors.all()),
            styles=tuple((row[0], int(row[1])) for row in styles.all() if row[0]),
        )

    async def name_match_counts(
        self,
        subcategories: Sequence[str],
        matches: Mapping[str, NameMatch],
        context: RetailerContext,
    ) -> dict[str, int]:
        """How many of these types' products are named with each chip's words.

        One read under the store scope, a count per chip. Names are folded
        exactly as `app/taxonomy/name_match.py` folds them, and each pattern
        is a bound parameter built from reviewed terms, never from a
        customer's or a model's words.
        """
        if not matches:
            return {}
        types = tuple(dict.fromkeys(subcategories))
        english = func.translate(
            func.lower(core_product.c.name_english), ENGLISH_FOLD_FROM, ENGLISH_FOLD_TO
        )
        arabic = func.translate(core_product.c.name_arabic, ARABIC_FOLD_FROM, ARABIC_FOLD_TO)
        keys = list(matches)
        counts = []
        for index, key in enumerate(keys):
            match = matches[key]
            found = [
                *((english.regexp_match(match.english),) if match.english else ()),
                *((arabic.regexp_match(match.arabic),) if match.arabic else ()),
            ]
            counts.append(func.count().filter(or_(*found)).label(f"m{index}"))
        result = await self._session.execute(
            select(*counts).where(
                *self._scope_clauses(context), core_product.c.commerce_subcategory.in_(types)
            )
        )
        row = result.first()
        return {key: int(row[index]) if row is not None else 0 for index, key in enumerate(keys)}

    async def count_active(self, context: RetailerContext) -> int:
        """How many active products the store has. Backs health and capability checks."""
        statement = (
            select(func.count()).select_from(core_product).where(*self._scope_clauses(context))
        )
        result = await self._session.execute(statement)
        return int(result.scalar_one())
