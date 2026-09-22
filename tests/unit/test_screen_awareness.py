"""What the models may see of the customer's screen, and what they may not.

The pass this file guards changed one thing: an agent that knew *five products
are displayed* now knows which five. That is the difference between "here's
what I found" and "only the second one seats five" (CLAUDE.md 2, 15).

Everything here is one of two claims:

* the projection carries the merchandise the customer is looking at, at the
  positions they would count from, drawn from the same verified objects the
  presentation payload is built from;
* it carries nothing that could address a repository, name a retailer, or be
  emitted back as a handle.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.acquisition import BundleAcquisition
from app.schemas.bundle import BundleStatus
from app.schemas.bundle_presentation import (
    GroundedBundleItem,
    GroundedBundlePresentation,
    GroundedBundleTotals,
)
from app.schemas.comparison import (
    ComparisonCell,
    ComparisonField,
    ComparisonRow,
    ComparisonStatus,
    ProductComparisonResult,
)
from app.schemas.dimensions import DimensionStatus, DimensionUnit, NormalisedDimensions
from app.schemas.grounding import GroundedProduct
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.screen import CustomerVisibleScreenView, PresentedCardView
from app.services.screen_view import (
    cards_from_candidates,
    comparison_view,
    room_view,
    screen_from_presentation,
)
from app.taxonomy.words import customer_words
from pydantic import ValidationError

STORE = 50


def _dimensions() -> NormalisedDimensions:
    return NormalisedDimensions(
        length_cm=Decimal("232"),
        width_cm=Decimal("95"),
        height_cm=Decimal("82"),
        unit=DimensionUnit.CENTIMETRE,
        status=DimensionStatus.NORMALISED,
    )


def _grounded(ref: int, *, ordinal: int | None = None, capacity: int | None = 4) -> GroundedProduct:
    return GroundedProduct(
        grounding_ref=ref,
        presented_ordinal=ordinal,
        name_english=f"Aurora {ref} Seater",
        price_amount=Decimal("4299"),
        price_unit="SAR",
        image_url=f"https://example.test/{ref}.jpg",
        product_url=f"https://example.test/{ref}",
        commerce=CommerceClassification(
            category="seating", subcategory="lounge-chair", seating_capacity=capacity
        ),
        dimensions=_dimensions(),
        main_color="Beige",
        styles=("Modern",),
        relaxation_depth=0,
    )


def _candidate(product_id: int, *, name: str | None = None) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=name or f"Sofa {product_id}",
        name_arabic="كنبة",
        price_amount=Decimal("1990"),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(
            category="seating", subcategory="sofa", seating_capacity=3
        ),
        dimensions=_dimensions(),
        main_color="Beige",
        styles=("Modern",),
    )


# ── A / B. the merchandise crosses ──────────────────────────────────────────


def test_a_card_carries_what_the_customer_can_see() -> None:
    screen = screen_from_presentation(products=(_grounded(1, ordinal=1),))

    card = screen.products[0]
    assert card.presented_ordinal == 1
    assert card.name == "Aurora 1 Seater"
    assert card.price_amount == Decimal("4299")
    assert card.price_unit == "SAR"
    assert card.seating_capacity == 4
    assert card.main_color == "Beige"
    assert card.styles == ("Modern",)
    assert card.dimensions is not None
    assert card.dimensions.length_cm == Decimal("232")


# ── C. position is the handle, and it holds ─────────────────────────────────


def test_card_n_is_the_nth_thing_the_customer_was_shown() -> None:
    screen = screen_from_presentation(
        products=tuple(_grounded(n, ordinal=n) for n in (1, 2, 3))
    )

    assert [card.presented_ordinal for card in screen.products] == [1, 2, 3]


def test_a_product_with_no_position_is_not_a_card() -> None:
    """A selection made three turns ago is real and is not card *n* of
    anything. Inventing a position would claim a place on screen it does not
    have."""
    screen = screen_from_presentation(products=(_grounded(7),))

    assert screen.products == ()


# ── I. a stale row leaves a hole rather than renumbering ────────────────────


def test_a_product_the_catalog_dropped_does_not_pull_the_others_up() -> None:
    """The invariant every ordinal reference rests on.

    Card 3 has to keep meaning the third thing on their screen even when the
    second was deactivated between turns. Closing the gap would resolve "the
    third one" to a product they never saw (CLAUDE.md 7).
    """
    cards = cards_from_candidates((_candidate(10), _candidate(30)), (10, 20, 30))

    assert [card.presented_ordinal for card in cards] == [1, 3]
    assert [card.name for card in cards] == ["Sofa 10", "Sofa 30"]


def test_an_empty_screen_is_an_ordinary_state() -> None:
    assert cards_from_candidates((), ()) == ()
    assert screen_from_presentation().is_empty()


def test_cards_may_not_be_reordered() -> None:
    """Refused at construction. A projection that sorted its cards would make
    every ordinal in the conversation wrong at once."""
    with pytest.raises(ValidationError, match="order they are presented"):
        CustomerVisibleScreenView(
            products=(
                PresentedCardView(presented_ordinal=2, name="b"),
                PresentedCardView(presented_ordinal=1, name="a"),
            )
        )


def test_two_cards_cannot_share_a_position() -> None:
    with pytest.raises(ValidationError, match="same position"):
        CustomerVisibleScreenView(
            products=(
                PresentedCardView(presented_ordinal=1, name="a"),
                PresentedCardView(presented_ordinal=1, name="b"),
            )
        )


# ── D / E / F / G. what never crosses ───────────────────────────────────────


@pytest.mark.parametrize(
    "forbidden",
    [
        "product_id",
        "uuid",
        "store_id",
        "pinecone",
        "image_url",
        "product_url",
        "https://",
        "grounding_ref",
        "relaxation_depth",
        "similarity",
        "score",
    ],
)
def test_no_identity_or_internal_signal_reaches_a_card(forbidden: str) -> None:
    screen = screen_from_presentation(
        products=tuple(_grounded(n, ordinal=n) for n in (1, 2))
    )

    assert forbidden not in screen.model_dump_json(), forbidden


def test_a_card_has_no_field_that_could_address_the_catalog() -> None:
    """Checked on the field names too, so a value that happened to be absent
    in one fixture cannot hide a field that exists."""
    names = set(PresentedCardView.model_fields)

    for forbidden in ("id", "uuid", "url", "store", "score", "ref", "depth"):
        assert not any(forbidden in name for name in names), forbidden


# ── 50. internal vocabulary never reaches prose ─────────────────────────────


def test_a_category_reaches_the_model_as_words() -> None:
    screen = screen_from_presentation(products=(_grounded(1, ordinal=1),))

    assert screen.products[0].commerce_subcategory == "lounge chair"
    assert "lounge-chair" not in screen.model_dump_json()


def test_the_conversion_renames_nothing() -> None:
    """Mechanical, so the registry stays the only vocabulary (CLAUDE.md 14.2)."""
    from app.taxonomy.registry import load_taxonomy

    taxonomy = load_taxonomy()
    for category in taxonomy.categories:
        for subcategory in taxonomy.subcategories(category):
            assert customer_words(subcategory).replace(" ", "-") == subcategory


# ── unclassified data is reported, never guessed ────────────────────────────


def test_an_unreviewed_product_has_no_category_rather_than_a_guessed_one() -> None:
    """The agent reports what the catalog contains. It does not borrow the
    visual classification or read the name (CLAUDE.md 6.1)."""
    product = _grounded(1, ordinal=1).model_copy(
        update={"commerce": CommerceClassification(category=None, subcategory=None)}
    )

    card = screen_from_presentation(products=(product,)).products[0]

    assert card.commerce_category is None
    assert card.commerce_subcategory is None
    assert card.name == "Aurora 1 Seater", "it is still a card on their screen"


def test_an_unusable_unit_yields_no_measurements_at_all() -> None:
    """A number in an unknown scale is worse than no number: 2.2 beside 220
    (CLAUDE.md 15.1)."""
    product = _grounded(1, ordinal=1).model_copy(
        update={"dimensions": NormalisedDimensions(status=DimensionStatus.ABSENT)}
    )

    assert screen_from_presentation(products=(product,)).products[0].dimensions is None


# ── 12. the comparison the customer is looking at ───────────────────────────


def _comparison() -> ProductComparisonResult:
    return ProductComparisonResult(
        products=(_grounded(1, ordinal=1), _grounded(2, ordinal=2)),
        rows=(
            ComparisonRow(
                field=ComparisonField.PRICE,
                cells=(
                    ComparisonCell(known=True, value="SAR 4299"),
                    ComparisonCell(known=True, value="SAR 5100"),
                ),
                status=ComparisonStatus.DIFFERENT,
            ),
            ComparisonRow(
                field=ComparisonField.SEATING_CAPACITY,
                cells=(ComparisonCell(known=False), ComparisonCell(known=False)),
                status=ComparisonStatus.UNKNOWN,
            ),
        ),
    )


def test_a_comparison_projects_its_columns_by_position() -> None:
    view = comparison_view(_comparison())

    assert view.ordinals == (1, 2)
    assert view.rows[0].field is ComparisonField.PRICE
    assert [cell.value for cell in view.rows[0].cells] == ["SAR 4299", "SAR 5100"]


def test_an_unknown_cell_stays_unknown() -> None:
    """Hiding it would let a reply imply the two were equal on that field."""
    view = comparison_view(_comparison())

    material = next(row for row in view.rows if row.field is ComparisonField.SEATING_CAPACITY)
    assert material.status is ComparisonStatus.UNKNOWN
    assert all(cell.known is False for cell in material.cells)


def test_a_comparison_without_positions_is_withheld_rather_than_mislabelled() -> None:
    """With no ordinal there is no safe handle for a column, and a table the
    reply cannot point at correctly is worse than no table."""
    off_screen = ProductComparisonResult(
        products=(_grounded(1), _grounded(2)), rows=()
    )

    assert comparison_view(off_screen).ordinals == ()


# ── 11. the room the customer is looking at ─────────────────────────────────


def _room() -> GroundedBundlePresentation:
    return GroundedBundlePresentation(
        status=BundleStatus.COMPLETE,
        items=(
            GroundedBundleItem(
                grounding_ref=1,
                name_english="Aurora Sofa",
                image_url="https://example.test/a.jpg",
                product_url="https://example.test/a",
                commerce=CommerceClassification(category="seating", subcategory="sofa"),
                quantity=1,
                acquisition=BundleAcquisition.TO_BUY,
                locked=True,
                unit_price=Decimal("4299"),
                price_unit="SAR",
                new_spend_line_total=Decimal("4299"),
            ),
            GroundedBundleItem(
                grounding_ref=2,
                name_english="Old Rug",
                image_url="https://example.test/b.jpg",
                product_url="https://example.test/b",
                commerce=CommerceClassification(category="decor", subcategory="carpet"),
                quantity=1,
                acquisition=BundleAcquisition.ALREADY_OWNED,
                locked=True,
                unit_price=Decimal("600"),
                price_unit="SAR",
            ),
        ),
        totals=GroundedBundleTotals(
            new_spend_total=Decimal("4299"),
            currency="SAR",
            budget_max_amount=Decimal("12000"),
            budget_currency="SAR",
            within_budget=True,
        ),
    )


def test_a_room_projects_its_pieces_and_its_arithmetic() -> None:
    view = room_view(_room())

    assert view.status is BundleStatus.COMPLETE
    assert [line.presented_ordinal for line in view.cards] == [1, 2]
    assert view.new_spend_total == Decimal("4299")
    assert view.budget_max_amount == Decimal("12000")
    assert view.within_budget is True


def test_an_owned_piece_adds_nothing_to_the_spend() -> None:
    """The absence is the point: a line total on an owned piece would say they
    are buying it again."""
    view = room_view(_room())

    owned = next(
        line for line in view.cards if line.acquisition is BundleAcquisition.ALREADY_OWNED
    )
    assert owned.new_spend_line_total is None
    assert owned.locked is True


def test_a_room_card_carries_no_internal_identity() -> None:
    rendered = room_view(_room()).model_dump_json()

    for forbidden in ("line_id", "need_id", "product_id", "https://", "bundle_revision"):
        assert forbidden not in rendered, forbidden
