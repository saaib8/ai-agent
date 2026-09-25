"""The read-only product projection: colour, styles and the ranking shape.

Two things are proved here. Style tokens have exactly one behavioural
definition, shared with the SQL matcher, so a product can never be displayed
with a style it would not match on. And `EligibleProduct` stays the minimum
ranking needs, because its smallness is what makes reading the complete
eligible pool affordable.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from app.schemas.dimensions import RawDimensions
from app.schemas.product import (
    STYLE_IGNORED_CHARACTER,
    STYLE_VALUE_SEPARATOR,
    CommerceClassification,
    EligibleProduct,
    ProductRow,
    parse_style_tokens,
)
from app.services.discovery import to_candidate
from pydantic import ValidationError


def _row(**overrides: object) -> ProductRow:
    values: dict[str, object] = {
        "id": 1,
        "uuid": uuid4(),
        "store_id": 50,
        "name_english": "Sofa",
        "name_arabic": "كنبة",
        "price_amount": Decimal("2450.00"),
        "price_unit": "SAR",
        "image_url": "https://example.test/1.jpg",
        "product_url": "https://example.test/1",
        "visual_category": "3-seater-sofa",
        "commerce": CommerceClassification(category="seating", subcategory="sofa"),
        "dimensions": RawDimensions(unit="cm"),
        "main_color": "Beige",
        "styles": ("Modern",),
        "is_active": True,
    }
    values.update(overrides)
    return ProductRow(**values)


# ── style tokens ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Modern, Minimalist", ("Modern", "Minimalist")),
        ("Modern,Minimalist", ("Modern", "Minimalist")),
        ("  Modern ,  Minimalist  ", ("Modern", "Minimalist")),
        ("Modern", ("Modern",)),
        ("", ()),
        ("   ", ()),
        (",,", ()),
        ("Modern,,Zen", ("Modern", "Zen")),
        (None, ()),
    ],
)
def test_stored_styles_become_exact_tokens(
    raw: str | None, expected: tuple[str, ...]
) -> None:
    """Merchant spacing varies row to row; the tokens must not."""
    assert parse_style_tokens(raw) == expected


def test_a_token_is_never_a_substring_match() -> None:
    """`Modern` must not be found inside `Modern_Classic` (CLAUDE.md 12.4)."""
    assert parse_style_tokens("Modern_Classic") == ("Modern_Classic",)
    assert "Modern" not in parse_style_tokens("Rustic_Modern")


def test_stored_order_is_preserved() -> None:
    assert parse_style_tokens("Zen, Modern") == ("Zen", "Modern")


def test_the_sql_matcher_is_built_from_the_same_constants() -> None:
    """One behavioural definition, shared across the language boundary.

    Python cannot execute the SQL rule, so the guarantee is that both are
    generated from the same two constants rather than written twice. Parity on
    real rows is proved against PostgreSQL in the integration suite.
    """
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/repositories/products.py").read_text()

    assert "STYLE_IGNORED_CHARACTER" in source
    assert "STYLE_VALUE_SEPARATOR" in source
    assert 'func.replace(core_product.c.styles, " ", "")' not in source
    assert STYLE_VALUE_SEPARATOR == ","
    assert STYLE_IGNORED_CHARACTER == " "


# ── the candidate projection ────────────────────────────────────────────────


def test_a_candidate_carries_the_recorded_colour_and_styles() -> None:
    candidate = to_candidate(_row(main_color="Taupe", styles=("Modern", "Zen")))

    assert candidate.main_color == "Taupe"
    assert candidate.styles == ("Modern", "Zen")


def test_an_unclassified_colour_stays_absent() -> None:
    """NULL is unclassified, never a colour guessed from the name."""
    candidate = to_candidate(_row(main_color=None, styles=()))

    assert candidate.main_color is None
    assert candidate.styles == ()


# ── the ranking shape ───────────────────────────────────────────────────────


def test_eligible_product_carries_only_what_ranking_orders_on() -> None:
    assert set(EligibleProduct.model_fields) == {
        "product_id",
        "price_amount",
        "main_color",
        "styles",
    }


def test_eligible_product_is_frozen_and_closed() -> None:
    product = EligibleProduct(product_id=1, price_amount=Decimal("10"))

    with pytest.raises(ValidationError):
        product.product_id = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        EligibleProduct(
            product_id=1, price_amount=Decimal("10"), name_english="leak"
        )  # type: ignore[call-arg]


def test_eligible_product_carries_no_customer_facing_fact() -> None:
    """Names, URLs and dimensions are re-read at hydration, never remembered."""
    for forbidden in ("name", "url", "image", "dimension", "commerce", "store"):
        assert not any(forbidden in f for f in EligibleProduct.model_fields), forbidden
