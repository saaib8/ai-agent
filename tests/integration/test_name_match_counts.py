"""Counting a card's chips in product names, against a real PostgreSQL.

The unit suite proves which chips a card offers given the counts, and what
each pattern finds in Python. This proves PostgreSQL counts the same: names
folded alike (accents, Arabic spellings), whole words with their plural, a
hyphen or space between words, and only this store's active products of the
types asked about.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.db.tables import core_product, core_store
from app.integrations.postgres import Database
from app.repositories.products import ProductRepository
from app.schemas.retailer import RetailerContext
from app.taxonomy.name_match import matches, name_match
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

STORE_A = 1
STORE_B = 2
CONTEXT = RetailerContext(store_id=STORE_A)

NAMES: list[tuple[int, str, str, str, int, bool]] = [
    # id, English name, Arabic name, sub-category, store, active
    (1, "Queen Fabric Storage Bed with Gas-Lift Base", "سرير تخزين", "bed", STORE_A, True),
    (2, "Single Upholstered Bed with Reading Light", "سرير مبطن", "bed", STORE_A, True),
    (3, "King Upholstered Bed - Light Beige", "سرير كينج", "bed", STORE_A, True),
    (
        4,
        "Bouclé Accent Sofa Chair - Rolled Arm",
        "كرسي بوكليه",
        "single-seater-sofa",
        STORE_A,
        True,
    ),
    (5, "Armless Boucle Sofa Chair", "كرسي بدون ذراعين", "single-seater-sofa", STORE_A, True),
    (
        6,
        "Chenille Sofa Chair with Rolled Arms",
        "كنبة مخـمـل مفردة",
        "single-seater-sofa",
        STORE_A,
        True,
    ),
    (7, "Retired Gas Lift Bed", "سرير", "bed", STORE_A, False),
    (8, "Other Store Gas-Lift Bed", "سرير", "bed", STORE_B, True),
]

CHECKS = {
    "lift": ["gas lift", "ottoman"],
    "light": ["reading light"],
    "upholstered": ["upholstered"],
    "boucle": ["bouclé"],
    "arms": ["arms"],
    "velvet": ["velvet", "مخمل"],
    "nothing": ["four poster"],
}


def _product(
    product_id: int, name: str, arabic: str, sub: str, store: int, active: bool
) -> dict[str, Any]:
    return {
        "id": product_id,
        "uuid": uuid4(),
        "store_id": store,
        "name_english": name,
        "name_arabic": arabic,
        "price_amount": Decimal("1000"),
        "price_unit": "SAR",
        "image_url": f"https://example.test/{product_id}.jpg",
        "product_url": f"https://example.test/{product_id}",
        "category": sub,
        "commerce_category": "bedroom" if sub == "bed" else "seating",
        "commerce_subcategory": sub,
        "is_active": active,
        "detection": False,
    }


@pytest.fixture
async def seeded(writable_engine: AsyncEngine) -> None:
    async with writable_engine.begin() as connection:
        await connection.execute(
            core_store.insert(),
            [
                {"id": s, "uuid": uuid4(), "name_english": f"Store {s}", "active_status": True}
                for s in (STORE_A, STORE_B)
            ],
        )
        await connection.execute(core_product.insert(), [_product(*row) for row in NAMES])


async def _counts(database: Database, types: tuple[str, ...]) -> dict[str, int]:
    checks = {key: name_match(terms) for key, terms in CHECKS.items()}
    async with database.session() as session:
        return await ProductRepository(session).name_match_counts(types, checks, CONTEXT)


async def test_postgresql_counts_what_the_patterns_find(database: Database, seeded: None) -> None:
    types = ("bed", "single-seater-sofa")

    counts = await _counts(database, types)

    expected = {
        key: sum(
            1
            for _id, name, arabic, sub, store, active in NAMES
            if store == STORE_A
            and active
            and sub in types
            and matches(name_match(terms), name, arabic)
        )
        for key, terms in CHECKS.items()
    }
    assert counts == expected
    assert counts == {
        "lift": 1,  # gas-lift, hyphenated
        "light": 1,  # never "Light Beige"
        "upholstered": 2,
        "boucle": 2,  # Bouclé and Boucle
        "arms": 2,  # Arm and Arms, never Armless
        "velvet": 1,  # the Arabic name, stretched
        "nothing": 0,
    }


async def test_only_this_stores_active_products_of_these_types_count(
    database: Database, seeded: None
) -> None:
    counts = await _counts(database, ("bed",))

    # The retired bed and the other store's bed are named gas-lift too.
    assert counts["lift"] == 1
    assert counts["boucle"] == 0


async def test_no_chip_to_check_reads_nothing(database: Database, seeded: None) -> None:
    async with database.session() as session:
        assert await ProductRepository(session).name_match_counts(("bed",), {}, CONTEXT) == {}
