"""The store's product data: what is read, what is believed, what is cleaned."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from app.agents.figures import flatten
from app.core.config import ProductDataSettings
from app.repositories.products import _SELECTED_COLUMNS, _to_product_row
from app.schemas.grounding import GroundedProduct
from app.schemas.product import ProductCandidate, ProductListing
from app.services.discovery import to_candidate
from app.services.grounding_builder import to_grounded_product
from app.services.product_details import cut, shown_reviews, tidy
from app.services.product_value import discount_of
from pydantic import ValidationError

SETTINGS = ProductDataSettings()


def _listing(**fields: Any) -> ProductListing:
    return ProductListing(**fields)


# ── a discount is believed only when it is one ═══════════════════════════════


def test_a_real_reduction_is_worked_out_and_rounded_down() -> None:
    found = discount_of(Decimal("1999"), _listing(regular_price=Decimal("2499")), 0.9)

    assert found is not None
    assert (found.old_price, found.saving, found.percent) == (Decimal("2499"), Decimal("500"), 20)


@pytest.mark.parametrize(
    ("price", "old"),
    [
        (Decimal("1999"), None),  # no old price listed
        (Decimal("1999"), Decimal("1999")),  # the same
        (Decimal("1999"), Decimal("1500")),  # lower than now
        (Decimal("0"), Decimal("100")),  # no current price
        (Decimal("50"), Decimal("1000")),  # 95% off: a data error
        (Decimal("995"), Decimal("1000")),  # under one percent
    ],
)
def test_anything_else_is_no_discount(price: Decimal, old: Decimal | None) -> None:
    assert discount_of(price, _listing(regular_price=old), 0.9) is None


@pytest.mark.parametrize(
    ("price", "old", "expected"),
    [
        (Decimal("100"), Decimal("1000"), 90),  # exactly at the 90% cap: believed
        (Decimal("99"), Decimal("100"), 1),  # exactly 1%: a sale
    ],
)
def test_the_edges_of_a_believable_discount(price: Decimal, old: Decimal, expected: int) -> None:
    found = discount_of(price, _listing(regular_price=old), 0.9)

    assert found is not None and found.percent == expected


# ── reviews: only text, cleaned, the best first ══════════════════════════════


def _review(
    text: str | None, *, stars: Any = 5, verified: bool = False, when: str = ""
) -> dict[str, Any]:
    return {"text": text, "stars": stars, "verified_buyer": verified, "created_at": when or None}


def test_only_reviews_with_text_are_kept_verified_first_then_newest() -> None:
    raw = [
        _review("old, verified", verified=True, when="2025-01-02T10:00:00+03:00"),
        _review(None, verified=True),
        _review("new, unverified", when="2026-09-01T10:00:00+03:00"),
        _review("new, verified", verified=True, when="2026-02-22T13:11:54+03:00"),
        _review("   "),
    ]

    shown = shown_reviews(raw, SETTINGS)

    assert [r.text for r in shown] == ["new, verified", "old, verified", "new, unverified"]
    assert shown[0].written_on == "2026-02-22"


def test_at_most_the_configured_number_are_shown() -> None:
    raw = [_review(f"review {n}") for n in range(8)]

    assert len(shown_reviews(raw, ProductDataSettings(reviews_max_shown=2))) == 2


@pytest.mark.parametrize(
    "contact",
    [
        "+966 55 123 4567",
        "0551234567",
        "055.123.4567",
        "a@b.com",
        "https://x.test/p",
        "cheapsofas.com",
        "wa.me/966551234567",
        "@my_store",
    ],
)
def test_contact_details_and_links_are_taken_out(contact: str) -> None:
    (review,) = shown_reviews([_review(f"great, reach {contact} today")], SETTINGS)

    assert review.text == "great, reach today"


@pytest.mark.parametrize(
    "kept",
    [
        "size 200 180 90 cm",
        "delivered 2025-01-02",
        "10 - 12 - 2025",
        "2 years, 5 stars",
        "4500 SAR",
    ],
)
def test_sizes_dates_and_counts_in_a_review_stay(kept: str) -> None:
    (review,) = shown_reviews([_review(kept)], SETTINGS)

    assert review.text == kept


def test_a_comment_without_a_rating_is_kept_without_stars() -> None:
    (review,) = shown_reviews([_review("nice colour", stars=0)], SETTINGS)

    assert review.stars is None and review.text == "nice colour"


@pytest.mark.parametrize("flag", [True, 1, "true", "True"])
def test_a_verified_buyer_is_read_however_it_is_stored(flag: Any) -> None:
    (review,) = shown_reviews([{"text": "ok", "stars": 4, "verified_buyer": flag}], SETTINGS)

    assert review.verified_buyer


@pytest.mark.parametrize(
    "when",
    [
        "2026-02-22T13:11:54+03:00",
        {"date": "2026-02-22 13:11:54.000000", "timezone": "Asia/Riyadh"},
    ],
)
def test_review_dates_are_read_in_either_stored_form(when: Any) -> None:
    (review,) = shown_reviews([{"text": "ok", "stars": 4, "created_at": when}], SETTINGS)

    assert review.written_on == "2026-02-22"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not json",
        {"stars": 5},
        [{"stars": 9, "text": "too many stars"}],
        [{"stars": True, "text": "a bool is not stars"}],
        ["not a review"],
    ],
)
def test_what_is_not_a_review_list_is_skipped(raw: Any) -> None:
    assert shown_reviews(raw, SETTINGS) == ()


def test_reviews_stored_as_a_json_string_are_read() -> None:
    assert len(shown_reviews(json.dumps([_review("fine")]), SETTINGS)) == 1


def test_long_text_is_cut_at_a_word_and_marked() -> None:
    cut_text = cut("a very long description of a lovely sofa", 20)

    assert cut("abcdefghijklmnopqrstuvwxyz", 10) == "abcdefghi…"
    assert len(cut_text) <= 20 and cut_text.endswith("…")
    words = ["a", "very", "long", "description", "of", "a", "lovely", "sofa"]
    assert cut_text.rstrip("…").split(" ")[-1] in words
    assert tidy("  a  b \n c ") == "a b c" and tidy("   ") is None


# ── the listing rides with every product, and only what is safe ══════════════


def _db_row(**listing: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": 7,
        "uuid": "00000000-0000-0000-0000-000000000007",
        "store_id": 21,
        "name_english": "Sofa",
        "name_arabic": "كنبة",
        "price_amount": Decimal("1999"),
        "price_unit": "SAR",
        "image_url": "https://x.test/i",
        "product_url": "https://x.test/p",
        "category": None,
        "commerce_category": "seating",
        "commerce_subcategory": "sofa",
        "seating_capacity": 3,
        "length": None,
        "width": None,
        "height": None,
        "dimension_unit": None,
        "main_color": "Beige",
        "styles": "Modern",
        "is_active": True,
        "material": None,
        "regular_price": None,
        "rating": None,
        "review_count": None,
        "weight_kg": None,
        "sku": None,
        "warranty_months": None,
    }
    return SimpleNamespace(**{**base, **listing})


def test_a_row_carries_its_listing_to_the_screen() -> None:
    row = _to_product_row(
        _db_row(material=" Velvet ", sku="001234", rating=Decimal("4.5"), review_count=2)
    )  # type: ignore[arg-type]
    grounded = to_grounded_product(to_candidate(row), grounding_ref=1)

    assert grounded.listing == ProductListing(
        material="Velvet", sku="001234", rating=Decimal("4.5"), review_count=2
    )
    assert grounded.model_dump(mode="json")["listing"] == {
        "material": "Velvet",
        "rating": "4.5",
        "review_count": 2,
        "sku": "001234",
    }


@pytest.mark.parametrize(
    "stored",
    [
        {"material": "   "},
        {"review_count": 0},  # the import's "nobody reviewed it"
        {"rating": Decimal("4.0"), "review_count": 0},
        {"rating": None, "review_count": 3},
        {"regular_price": Decimal("2499")},  # never sent, so nothing to send
    ],
)
def test_a_product_with_nothing_to_show_answers_exactly_as_before(stored: dict[str, Any]) -> None:
    row = _to_product_row(_db_row(**stored))  # type: ignore[arg-type]
    grounded = to_grounded_product(to_candidate(row), grounding_ref=1)

    assert row.listing.is_empty
    assert row.listing.rating is None and row.listing.review_count is None
    assert "listing" not in grounded.model_dump(mode="json")


def test_the_old_price_is_kept_but_never_sent() -> None:
    listing = ProductListing(regular_price=Decimal("2499"), material="Velvet")

    assert listing.regular_price == Decimal("2499")
    assert listing.model_dump() == {"material": "Velvet"}


def test_a_rating_never_comes_without_its_count() -> None:
    with pytest.raises(ValidationError):
        ProductListing(rating=Decimal("5"))


def test_the_sales_count_is_never_read_into_a_shown_shape() -> None:
    for shape in (ProductListing, ProductCandidate, GroundedProduct):
        assert "sold_quantity" not in shape.model_fields
    assert "sold_quantity" not in {c.name for c in _SELECTED_COLUMNS}


def test_a_sku_is_never_a_figure() -> None:
    assert flatten({"product": {"sku": "001234", "price": "1999"}}) == {
        "product.price": Decimal("1999")
    }


def test_the_settings_refuse_nonsense() -> None:
    with pytest.raises(ValidationError):
        ProductDataSettings(discount_max_share=1.5)
    with pytest.raises(ValidationError):
        ProductDataSettings(reviews_max_shown=0)
