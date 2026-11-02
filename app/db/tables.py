"""Read-only SQLAlchemy Core projection of the Django-owned catalog tables.

This is a *projection*, not a model definition. Django owns ``core_product``
and ``core_store`` and every migration that touches them; this service never
writes to them and never migrates them (CLAUDE.md 4).

The column definitions mirror ``core.models.Product`` / ``core.models.Store``
in the Django repository **as deployed today**. The reviewed commerce columns
are present and required; :mod:`app.db.catalog_schema` verifies them at startup.

``material`` is deliberately absent: it is deferred to a later milestone and
this service must not require it or substitute anything for it.

``metadata`` may be used to build an equivalent schema in a *test* database.
It must never be pointed at a real one.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()

core_store = Table(
    "core_store",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("uuid", UUID(as_uuid=True), nullable=False, unique=True),
    Column("name_english", String(255), nullable=False),
    Column("name_arabic", String(255), nullable=True),
    Column("active_status", Boolean, nullable=False),
    # Where the store sells. A Salla store's own id names it to Salla's
    # storefront API, which the cart asks before adding (CLAUDE.md 36).
    Column("provider", String(255), nullable=False),
    Column("salla_merchant_id", Integer, nullable=True),
    # The store's own customer support, set by its owner on the platform. Read
    # only by `SupportContactsRepository`, and only where the support agent is
    # on - so a catalog without the columns runs exactly as before.
    Column("support_email", String(254), nullable=True),
    Column("support_whatsapp", String(15), nullable=True),
    Column("business_id", Integer, nullable=True),
)

# The business a store belongs to, and the person who owns it: only their
# contact, the store's support where it saved none of its own.
core_business = Table(
    "core_business",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("owner_id", Integer, nullable=True),
)

account_user = Table(
    "account_user",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("email", String(254), nullable=False),
    Column("phone", String(255), nullable=True),
)

core_product = Table(
    "core_product",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("uuid", UUID(as_uuid=True), nullable=False, unique=True),
    Column("store_id", Integer, ForeignKey("core_store.id"), nullable=False),
    Column("name_english", String(500), nullable=False),
    Column("name_arabic", String(500), nullable=False),
    Column("price_amount", Numeric(10, 2), nullable=False),
    Column("price_unit", String(255), nullable=False),
    Column("image_url", String(2048), nullable=False),
    Column("product_url", String(2048), nullable=False),
    # The VISUAL classification: what the product looks like in its image.
    # It is NOT the commerce taxonomy and is never used to derive, correct or
    # fall back to the commerce columns below (CLAUDE.md 6.1).
    Column("category", String(255), nullable=True),
    # Reviewed commerce classification, produced by the external product-data
    # preparation process. Authoritative: consumed as-is, never re-derived.
    Column("commerce_category", String(255), nullable=True),
    Column("commerce_subcategory", String(255), nullable=True),
    Column("seating_capacity", SmallInteger, nullable=True),
    # Raw dimensions in whatever unit the merchant supplied. Never compare
    # these across rows: `dimension_unit` varies and is unvalidated upstream.
    Column("length", Numeric(10, 2), nullable=True),
    Column("width", Numeric(10, 2), nullable=True),
    Column("height", Numeric(10, 2), nullable=True),
    Column("dimension_unit", String(50), nullable=True),
    Column("is_active", Boolean, nullable=False),
    Column("detection", Boolean, nullable=False),
    Column("product_color", String(50), nullable=True),
    Column("main_color", String(100), nullable=True),
    Column("secondary_colors", String(100), nullable=True),
    Column("styles", String(100), nullable=True),
    Column("room_types", String(100), nullable=True),
    Column("style_tags", String(100), nullable=True),
    Column("pinecone_id", String(255), nullable=True),
    Column("file_id", Integer, nullable=True),
    Column("two_d_icon", String(2048), nullable=True),
    Column("three_d_model", String(2048), nullable=True),
    Column("salla_product_id", String(255), nullable=True, index=True),
    Column("time_created", DateTime(timezone=True), nullable=True),
    Column("time_updated", DateTime(timezone=True), nullable=True),
    # Product data the store's import fills (Salla's pipeline in Django), read
    # as stored. `material` is the enrichment's one main material, used by
    # meaning and shown as the main material - never a filter (CLAUDE.md 6).
    Column("material", String(100), nullable=True),
    Column("description", Text, nullable=True),
    Column("description_english", Text, nullable=True),
    Column("regular_price", Numeric(10, 2), nullable=True),
    Column("rating", Numeric(3, 2), nullable=True),
    Column("review_count", Integer, nullable=True),
    Column("reviews_json", JSONB, nullable=True),
    # Read only for ordering and the best-seller rule: the number itself never
    # reaches a fact, a reply or a client.
    Column("sold_quantity", Integer, nullable=True),
    Column("weight_kg", Numeric(10, 3), nullable=True),
    Column("sku", String(255), nullable=True),
    Column("warranty_months", SmallInteger, nullable=True),
    # How many the store has, as its import last saw it - a limit on how many
    # one product can fill, never a filter or an order; the store's own cart
    # check at add time is the final word.
    Column("stock_quantity", Integer, nullable=True),
)

# Commerce columns this service requires the live catalog to have. Verified at
# startup (see app/db/catalog_schema.py).
SUPPORT_CONTACT_COLUMNS: frozenset[str] = frozenset({"support_email", "support_whatsapp"})
"""What the support agent reads from ``core_store``, checked at startup where it
is on."""

REQUIRED_COMMERCE_COLUMNS: frozenset[str] = frozenset(
    {
        "commerce_category",
        "commerce_subcategory",
        "seating_capacity",
    }
)

PRODUCT_DATA_COLUMNS: frozenset[str] = frozenset(
    {
        "material",
        "description",
        "description_english",
        "regular_price",
        "rating",
        "review_count",
        "reviews_json",
        "sold_quantity",
        "weight_kg",
        "sku",
        "warranty_months",
        "stock_quantity",
    }
)
"""The product data every product read selects: checked at startup like the
commerce columns, so a catalog without them fails loudly rather than per query.
Empty values are expected - most stores have not filled them."""
