"""Browse Catalogue contracts: filtering the store's catalog, and rendering a
room from pieces the customer picked.

Browsing is scoped like every other read: the store comes from the request's
`RetailerContext`, never from a filter, so a page can only ever hold the active
store's active products. Filter values that name vocabulary - a category, a
colour, a style - are checked against the registries by the service before
they reach SQL (CLAUDE.md 14.3).

A catalogue render names products by id. The ids are only a request: each is
read back through the store-scoped repository, so a product the store does not
sell, or no longer sells, is simply not in the picture.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.dimensions import NormalisedDimensions
from app.schemas.discovery import ProductSort
from app.schemas.furniture_finder import SESSION_ID_PATTERN
from app.schemas.product import CommerceClassification
from app.schemas.visualization import RenderRoomSpec, RenderView, RoomType

# ── browsing ────────────────────────────────────────────────────────────────


class CatalogSort(StrEnum):
    FEATURED = "featured"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"


class CatalogQuery(BaseModel):
    """One page of the catalog, as the browse screen asks for it.

    Transport-level bounds only. Page size and query length are bounded again
    by configuration in the service, and vocabulary values are validated there.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    store_id: int = Field(ge=1)
    q: str | None = Field(default=None, max_length=200)
    """Words in the product's English or Arabic name."""

    category: str | None = Field(default=None, max_length=64)
    subcategory: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, max_length=64)
    style: str | None = Field(default=None, max_length=64)
    min_price: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    max_price: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    currency: str | None = Field(default=None, min_length=1, max_length=16)
    """Required with a price bound: amounts are never compared across
    currencies, so a bound without one cannot be applied."""

    sort: CatalogSort = CatalogSort.FEATURED
    page: int = Field(default=1, ge=1, le=10_000)
    page_size: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _price_bounds_are_usable(self) -> Self:
        has_bound = self.min_price is not None or self.max_price is not None
        if has_bound and self.currency is None:
            raise ValueError("a price bound needs a currency")
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price cannot exceed max_price")
        if self.subcategory is not None and self.category is None:
            raise ValueError("a subcategory needs its category")
        return self


class CatalogFilter(BaseModel):
    """A browse query after validation: exactly what the repository executes.

    Every vocabulary value here is approved, and `name_words` are the
    customer's words already split and bounded; the repository escapes them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name_words: tuple[str, ...] = ()
    category: str | None = None
    subcategory: str | None = None
    color: str | None = None
    style: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    currency: str | None = None
    sort: ProductSort = ProductSort.DEFAULT
    lead_categories: tuple[str, ...] = ()
    """Categories shown ahead of the rest, before the sort's own order. Only
    the default order uses it: a price sort is an instruction, and nothing
    goes ahead of the cheapest."""


class CatalogItem(BaseModel):
    """One product on a browse page: an allowlisted view, like a search result.

    `stands_on_floor`, `footprint_cm2` and `longest_side_cm` exist for the
    room fit check. A rug, a lamp or a vase does not stand on the floor in the
    sense that crowds a room, and has neither figure. A floor piece whose size
    the catalog could not normalise has no figures either, and the check says
    it was not counted rather than guessing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: int
    name_english: str
    name_arabic: str
    price_amount: Decimal
    price_unit: str
    image_url: str
    product_url: str
    commerce: CommerceClassification
    dimensions: NormalisedDimensions
    main_color: str | None
    styles: tuple[str, ...]
    stands_on_floor: bool = False
    footprint_cm2: float | None = None
    longest_side_cm: float | None = None


class CatalogPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[CatalogItem, ...]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total_pages: int = Field(ge=0)
    count: int = Field(ge=0)
    """Products matching the filters, across every page."""


# ── facets ──────────────────────────────────────────────────────────────────


class Facet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str
    count: int = Field(ge=1)


class CategoryFacet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str
    count: int = Field(ge=1)
    subcategories: tuple[Facet, ...] = ()


class PriceRange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: str
    min_amount: Decimal
    max_amount: Decimal


class RoomTypeOption(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: RoomType
    label: str


class StudioOptions(BaseModel):
    """What the room-setup step offers and allows, so the screen never keeps
    its own copy of a vocabulary or a limit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_types: tuple[RoomTypeOption, ...]
    styles: tuple[str, ...]
    """Every approved style, not only those this store tags: a room's style
    describes the room, not the products in it."""

    max_products: int = Field(ge=1)
    max_quantity: int = Field(ge=1)
    min_room_side_m: float = Field(gt=0)
    max_room_side_m: float = Field(gt=0)
    crowded_floor_ratio: float = Field(gt=0, le=1)
    render_available: bool
    """False when no image model is configured: browsing still works."""


class CatalogFacets(BaseModel):
    """The filter values this store's live catalog actually has, with counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    categories: tuple[CategoryFacet, ...]
    colors: tuple[Facet, ...]
    styles: tuple[Facet, ...]
    price: PriceRange | None
    studio: StudioOptions


# ── rendering a selection ───────────────────────────────────────────────────


class CatalogSelectionItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: int = Field(ge=1)
    quantity: int = Field(default=1, ge=1, le=100)


class CatalogVisualizeRequest(BaseModel):
    """Render a room from picked products. Counts are bounded by
    configuration in the service; these bounds only refuse absurd input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, pattern=SESSION_ID_PATTERN)
    store_id: int = Field(ge=1)
    items: tuple[CatalogSelectionItem, ...] = Field(min_length=1, max_length=50)
    room: RenderRoomSpec
    view: RenderView = RenderView.CORNER
    expected_session_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _each_product_once(self) -> Self:
        ids = [item.product_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("each product may appear once; use its quantity for more")
        return self
