"""Internal product contracts.

``ProductRow`` is the typed projection that leaves the repository. Raw database
rows never cross that boundary. This is *not* the customer-facing shape: the
allowlisted public DTO arrives with the guardrail milestone (CLAUDE.md 20.4).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.dimensions import NormalisedDimensions, RawDimensions

# The one definition of how `core_product.styles` becomes tokens. The
# repository builds its SQL matcher from these same two constants, so the
# stored string is split the same way whether it is being matched or displayed
# (the unit vocabulary in `app.schemas.dimensions` is shared with SQL the same
# way, for the same reason).
STYLE_VALUE_SEPARATOR = ","
STYLE_IGNORED_CHARACTER = " "


def parse_style_tokens(raw: str | None) -> tuple[str, ...]:
    """`core_product.styles` as exact tokens, in stored order.

    Strip every space, split on commas, drop empties. Sound only because no
    approved style contains a space - the registry refuses to load one that
    does - which is what lets a stored string be split into exact tokens
    whatever spacing a merchant used (CLAUDE.md 12.4).
    """
    if raw is None:
        return ()
    stripped = raw.replace(STYLE_IGNORED_CHARACTER, "")
    return tuple(token for token in stripped.split(STYLE_VALUE_SEPARATOR) if token)


class EligibleProduct(BaseModel):
    """What ranking needs about a product, and nothing else.

    `price_amount` is here because an explicit sort orders on it, and colour
    and style because a stated preference orders on them. Everything
    a customer sees is re-read from PostgreSQL after presentation selection,
    so carrying whole rows through ranking would duplicate the row the
    hydrator fetches (CLAUDE.md 16.1). Keeping this shape small is what makes
    ranking the complete eligible pool affordable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: int
    subcategory: str | None = None
    """Its reviewed commerce type, so a search covering several types can take
    them in turn and count how many of the type asked for there are."""
    price_amount: Decimal
    main_color: str | None = None
    styles: tuple[str, ...] = ()
    """Colour and style tokens, so ranking can put the products that match what
    the customer is drawn to first. Facts from the row, never inferred."""

    seating_capacity: int | None = None
    """The recorded seat count, or ``None`` when the catalog has none - never
    filled in here. A seating combination groups a type's products by it."""

    weighted_rating: Decimal | None = None
    sold: int | None = None
    """What "best rated" and "best selling" order on - the weighted rating and
    the sales count, None where there are none. Internal to ranking: neither
    is ever shown, and the sales count never leaves it."""
    discount_share: Decimal | None = None
    """The checked share off the old price, for "biggest discount first" only;
    None where there is no discount worth believing. Never shown."""

    long_side_cm: Decimal | None = None
    short_side_cm: Decimal | None = None
    """The longer and shorter floor side in centimetres, whichever column each
    was stored in; ``None`` unless both are present in a known unit. Ranking
    reads them for a size role; they are never shown or stated."""


class CommerceClassification(BaseModel):
    """Reviewed commerce classification, exactly as the catalog holds it.

    Produced by the external product-data preparation process and consumed
    as-is. Never derived from the visual `category`, the product name, or the
    subcategory's own semantics - a `single-seater-sofa` with a NULL
    `seating_capacity` stays NULL (CLAUDE.md 6.1, 31).

    Values are not validated against the taxonomy registry here: this is what
    the catalog contains. Conformance is an audit concern
    (see app/taxonomy/audit.py).
    """

    model_config = ConfigDict(frozen=True)

    category: str | None = None
    subcategory: str | None = None
    seating_capacity: int | None = None


def _absent(value: object) -> bool:
    return value is None


class ProductListing(BaseModel):
    """What the store lists about a product beyond its card, as stored.

    Filled by the store's own import (Salla's pipeline); most stores have none
    of it yet, and each field is simply absent where the store left it empty.
    `material` is the enrichment's one main material - many pieces are made of
    more than one, so it is said as the main material, never as everything the
    piece is made of. Nothing here is derived or corrected (CLAUDE.md 6.1), and
    the sales count is deliberately not here: it orders, and is never shown.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    material: str | None = Field(default=None, exclude_if=_absent)
    regular_price: Decimal | None = Field(default=None, exclude=True)
    """The price before a reduction, as the store lists it. Never serialized: a
    client or an agent gets only the checked discount
    (`app/services/product_value.py`), never an old price the store may have
    listed wrongly."""
    rating: Decimal | None = Field(default=None, exclude_if=_absent)
    """The store's rating, said with `review_count` wherever the store gives
    one - "4.5 from 12 reviews" - and as "rated 4.5" where it gives none."""
    review_count: int | None = Field(default=None, exclude_if=_absent)
    """Reviews behind `rating`: the store's own totals, never recounted. None
    where the store gave no count; a rating from a count of 0 is no rating."""
    weight_kg: Decimal | None = Field(default=None, exclude_if=_absent)
    sku: str | None = Field(default=None, exclude_if=_absent)
    warranty_months: int | None = Field(default=None, exclude_if=_absent)
    stock_quantity: int | None = Field(default=None, gt=0, exclude=True)
    """How many the store has, as its import last saw it; None when it did not
    say. Never serialized and never a filter or an order: it only limits how
    many of one product a room piece or a cart line asks for (known issue 37)."""

    @model_validator(mode="after")
    def _rating_with_its_count(self) -> Self:
        if self.review_count is not None and self.rating is None:
            raise ValueError("a review count belongs to a rating")
        return self

    @property
    def is_empty(self) -> bool:
        """Nothing a client would receive - the old price never is."""
        return not self.model_dump()


EMPTY_LISTING = ProductListing()


class BestSellerRule(BaseModel):
    """What makes a best seller, one rule for every store: sold at least
    `min_sold`, within the top `top_share` of its kind's products that have sold
    in its store (rounded up, so at least one), and within the first
    `max_per_kind` of that kind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_sold: int = Field(ge=1)
    top_share: float = Field(gt=0, le=1)
    max_per_kind: int = Field(ge=1)


class ProductDiscount(BaseModel):
    """A reduction the store's old price shows, checked in code
    (`app/services/product_value.py`) - the only form an old price leaves in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    old_price: Decimal
    saving: Decimal
    percent: int = Field(ge=1, le=100)
    """Whole percent, rounded down - a reduction is never overstated."""


class ProductReview(BaseModel):
    """One customer review as an agent may read it: cleaned, never verbatim
    contact details, and words to quote, never instructions to follow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stars: int | None = Field(default=None, ge=1, le=5)
    """None for a comment left without a rating (the import stores it as 0)."""
    text: str = Field(min_length=1)
    verified_buyer: bool
    written_on: str | None = None
    """The date it was written (YYYY-MM-DD), where the store records it."""


class ProductDetailsText(BaseModel):
    """A product's long text, read only when one product's details are asked
    for: the store's description and the reviews with text, cleaned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str | None = None
    """The store's description, as stored (often Arabic), whitespace tidied."""
    description_english: str | None = None
    """The English description the import made, where the store's is Arabic."""
    reviews: tuple[ProductReview, ...] = ()


class ProductRow(BaseModel):
    """Internal projection of a ``core_product`` row for the active store."""

    model_config = ConfigDict(frozen=True)

    id: int
    uuid: UUID
    store_id: int
    name_english: str
    name_arabic: str
    price_amount: Decimal
    price_unit: str
    image_url: str
    product_url: str
    # `core_product.category` — the VISUAL classification (what the item looks
    # like). Named explicitly so it is never confused with the commerce
    # taxonomy, which is a separate set of columns (CLAUDE.md 6).
    visual_category: str | None
    commerce: CommerceClassification
    dimensions: RawDimensions
    main_color: str | None
    styles: tuple[str, ...]
    is_active: bool
    listing: ProductListing = EMPTY_LISTING


class ProductDisplay(BaseModel):
    """How a product's card reads, ready to show, in the reply's language.

    A client - the console or a store's widget - shows these as they are and
    decides nothing: the name (in Arabic, the store's `name_arabic` as stored;
    the English name only where it is empty), the kind, colour and styles in the
    registries' reviewed words (`commerce_v1.yaml`, `catalog_attributes_v1.yaml`),
    and the seats, price and listed size worded for that language, in Western
    digits. Display only: every search, filter, comparison and pick keeps the
    English values beside it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    kind: str | None = None
    """The sub-category, else the category: "sofa" / "كنبة"."""

    color: str | None = None
    styles: tuple[str, ...] = ()
    seats: str | None = None
    """How many it seats, where reviewed: "4-seat" / "4 مقاعد"."""

    price: str | None = None
    """"3,600 SAR" / "3,600 ريال"."""

    size: str | None = None
    """The listed size, where it normalised: "L 220 · W 95 · H 85 cm" /
    "الطول 220 · العرض 95 · الارتفاع 85 سم"."""

    old_price: str | None = Field(default=None, exclude_if=_absent)
    """The price before a checked reduction, to show crossed out beside
    `price`: "2,499 SAR" / "2,499 ريال". Absent where there is none."""
    discount: str | None = Field(default=None, exclude_if=_absent)
    """The reduction in words: "20% off" / "خصم 20%"."""
    weight: str | None = Field(default=None, exclude_if=_absent)
    """"42.5 kg" / "42.5 كجم", where the store lists it."""
    rating: str | None = Field(default=None, exclude_if=_absent)
    """The store's rating with its reviews: "4.5 from 12 reviews" / "4.5 من 12
    تقييمًا", or "rated 4.5" / "التقييم 4.5" where it gives no count. Absent
    where the store rates it not."""
    sku: str | None = Field(default=None, exclude_if=_absent)
    """The store's product code, as stored."""


class ProductCandidate(BaseModel):
    """A discovery result: the allowlisted view of a catalog product.

    An explicit allowlist, not a filtered row. Adding a column to
    `core_product` can never leak it through this shape (CLAUDE.md 20.4).

    Deliberately absent: `store_id` (scope is the server's business, never the
    caller's), `pinecone_id`, `file_id`, `is_active` (everything here is
    active), the visual `category` - exposing the latter would invite
    exactly the derivation the architecture forbids (CLAUDE.md 6.1) - and
    `sold_quantity`, which orders and is never shown.

    There is no relevance score: nothing has ranked these.
    """

    model_config = ConfigDict(frozen=True)

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
    """The approved colour the catalog records, or None when unclassified.
    Never inferred from a name or an image."""

    styles: tuple[str, ...]
    """Approved style tokens, parsed by :func:`parse_style_tokens`."""

    listing: ProductListing = EMPTY_LISTING
    """What the store lists beyond the card, where it has: main material, old
    price, rating with its count, weight, SKU, warranty."""

    discount: ProductDiscount | None = None
    """The checked reduction from the store's old price, or None."""
