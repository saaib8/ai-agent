"""What a shopper turned down and was shown, and the record of it.

The lists themselves live in the conversation's state
(`ProductInteractionState.turned_down_product_ids`, `seen_product_ids`), changed
only through the reducers (`turn_down`, `take_back`, `commit_search_results`),
and travel with the shopper's cart from chat to chat (`ShopperCart`): capped and
expiring, so a shopper's memory never grows however long they browse.

Every change is also logged as one structured event - which products, what
happened, in which store - so a durable event store can later be filled from
the same calls without touching an agent or a tool (docs/shopper-memory.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from app.core.logging import get_logger
from app.schemas.retailer import RetailerContext

logger = get_logger(__name__)

ShopperEvent = Literal["seen", "turned_down", "taken_back"]


def log_shopper_event(
    event: ShopperEvent, context: RetailerContext, product_ids: Sequence[int]
) -> None:
    """One structured line per change: the products and what happened to them.
    Product references only - no names, prices or customer words."""
    if not product_ids:
        return
    logger.info(
        "shopper_product_event",
        shopper_event=event,
        store_id=context.store_id,
        product_ids=list(product_ids),
    )
