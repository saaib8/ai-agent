"""The rules a design decision must keep, whoever wrote it.

Nora makes the design decisions and writes them as tool arguments; these are
the deterministic checks every one of them passes, moved here from the former
design specialist so a judgement is held to exactly the rules it always was
(CLAUDE.md 3.3, 14.3, 20.6):

* a direction names only colours and styles the store stocks for that kind -
  a value it does not come in is dropped, never matched to something near it;
* a product type is an approved pair;
* a size for the next piece is usable and of an approved kind;
* a piece shown beside an anchor is not the anchor's own kind;
* prose states no figure - every number is a fact's, written by code.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from app.core.exceptions import TaxonomyValidationError
from app.schemas.design import DesignDirection, NextSize, StockedLook
from app.taxonomy.registry import CommerceTaxonomy

_DIGITS = re.compile(r"\d")


def stocked_direction(
    direction: DesignDirection, look: StockedLook | None
) -> tuple[DesignDirection | None, int]:
    """The direction kept to what the shop stocks for the kind, and how many
    values were dropped. None when nothing stocked is left."""
    colours = look.colours if look else ()
    styles = look.styles if look else ()
    kept = DesignDirection(
        colours=_stocked(direction.colours, colours),
        styles=_stocked(direction.styles, styles),
        avoid_colours=_stocked(direction.avoid_colours, colours),
        avoid_styles=_stocked(direction.avoid_styles, styles),
        size_ratio=direction.size_ratio,
        fits_inside_cm=direction.fits_inside_cm,
    )
    dropped = sum(
        len(asked) - len(found)
        for asked, found in (
            (direction.colours, kept.colours),
            (direction.styles, kept.styles),
            (direction.avoid_colours, kept.avoid_colours),
            (direction.avoid_styles, kept.avoid_styles),
        )
    )
    return (None if kept.is_empty else kept), dropped


def stocked_values(values: Sequence[str], stocked: Sequence[str]) -> tuple[str, ...]:
    """The values the stock holds, in its own spelling - case, spaces, "_" and
    "-" normalised, nothing matched by likeness."""
    return _stocked(values, stocked)


def approved_pair(taxonomy: CommerceTaxonomy, category: str, subcategory: str | None) -> bool:
    try:
        if subcategory is None:
            taxonomy.subcategories(category)
        else:
            taxonomy.validate_pair(category, subcategory)
    except TaxonomyValidationError:
        return False
    return True


def usable_next_sizes(
    sizes: Iterable[NextSize], taxonomy: CommerceTaxonomy
) -> tuple[NextSize, ...]:
    """Sizes that name a longer side, keep each range in order, state no figure
    in their reason and are of an approved kind."""
    return tuple(
        size
        for size in sizes
        if size.is_usable()
        and approved_pair(taxonomy, size.commerce_category, size.commerce_subcategory)
    )


def not_the_anchors_kind(subcategory: str | None, anchors: Iterable[str | None]) -> bool:
    """Whether a kind shown beside the anchors is another kind than theirs."""
    return subcategory is None or subcategory not in set(anchors)


def figure_free(*texts: str | None) -> bool:
    """Whether none of the texts states a figure (Arabic-Indic digits too)."""
    return not any(_DIGITS.search(text or "") for text in texts)


def _stocked(values: Sequence[str], stocked: Sequence[str]) -> tuple[str, ...]:
    by_spelling = {_spelling(value): value for value in stocked}
    return tuple(
        dict.fromkeys(
            by_spelling[_spelling(value)] for value in values if _spelling(value) in by_spelling
        )
    )


def _spelling(value: str) -> str:
    return "".join(value.casefold().replace("_", " ").replace("-", " ").split())
