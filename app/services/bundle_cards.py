"""The room as the customer sees it grouped, derived from state alone.

One definition of what a visible card *is*, because two layers need it and they
must agree. A room is rendered once from an optimisation and again, later, from
durable state - and "the second one" has to mean the same piece both times. If
each layer grouped and ordered for itself, a reference could resolve to a
product the customer never saw in that position.

Derivable from `bundle_items` and nothing else: no price, no name, no category,
no catalog read. That is what lets the same ordering be reconstructed on a
later turn, when the outcome that produced it is long gone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_state import BundleItemState, BundleItemStatus


@dataclass(frozen=True, slots=True)
class BundleCardGroup:
    """One rendered card, and the state lines behind it.

    **Application-only.** It carries a product id and line ids because the
    application resolves references with them; neither reaches a model.

    A card may span several design needs - one chair filling two roles is still
    one card - which is why `need_ids` is a tuple. What to do about that when
    replacing a product is E4C's problem; grouping does not pre-judge it.
    """

    product_id: int
    line_ids: tuple[int, ...]
    need_ids: tuple[int, ...]
    quantity: int
    acquisition: BundleAcquisition
    locked: bool


def group_bundle_cards(
    lines: Sequence[BundleItemState],
) -> tuple[BundleCardGroup, ...]:
    """Cards in the order the customer sees them.

    Grouped by product, acquisition and lock status - the three things a card
    states about a piece. Two lines that differ on any of them are two
    different things to say, however identical the product: one being kept and
    one merely suggested is not one card, and neither is one owned and one
    being bought.

    Ordered by the **first contributing line**, which is durable and needs no
    catalog fact. Sorting by name, price or category would need a product read
    and would reorder the room whenever the catalog changed.

    `need_ids` keeps distinct non-null needs in first-seen order; a card
    belonging to no need contributes none.
    """
    order: list[tuple[int, BundleAcquisition, bool]] = []
    grouped: dict[tuple[int, BundleAcquisition, bool], list[BundleItemState]] = {}

    for line in lines:
        key = (line.product_id, line.acquisition, line.status is BundleItemStatus.LOCKED)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(line)

    return tuple(
        BundleCardGroup(
            product_id=key[0],
            line_ids=tuple(line.line_id for line in members),
            need_ids=tuple(
                dict.fromkeys(
                    line.need_id for line in members if line.need_id is not None
                )
            ),
            quantity=sum(line.quantity for line in members),
            acquisition=key[1],
            locked=key[2],
        )
        for key in order
        if (members := grouped[key])
    )
