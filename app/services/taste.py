"""Taste learned quietly from what the customer liked, picked and asked more
like of (docs/designer-led-shopping-plan.md, 5.1).

Worked out when a search starts, from the products' current catalog colour
and style - never stored as numbers, so un-liking removes it and a product
that leaves the catalog drops out. A colour leans only searches of the kind it
was seen on: a beige sofa says nothing about the rug, which is the designer's
call. A style carries to any kind.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from app.schemas.product import ProductCandidate

MAX_LEARNED = 2
"""The strongest one or two values per family - a lean, not a profile."""


def learned_taste(
    products: Sequence[ProductCandidate], commerce_subcategory: str | None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The colours (from pieces of this kind) and styles (from any) they lean
    towards, most often first; the newest wins a tie."""
    newest_first = list(reversed(products))
    colours = Counter(
        product.main_color
        for product in newest_first
        if product.main_color and product.commerce.subcategory == commerce_subcategory
    )
    styles = Counter(style for product in newest_first for style in product.styles)
    return (
        tuple(colour for colour, _ in colours.most_common(MAX_LEARNED)),
        tuple(style for style, _ in styles.most_common(MAX_LEARNED)),
    )
