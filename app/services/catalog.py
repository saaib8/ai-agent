"""Browse Catalogue: the store's products, filtered and paged for a picker.

A browse is not a search in the discovery sense. Nothing interprets language
and nothing ranks: the customer names a category, a colour, a price and some
words in a name, and gets the products that carry them, in a stated order. So
it goes straight to the repository, under the same store scope as everything
else, and every vocabulary value is checked against its registry first - an
unapproved category never reaches SQL (CLAUDE.md 14.3).

Facets are counted from the live catalog of the active store and then passed
through the registries, so a colour the data happens to hold but the
vocabulary does not approve is never offered as a filter.
"""

from __future__ import annotations

import math

from app.core.config import CatalogSettings
from app.core.exceptions import CatalogFilterRejectedError
from app.prompts.visualization.v1 import ROOM_TYPES
from app.repositories.products import ProductRepository
from app.schemas.catalog import (
    CatalogFacets,
    CatalogFilter,
    CatalogItem,
    CatalogPage,
    CatalogQuery,
    CatalogSort,
    CategoryFacet,
    Facet,
    PriceRange,
    RoomTypeOption,
    StudioOptions,
)
from app.schemas.dimensions import NormalisedDimensions
from app.schemas.discovery import ProductSort
from app.schemas.product import ProductRow
from app.schemas.retailer import RetailerContext
from app.services.discovery import to_candidate
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.registry import CommerceTaxonomy

FLOOR_CATEGORIES = frozenset(
    {"seating", "bedroom", "tables", "dining", "office", "storage", "fitness"}
)
"""Commerce categories whose pieces stand on the floor and take up room.

The fit check counts only these. A rug lies under the furniture, a lamp or a
vase stands on it, art hangs on a wall: none of them makes a room crowded.
Every entry must be an approved category (tested)."""

MAX_NAME_WORDS = 8

_SORTS = {
    CatalogSort.FEATURED: ProductSort.DEFAULT,
    CatalogSort.PRICE_ASC: ProductSort.PRICE_ASC,
    CatalogSort.PRICE_DESC: ProductSort.PRICE_DESC,
}


class CatalogService:
    def __init__(
        self,
        repository: ProductRepository,
        taxonomy: CommerceTaxonomy,
        attributes: CatalogAttributes,
        settings: CatalogSettings,
        *,
        render_available: bool,
    ) -> None:
        self._repository = repository
        self._taxonomy = taxonomy
        self._attributes = attributes
        self._settings = settings
        self._render_available = render_available

    async def browse(self, query: CatalogQuery, context: RetailerContext) -> CatalogPage:
        filters = self._filter(query)
        size = min(query.page_size or self._settings.page_size, self._settings.max_page_size)
        count = await self._repository.count_browse(filters, context)
        offset = (query.page - 1) * size
        rows = (
            await self._repository.browse(filters, context, offset=offset, limit=size)
            if offset < count
            else []
        )
        return CatalogPage(
            items=tuple(catalog_item(row) for row in rows),
            page=query.page,
            page_size=size,
            total_pages=math.ceil(count / size),
            count=count,
        )

    async def facets(self, context: RetailerContext) -> CatalogFacets:
        types = await self._repository.supported_commerce_types(context)
        counts = await self._repository.facet_counts(context)
        price = counts.prices[0] if counts.prices else None
        return CatalogFacets(
            categories=self._categories(types),
            colors=self._approved(AttributeFamily.COLOR, counts.colors),
            styles=self._approved(AttributeFamily.STYLE, counts.styles),
            price=(
                PriceRange(currency=price[0], min_amount=price[1], max_amount=price[2])
                if price is not None
                else None
            ),
            studio=StudioOptions(
                room_types=tuple(
                    RoomTypeOption(value=room_type, label=label)
                    for room_type, (label, _) in ROOM_TYPES.items()
                ),
                styles=tuple(sorted(self._attributes.styles)),
                max_products=self._settings.max_products,
                max_quantity=self._settings.max_quantity,
                min_room_side_m=self._settings.min_room_side_m,
                max_room_side_m=self._settings.max_room_side_m,
                crowded_floor_ratio=self._settings.crowded_floor_ratio,
                render_available=self._render_available,
            ),
        )

    # ── validation ──────────────────────────────────────────────────────────

    def _filter(self, query: CatalogQuery) -> CatalogFilter:
        category = _slug(query.category)
        subcategory = _slug(query.subcategory)
        if category is not None and not self._taxonomy.is_category(category):
            raise CatalogFilterRejectedError(
                public_message="That category isn't in this catalogue.", category=category
            )
        if (
            category is not None
            and subcategory is not None
            and not self._taxonomy.is_pair(category, subcategory)
        ):
            raise CatalogFilterRejectedError(
                public_message="That type isn't in this category.", subcategory=subcategory
            )
        return CatalogFilter(
            name_words=self._name_words(query.q),
            category=category,
            subcategory=subcategory,
            color=self._canonical(AttributeFamily.COLOR, query.color),
            style=self._canonical(AttributeFamily.STYLE, query.style),
            min_price=query.min_price,
            max_price=query.max_price,
            currency=query.currency.strip() if query.currency else None,
            sort=_SORTS[query.sort],
            # "Featured" opens on furniture: this catalogue is for furnishing a
            # room, and a vase is rarely where that starts.
            lead_categories=tuple(sorted(FLOOR_CATEGORIES)),
        )

    def _name_words(self, q: str | None) -> tuple[str, ...]:
        text = " ".join((q or "").split())
        if len(text) > self._settings.max_query_chars:
            raise CatalogFilterRejectedError(
                public_message="That search is too long. Try a few words from the name."
            )
        return tuple(dict.fromkeys(text.split()))[:MAX_NAME_WORDS]

    def _canonical(self, family: AttributeFamily, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        canonical = self._attributes.canonical(family, value)
        if canonical is None:
            raise CatalogFilterRejectedError(
                public_message=f"That {family.value} isn't one this catalogue uses.",
                family=family.value,
            )
        return canonical

    # ── facets ──────────────────────────────────────────────────────────────

    def _categories(
        self, types: tuple[tuple[str, str | None, int], ...]
    ) -> tuple[CategoryFacet, ...]:
        """Approved categories this store stocks, each with its approved
        subcategories. A category counts every product in it, including those
        whose subcategory was never reviewed."""
        totals: dict[str, int] = {}
        children: dict[str, list[Facet]] = {}
        for category, subcategory, count in types:
            if not self._taxonomy.is_category(category):
                continue
            totals[category] = totals.get(category, 0) + count
            if subcategory is not None and self._taxonomy.is_pair(category, subcategory):
                children.setdefault(category, []).append(Facet(value=subcategory, count=count))
        return tuple(
            CategoryFacet(
                value=category,
                count=totals[category],
                subcategories=tuple(sorted(children.get(category, ()), key=lambda f: f.value)),
            )
            for category in sorted(totals)
        )

    def _approved(
        self, family: AttributeFamily, counted: tuple[tuple[str, int], ...]
    ) -> tuple[Facet, ...]:
        """Stored values mapped onto the vocabulary; spellings of one approved
        value are counted together, and unapproved values are left out."""
        merged: dict[str, int] = {}
        for value, count in counted:
            canonical = self._attributes.canonical(family, value)
            if canonical is not None:
                merged[canonical] = merged.get(canonical, 0) + count
        ordered = sorted(merged.items(), key=lambda item: (-item[1], item[0]))
        return tuple(Facet(value=value, count=count) for value, count in ordered)


def catalog_item(row: ProductRow) -> CatalogItem:
    candidate = to_candidate(row)
    footprint, longest = _floor_facts(candidate.commerce.category, candidate.dimensions)
    return CatalogItem(
        product_id=candidate.product_id,
        name_english=candidate.name_english,
        name_arabic=candidate.name_arabic,
        price_amount=candidate.price_amount,
        price_unit=candidate.price_unit,
        image_url=candidate.image_url,
        product_url=candidate.product_url,
        commerce=candidate.commerce,
        dimensions=candidate.dimensions,
        main_color=candidate.main_color,
        styles=candidate.styles,
        stands_on_floor=candidate.commerce.category in FLOOR_CATEGORIES,
        footprint_cm2=footprint,
        longest_side_cm=longest,
    )


def _floor_facts(
    category: str | None, dimensions: NormalisedDimensions
) -> tuple[float | None, float | None]:
    """Floor area and longest side of a floor-standing piece, or nothing.

    `length` and `width` are the planar axes throughout the catalog (height is
    always `height`), so their product is the footprint whichever way round a
    merchant recorded them.
    """
    if category not in FLOOR_CATEGORIES:
        return None, None
    if not dimensions.is_usable or dimensions.length_cm is None or dimensions.width_cm is None:
        return None, None
    length, width = float(dimensions.length_cm), float(dimensions.width_cm)
    if length <= 0 or width <= 0:
        return None, None
    return round(length * width, 1), max(length, width)


def _slug(value: str | None) -> str | None:
    cleaned = (value or "").strip().lower()
    return cleaned or None
