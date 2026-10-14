"""Internal product contracts.

``ProductRow`` is the typed projection that leaves the repository. Raw database
rows never cross that boundary. This is *not* the customer-facing shape: the
allowlisted public DTO arrives with the guardrail milestone (CLAUDE.md 20.4).
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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


class ProductCandidate(BaseModel):
    """A discovery result: the allowlisted view of a catalog product.

    An explicit allowlist, not a filtered row. Adding a column to
    `core_product` can never leak it through this shape (CLAUDE.md 20.4).

    Deliberately absent: `store_id` (scope is the server's business, never the
    caller's), `pinecone_id`, `file_id`, `is_active` (everything here is
    active), and the visual `category` - exposing the latter would invite
    exactly the derivation the architecture forbids (CLAUDE.md 6.1).

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
