"""Building the safe screen view from what the application actually rendered.

One rule governs this module: **project, never re-derive**. Every value here is
copied from the object the presentation payload was built from, so a figure the
model reads and a figure the customer sees cannot disagree. Nothing is
computed, nothing is summed, nothing is looked up a second time.

Two callers, at two moments, and the difference matters.

The **response** model is shown the screen this turn produced, because that is
what its words will sit beside.

The **decision** model is shown the screen the customer was looking at *when
they typed*, which is the previous turn's. Those cards are rebuilt by hydrating
the recorded ids fresh, never by remembering product facts in session state: a
price read from state is a price that was true once (CLAUDE.md 61, 62).

Pure and total. No I/O, no clock, no model.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.schemas.bundle_presentation import GroundedBundlePresentation
from app.schemas.comparison import ProductComparisonResult
from app.schemas.dimensions import NormalisedDimensions
from app.schemas.grounding import GroundedProduct
from app.schemas.product import ProductCandidate
from app.schemas.screen import (
    CustomerVisibleScreenView,
    PresentedCardView,
    ScreenCardDimensions,
    ScreenComparisonCellView,
    ScreenComparisonRowView,
    ScreenComparisonView,
    ScreenRoomCardView,
    ScreenRoomView,
)
from app.taxonomy.words import customer_words_or_none


def _dimensions(dimensions: NormalisedDimensions) -> ScreenCardDimensions | None:
    """Centimetres, or nothing.

    `is_usable` is the whole check: a product whose unit the catalog could not
    recognise has no comparable measurement, and printing the raw number would
    put 2.2 metres beside 220 centimetres (CLAUDE.md 15.1).
    """
    if not dimensions.is_usable:
        return None
    if (
        dimensions.length_cm is None
        and dimensions.width_cm is None
        and dimensions.height_cm is None
    ):
        return None
    return ScreenCardDimensions(
        length_cm=dimensions.length_cm,
        width_cm=dimensions.width_cm,
        height_cm=dimensions.height_cm,
    )


def card_from_grounded(product: GroundedProduct) -> PresentedCardView | None:
    """One rendered search result as a safe card.

    None when the product has no position on screen. A product grounded by
    another route - a comparison, a detail question, a selection made three
    turns ago - is real but is not card *n* of anything, and inventing a
    position would claim a place in a list it does not occupy.
    """
    if product.presented_ordinal is None:
        return None
    return PresentedCardView(
        presented_ordinal=product.presented_ordinal,
        name=product.name_english,
        commerce_category=customer_words_or_none(product.commerce.category),
        commerce_subcategory=customer_words_or_none(product.commerce.subcategory),
        price_amount=product.price_amount,
        price_unit=product.price_unit,
        seating_capacity=product.commerce.seating_capacity,
        dimensions=_dimensions(product.dimensions),
        main_color=product.main_color,
        styles=product.styles,
    )


def cards_from_candidates(
    products: Sequence[ProductCandidate], presented_ids: Sequence[int]
) -> tuple[PresentedCardView, ...]:
    """The previous turn's cards, rebuilt from freshly read products.

    Positions come from `presented_ids` - the order the customer was shown -
    rather than from the hydrated sequence. A product the catalog no longer
    returns leaves its position empty instead of pulling the rest up: "the
    third one" has to keep meaning the third thing on their screen, even when
    the second has since been deactivated (CLAUDE.md 7).
    """
    by_id = {product.product_id: product for product in products}
    cards: list[PresentedCardView] = []
    for position, product_id in enumerate(presented_ids, start=1):
        product = by_id.get(product_id)
        if product is None:
            continue
        cards.append(
            PresentedCardView(
                presented_ordinal=position,
                name=product.name_english,
                commerce_category=customer_words_or_none(product.commerce.category),
                commerce_subcategory=customer_words_or_none(product.commerce.subcategory),
                price_amount=product.price_amount,
                price_unit=product.price_unit,
                seating_capacity=product.commerce.seating_capacity,
                dimensions=_dimensions(product.dimensions),
                main_color=product.main_color,
                styles=product.styles,
            )
        )
    return tuple(cards)


def comparison_view(comparison: ProductComparisonResult) -> ScreenComparisonView:
    """The comparison table, cells and all.

    Cells are copied verbatim, including the ones marked unknown: "we could not
    establish that" is an answer the customer can see, and hiding it would let
    a reply imply the field was equal.
    """
    ordinals = tuple(
        product.presented_ordinal
        for product in comparison.products
        if product.presented_ordinal is not None
    )
    if len(ordinals) != len(comparison.products):
        # A compared product with no position cannot be named by ordinal, so
        # the table has no safe handle and is better withheld than mislabelled.
        return ScreenComparisonView()
    return ScreenComparisonView(
        ordinals=ordinals,
        rows=tuple(
            ScreenComparisonRowView(
                field=row.field,
                status=row.status,
                cells=tuple(
                    ScreenComparisonCellView(known=cell.known, value=cell.value)
                    for cell in row.cells
                ),
            )
            for row in comparison.rows
        ),
    )


def room_view(room: GroundedBundlePresentation) -> ScreenRoomView:
    """The room package, exactly as its arithmetic was rendered."""
    totals = room.totals
    return ScreenRoomView(
        status=room.status,
        cards=tuple(
            ScreenRoomCardView(
                presented_ordinal=item.grounding_ref,
                name=item.name_english,
                commerce_category=customer_words_or_none(item.commerce.category),
                commerce_subcategory=customer_words_or_none(item.commerce.subcategory),
                quantity=item.quantity,
                acquisition=item.acquisition,
                locked=item.locked,
                unit_price=item.unit_price,
                price_unit=item.price_unit,
                new_spend_line_total=item.new_spend_line_total,
            )
            for item in room.items
        ),
        new_spend_total=totals.new_spend_total,
        currency=totals.currency,
        budget_max_amount=totals.budget_max_amount,
        within_budget=totals.within_budget,
    )


def screen_from_presentation(
    *,
    products: Sequence[GroundedProduct] = (),
    comparison: ProductComparisonResult | None = None,
    room: GroundedBundlePresentation | None = None,
) -> CustomerVisibleScreenView:
    """This turn's screen, from the same objects the payload was built from."""
    cards = tuple(
        card for product in products if (card := card_from_grounded(product)) is not None
    )
    return CustomerVisibleScreenView(
        products=cards,
        comparison=comparison_view(comparison) if comparison is not None else None,
        room=room_view(room) if room is not None else None,
    )
