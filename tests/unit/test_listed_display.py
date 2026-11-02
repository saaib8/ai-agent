"""What the card and the comparison pop-up show of the store's listed extras,
in the shopper's language - and nothing for a store that lists none."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.chat import ChatPresentation
from app.schemas.comparison import (
    ComparisonCell,
    ComparisonField,
    ComparisonRow,
    ComparisonStatus,
    ProductComparisonResult,
)
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.grounding import GroundedProduct
from app.schemas.language import ReplyLanguage
from app.schemas.product import CommerceClassification, ProductDiscount, ProductListing
from app.services.product_display import (
    ProductDisplayWords,
    displayed,
    month_words,
    rating_words,
    weight_words,
)
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.registry import load_taxonomy

WORDS = ProductDisplayWords(load_taxonomy(), load_catalog_attributes())

LISTED = ProductListing(
    material="Velvet",
    rating=Decimal("4.50"),
    review_count=2,
    weight_kg=Decimal("42.500"),
    sku="001234",
    warranty_months=24,
)
DISCOUNT = ProductDiscount(old_price=Decimal("1250"), saving=Decimal("250"), percent=20)


def _product(ref: int, *, listed: bool = True) -> GroundedProduct:
    return GroundedProduct(
        grounding_ref=ref,
        name_english=f"Sofa {ref}",
        price_amount=Decimal("1000"),
        price_unit="SAR",
        image_url="https://x.test/i",
        product_url="https://x.test/p",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        listing=LISTED if listed else ProductListing(),
        discount=DISCOUNT if listed else None,
    )


def test_a_card_shows_the_old_price_discount_weight_and_sku() -> None:
    english = WORDS.product(_product(1), ReplyLanguage.EN)
    arabic = WORDS.product(_product(1), ReplyLanguage.AR)

    assert (english.old_price, english.discount, english.weight, english.sku) == (
        "1,250 SAR",
        "20% off",
        "42.5 kg",
        "001234",
    )
    assert (arabic.old_price, arabic.discount, arabic.weight) == (
        "1,250 ريال",
        "خصم 20%",
        "42.5 كجم",
    )


def test_a_product_with_nothing_listed_sends_exactly_the_old_card() -> None:
    card = WORDS.product(_product(1, listed=False), ReplyLanguage.EN).model_dump(mode="json")

    assert not {"old_price", "discount", "weight", "sku"} & card.keys()


def _cells(*values: str | None) -> tuple[ComparisonCell, ...]:
    return tuple(ComparisonCell(known=v is not None, value=v) for v in values)


def test_the_comparison_cells_are_worded_in_the_shoppers_language() -> None:
    rows = (
        ComparisonRow(
            field=ComparisonField.RATING,
            cells=_cells("4.5 from 2 reviews", None),
            status=ComparisonStatus.UNKNOWN,
        ),
        ComparisonRow(
            field=ComparisonField.WARRANTY,
            cells=_cells("24 months", None),
            status=ComparisonStatus.UNKNOWN,
        ),
        ComparisonRow(
            field=ComparisonField.DISCOUNT,
            cells=_cells("20% off, was 1250 SAR", "none"),
            status=ComparisonStatus.DIFFERENT,
        ),
        ComparisonRow(
            field=ComparisonField.MAIN_COLOR,
            cells=_cells("Beige", "Grey"),
            status=ComparisonStatus.DIFFERENT,
        ),
    )
    compared = ProductComparisonResult(products=(_product(1), _product(2, listed=False)), rows=rows)

    shown = displayed(ChatPresentation(comparison=compared), WORDS, ReplyLanguage.AR)

    assert shown is not None and shown.comparison is not None
    worded = {r.field: [c.display for c in r.cells] for r in shown.comparison.rows}
    assert worded[ComparisonField.RATING] == ["4.5 من تقييمين", None]
    assert worded[ComparisonField.WARRANTY] == ["24 شهرًا", None]
    assert worded[ComparisonField.DISCOUNT] == ["خصم 20%، كان 1,250 ريال", "لا يوجد"]
    # Rows that are not listed extras are worded by the products' display, as before.
    assert worded[ComparisonField.MAIN_COLOR] == [None, None]
    # The English every later turn reads is untouched.
    assert shown.comparison.rows[0].cells[0].value == "4.5 from 2 reviews"


def test_the_old_price_never_reaches_the_json_except_as_checked_words() -> None:
    sent = _product(1).model_dump(mode="json")

    assert "regular_price" not in sent["listing"]
    assert "discount" not in sent


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "4.8 من تقييم واحد"),
        (2, "4.8 من تقييمين"),
        (5, "4.8 من 5 تقييمات"),
        (20, "4.8 من 20 تقييمًا"),
        (100, "4.8 من 100 تقييم"),
        (103, "4.8 من 103 تقييمات"),
        (111, "4.8 من 111 تقييمًا"),
    ],
)
def test_arabic_counts_take_the_form_their_last_two_digits_ask(count: int, expected: str) -> None:
    assert rating_words(Decimal("4.8"), count, ReplyLanguage.AR) == expected


@pytest.mark.parametrize(
    ("stored", "shown"),
    [("0.340", "0.34 kg"), ("1.030", "1.03 kg"), ("42.500", "42.5 kg"), ("1250.000", "1,250 kg")],
)
def test_a_weight_keeps_its_grams(stored: str, shown: str) -> None:
    assert weight_words(Decimal(stored), ReplyLanguage.EN) == shown


def test_one_month_is_singular_in_both_languages() -> None:
    assert month_words(1, ReplyLanguage.EN) == "1 month"
    assert month_words(1, ReplyLanguage.AR) == "شهر واحد"


def test_a_lone_floor_size_is_worded_as_listed_in_both_languages() -> None:
    from app.schemas.dimensions import DimensionStatus as Status
    from app.services.product_display import lone_side_words

    lone = NormalisedDimensions(
        status=Status.NORMALISED, length_cm=Decimal("240"), height_cm=Decimal("85")
    )

    assert lone_side_words(lone, ReplyLanguage.EN) == "240 cm (one side listed)"
    assert lone_side_words(lone, ReplyLanguage.AR) == "240 سم (مذكور ضلع واحد)"
    both = lone.model_copy(update={"width_cm": Decimal("95")})
    assert lone_side_words(both, ReplyLanguage.EN) is None
