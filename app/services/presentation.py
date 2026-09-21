"""Choosing which ranked products the customer actually sees.

A pure function, and deliberately the whole of this module. Selection is the
*last* thing that happens to a result set, and keeping it separate from every
service that produces one is what stops it being mistaken for a bound on any
of them.

The distinction it exists to protect (CLAUDE.md 16.1):

    eligibility   M6/M8 decide which products match. No presentation bound.
    ranking       M9 orders the COMPLETE eligible pool. No candidate limit.
    presentation  this module takes the first few. Display only.

A presentation limit applied any earlier would choose the customer's options
before anything had judged them - reranking the first fifty of a hundred and
seventy-three leaves the best match unreachable. So nothing here is imported by
M6, M8, M9 or the repository, and a guard test proves it.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.exceptions import InvalidRequestError


def select_for_presentation(
    ranked_product_ids: Sequence[int], *, limit: int
) -> tuple[int, ...]:
    """The first `limit` products of an already-ranked list, in ranked order.

    Takes ids rather than a ranking result: there is nothing to decide beyond
    "how many", and a function that cannot see similarity scores or relaxation
    depths cannot start ordering by them.

    Order is the caller's; this never re-sorts. Fewer ranked products than the
    limit is an ordinary outcome, not a shortfall.
    """
    if limit < 1:
        raise InvalidRequestError(
            reason="a presentation limit must allow at least one product"
        )
    return tuple(ranked_product_ids[:limit])
