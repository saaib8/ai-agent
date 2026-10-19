"""The shopper's cart, carried into every conversation they have in a store.

The cart belongs to the shopper, not to one chat (CLAUDE.md 36). A conversation
that starts - the widget's "New chat", or one whose session expired - starts
with it, and one that resumes picks up what another chat changed, so "what's in
my cart?" answers the same wherever they ask. The session store keeps the cart
beside the conversations (`SessionStore.shopper_cart`); this puts it into the
state of the one being loaded.

Deterministic and never fatal: a cart that cannot be applied leaves the
conversation exactly as it was stored.
"""

from __future__ import annotations

from pydantic import ValidationError

from app.core.logging import get_logger
from app.schemas.agent_state import AgentStateV1, ProductInteractionState
from app.schemas.session import ShopperCart

logger = get_logger(__name__)


def with_shopper_cart(state: AgentStateV1, cart: object, *, new_conversation: bool) -> AgentStateV1:
    """`state` holding the shopper's cart in place of its own.

    Only the cart - its products and how many of each - is the shopper's;
    everything else stays. A product in focus that the new cart and the screen
    no longer hold loses the focus, as removing it would have done.
    """
    if not isinstance(cart, ShopperCart):
        return state
    interaction = state.product_interaction
    if (
        cart.selected_product_ids == interaction.selected_product_ids
        and cart.cart_quantities == interaction.cart_quantities
    ):
        return state
    updated = _with_cart(interaction, cart)
    if updated is None:
        logger.warning("shopper_cart_not_applied", lines=len(cart.selected_product_ids))
        return state
    try:
        applied = AgentStateV1.model_validate(
            {**state.model_dump(), "product_interaction": updated.model_dump()}
        )
    except ValidationError:
        logger.warning("shopper_cart_not_applied", lines=len(cart.selected_product_ids))
        return state
    logger.info(
        "shopper_cart_applied",
        lines=len(cart.selected_product_ids),
        new_conversation=new_conversation,
    )
    return applied


def _with_cart(
    interaction: ProductInteractionState, cart: ShopperCart
) -> ProductInteractionState | None:
    fields = {
        **interaction.model_dump(),
        "selected_product_ids": cart.selected_product_ids,
        "cart_quantities": [line.model_dump() for line in cart.cart_quantities],
    }
    try:
        return ProductInteractionState.model_validate(fields)
    except ValidationError:
        pass
    # The product in focus may have been the one another chat removed.
    try:
        return ProductInteractionState.model_validate({**fields, "focused_product_id": None})
    except ValidationError:
        return None
