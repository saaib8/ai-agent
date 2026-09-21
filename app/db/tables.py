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
)
from sqlalchemy.dialects.postgresql import UUID

metadata = MetaData()

core_store = Table(
    "core_store",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("uuid", UUID(as_uuid=True), nullable=False, unique=True),
    Column("name_english", String(255), nullable=False),
    Column("name_arabic", String(255), nullable=True),
    Column("active_status", Boolean, nullable=False),
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
)

# Commerce columns this service requires the live catalog to have. Verified at
# startup (see app/db/catalog_schema.py). `material` is NOT here: it is
# deferred, and nothing may depend on it.
REQUIRED_COMMERCE_COLUMNS: frozenset[str] = frozenset(
    {
        "commerce_category",
        "commerce_subcategory",
        "seating_capacity",
    }
)
