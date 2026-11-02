"""The look of what the customer liked, as a lean for their next search.

A like is a quiet taste signal (CLAUDE.md 10.2): the colours and styles of the
products they liked order a search they ask for - after everything they said,
before a designer's direction - and never filter it. Read from the liked
products' stored values, so a like leans only to an approved colour or style
the catalog records. Worked out when a search starts and kept by its pages:
an unliked product stops leaning from the next search.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.schemas.discovery import ProductSearchRequest
from app.schemas.product import ProductCandidate
from app.schemas.query import RankingLean


def with_liked_look(
    lean: RankingLean | None,
    liked: Sequence[ProductCandidate],
    request: ProductSearchRequest,
    related: tuple[str, ...] = (),
    *,
    avoid_colours: tuple[str, ...] = (),
    avoid_styles: tuple[str, ...] = (),
) -> RankingLean:
    """`lean` leaning to the liked products' look, worked out for this kind.

    A colour leans only from liked pieces of the kind searched and the kinds
    `related` to it - reviewed to be shown together, a sofa set beside sofas;
    a beige sofa says nothing about the lamp they want - a style from any
    liked piece. A colour or style they said to avoid is never leaned to,
    whatever they liked before.
    Marked `learned` even when nothing liked is of these kinds, so its pages
    keep exactly what this search was worked out with.
    """
    kind = request.commerce_subcategory
    if kind:
        kinds = {kind, *related}
        same_kind = [p for p in liked if p.commerce.subcategory in kinds]
    else:
        same_kind = [p for p in liked if p.commerce.category == request.commerce_category]
    colours = tuple(
        dict.fromkeys(
            p.main_color for p in same_kind if p.main_color and p.main_color not in avoid_colours
        )
    )
    styles = tuple(dict.fromkeys(s for p in liked for s in p.styles if s not in avoid_styles))
    return (lean or RankingLean()).model_copy(
        update={
            "learned": True,
            "learned_for": kind or request.commerce_category,
            "learned_colours": colours,
            "learned_styles": styles,
        }
    )
