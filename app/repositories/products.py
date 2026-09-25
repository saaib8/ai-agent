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

from collections.abc import Sequence
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Numeric,
    Select,
    Text,
    case,
    func,
    select,
    type_coerce,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import core_product
from app.schemas.dimensions import CENTIMETRES_PER_UNIT, UNIT_ALIASES, RawDimensions
from app.schemas.discovery import (
    AxisConstraint,
    DimensionConstraintKind,
    PlanarDimensionConstraint,
    ProductSearchRequest,
    ProductSort,
)
from app.schemas.product import (
    STYLE_IGNORED_CHARACTER,
    STYLE_VALUE_SEPARATOR,
    CommerceClassification,
    EligibleProduct,
    ProductRow,
    parse_style_tokens,
)
from app.schemas.retailer import RetailerContext
from app.taxonomy.dimensions import SourceAxis

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
)


_POOL_COLUMNS = (
    core_product.c.id,
    core_product.c.price_amount,
    core_product.c.main_color,
    core_product.c.styles,
)
"""What ranking needs, and nothing else. A narrow projection is what makes
reading the COMPLETE eligible pool affordable (CLAUDE.md 16.1)."""


# Sort key -> ORDER BY. An enum, not a caller-supplied column name, so no
# request can order by an arbitrary database column. Product id is the stable
# tiebreaker, so repeated identical searches return an identical order.
_ORDER_BY: dict[ProductSort, tuple[ColumnElement[Any], ...]] = {
    ProductSort.DEFAULT: (core_product.c.id.asc(),),
    ProductSort.PRICE_ASC: (core_product.c.price_amount.asc(), core_product.c.id.asc()),
    ProductSort.PRICE_DESC: (core_product.c.price_amount.desc(), core_product.c.id.asc()),
}


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
        {
            alias: column * CENTIMETRES_PER_UNIT[unit]
            for alias, unit in UNIT_ALIASES.items()
        },
        value=normalised_unit,
        else_=None,
    ).cast(Numeric(12, 4))


def _axis_clauses(constraint: AxisConstraint) -> list[ColumnElement[bool]]:
    """Deterministic numeric comparisons for one resolved constraint."""
    value = _centimetres(constraint.axis)
    if constraint.kind is DimensionConstraintKind.MIN:
        return [value >= constraint.min_cm]
    if constraint.kind is DimensionConstraintKind.MAX:
        return [value <= constraint.max_cm]
    if constraint.kind is DimensionConstraintKind.RANGE:
        return [value >= constraint.min_cm, value <= constraint.max_cm]
    # TARGET seeds the exact attempt with equality. Widening it to a tolerance
    # is the dimension-relaxation milestone's job, not this one.
    return [value == constraint.target_cm]


def _planar_clause(
    constraint: PlanarDimensionConstraint, axes: tuple[SourceAxis, SourceAxis]
) -> ColumnElement[bool]:
    """Match a pair of sides in either order.

    A rug described as 200 x 300 is the same rug as 300 x 200, so both stored
    orientations satisfy the request.
    """
    first, second = (_centimetres(axis) for axis in axes)
    low, high = constraint.sides
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
    )


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

    def _eligibility_clauses(
        self,
        request: ProductSearchRequest,
        context: RetailerContext,
        axis_constraints: Sequence[AxisConstraint],
        planar: tuple[PlanarDimensionConstraint, tuple[SourceAxis, SourceAxis]] | None,
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
        if request.commerce_subcategory is not None:
            clauses.append(
                core_product.c.commerce_subcategory == request.commerce_subcategory
            )

        price = request.price
        if price is not None:
            # Exact currency match: no conversion, and no assumption that a
            # bare amount is comparable across currencies.
            clauses.append(core_product.c.price_unit == price.currency)
            amount = core_product.c.price_amount
            if price.min_amount is not None:
                clauses.append(
                    amount > price.min_amount
                    if price.min_exclusive
                    else amount >= price.min_amount
                )
            if price.max_amount is not None:
                clauses.append(
                    amount < price.max_amount
                    if price.max_exclusive
                    else amount <= price.max_amount
                )

        capacity = request.seating_capacity
        if capacity is not None:
            if capacity.min_capacity is not None:
                clauses.append(
                    core_product.c.seating_capacity >= capacity.min_capacity
                )
            if capacity.max_capacity is not None:
                clauses.append(
                    core_product.c.seating_capacity <= capacity.max_capacity
                )

        for axis_constraint in axis_constraints:
            # Part of eligibility, so it is decided before ORDER BY and LIMIT.
            # Filtering afterwards in Python would silently drop products the
            # customer could have had.
            clauses.extend(_axis_clauses(axis_constraint))
        if planar is not None:
            clauses.append(_planar_clause(planar[0], planar[1]))

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
        planar: tuple[PlanarDimensionConstraint, tuple[SourceAxis, SourceAxis]] | None,
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
        planar: tuple[PlanarDimensionConstraint, tuple[SourceAxis, SourceAxis]] | None = None,
    ) -> list[ProductRow]:
        """Products matching an already-validated structured request.

        A presentation query: eligibility comes from
        :meth:`_eligibility_clauses`, and this method adds only the ordering
        and the bound a caller asked to display.
        """
        statement = self._eligible_select(
            _SELECTED_COLUMNS, request, context, axis_constraints, planar
        )
        statement = statement.order_by(*_ORDER_BY[request.sort]).limit(limit)
        result = await self._session.execute(statement)
        return [_to_product_row(row) for row in result]

    async def search_eligible_ids(
        self,
        request: ProductSearchRequest,
        context: RetailerContext,
        *,
        axis_constraints: Sequence[AxisConstraint] = (),
        planar: tuple[PlanarDimensionConstraint, tuple[SourceAxis, SourceAxis]] | None = None,
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
        planar: tuple[PlanarDimensionConstraint, tuple[SourceAxis, SourceAxis]] | None = None,
    ) -> list[EligibleProduct]:
        """Every eligible product with the one fact ranking orders on.

        Additive to :meth:`search_eligible_ids`, which keeps its contract: this
        exists because an explicit price sort has to see the whole pool's
        prices, and fetching them separately would mean a second query over the
        same rows. Same eligibility, same absence of a `limit` parameter.

        Ordered by the request's own sort, from the same `_ORDER_BY` table
        :meth:`search` uses. That matters beyond tidiness: when semantic
        ranking is switched off or degrades, the order this returns is the
        order the customer sees, so "cheapest first" has to already be true
        here (CLAUDE.md 16.1).
        """
        statement = self._eligible_select(
            _POOL_COLUMNS, request, context, axis_constraints, planar
        ).order_by(*_ORDER_BY[request.sort])
        result = await self._session.execute(statement)
        return [
            EligibleProduct(
                product_id=int(row.id),
                price_amount=row.price_amount,
                main_color=row.main_color,
                styles=parse_style_tokens(row.styles),
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

    async def count_active(self, context: RetailerContext) -> int:
        """How many active products the store has. Backs health and capability checks."""
        statement = (
            select(func.count())
            .select_from(core_product)
            .where(*self._scope_clauses(context))
        )
        result = await self._session.execute(statement)
        return int(result.scalar_one())
