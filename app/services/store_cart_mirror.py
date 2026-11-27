"""The store's own cart, mirrored into ours before a turn starts.

Our cart (the picks) is what the agents read and talk about. On a Salla store
the widget copies every change we make into Salla's cart - but the shopper can
also change Salla's cart on Salla's own cart page: remove a rug, change a
quantity, add something themselves. None of that ever reached us, so the agent
went on saying "your cart has 5 carpets" after the shopper had removed them.

Only the storefront page can read Salla's cart, so the widget reads it and
sends it with each message (`ChatRequest.store_cart`). Before the agents start,
our cart is made to match it exactly (CLAUDE.md 36):

* a product no longer in the store's cart leaves ours;
* every product's quantity becomes the store's;
* a product the shopper added on the store's own page joins ours, after the
  lines we already had - when our catalog knows it by its Salla id.

The liked list, the screen and everything else stay as they were.

It runs before the turn's "before" picture is taken, so none of it is reported
in the reply's `cart.changes`: the widget must never copy back into Salla what
came from Salla.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.core.logging import get_logger
from app.repositories.products import ProductRepository
from app.repositories.stores import StoreRepository
from app.schemas.agent_state import (
    MAX_CART_QUANTITY,
    AgentStateV1,
    CartQuantity,
    ProductInteractionState,
)
from app.schemas.cart import StoreCartLine
from app.schemas.retailer import RetailerContext
from app.schemas.session import ShopperCart
from app.services.shopper_cart import with_shopper_cart

logger = get_logger(__name__)


class StoreCartMirror:
    """Makes our cart match the store's own, as the widget reported it."""

    def __init__(self, stores: StoreRepository, products: ProductRepository) -> None:
        self._stores = stores
        self._products = products

    async def mirrored(
        self, state: AgentStateV1, lines: Sequence[StoreCartLine], context: RetailerContext
    ) -> AgentStateV1:
        """`state` with its cart made to match `lines`, the store's cart.

        On Salla, lines name products by their Salla id. On another store
        platform (OpenCart, ...) by their platform id ("opencart-50"). A store
        whose products carry neither is left unchanged: nothing could match,
        and the cart would be emptied by mistake.
        """
        on_salla = await self._stores.salla_store_id(context.store_id) is not None
        if on_salla:
            # 1. How many of each Salla product the store's cart holds. One
            #    product may come as several lines (one per option chosen).
            by_store_id = quantities_by_salla_id(lines)
            # 2. Which of our products each Salla id is. A product our catalog
            #    does not know cannot be shown or priced, so it is left out.
            ours = await self._products.ids_for_salla_products(tuple(by_store_id), context)
        else:
            platform_lines = [line for line in lines if line.external_id]
            if len(platform_lines) != len(lines) or (
                not lines and not await self._products.sells_through_platform(context)
            ):
                logger.info(
                    "store_cart_ignored", store_id=context.store_id, reason="no_platform_ids"
                )
                return state
            by_store_id = quantities_by_external_id(platform_lines)
            ours = await self._products.ids_for_external_products(tuple(by_store_id), context)
        quantities: dict[int, int] = {}
        for store_id, quantity in by_store_id.items():
            product_id = ours.get(store_id)
            if product_id is not None:
                quantities[product_id] = quantities.get(product_id, 0) + quantity

        # 3. Our cart, rebuilt to hold exactly those.
        cart = mirrored_cart(state.product_interaction, quantities)
        logger.info(
            "store_cart_mirrored",
            store_id=context.store_id,
            store_lines=len(by_store_id),
            unknown_to_catalog=len(by_store_id) - len(ours),
            lines_before=len(state.product_interaction.selected_product_ids),
            lines_after=len(cart.selected_product_ids),
        )
        return with_shopper_cart(state, cart, new_conversation=False)


def quantities_by_salla_id(lines: Sequence[StoreCartLine]) -> dict[str, int]:
    """How many of each Salla product, keyed by its Salla id as our catalog
    stores it (a string of digits), in the order the store's cart lists them.
    A line without a Salla id is not a Salla product: left out."""
    totals: dict[str, int] = {}
    for line in lines:
        if line.salla_id is None:
            continue
        key = str(line.salla_id)
        totals[key] = totals.get(key, 0) + line.quantity
    return totals


def quantities_by_external_id(lines: Sequence[StoreCartLine]) -> dict[str, int]:
    """How many of each platform product ("opencart-50"), in the order the
    store's cart lists them; several lines of one product add up."""
    totals: dict[str, int] = {}
    for line in lines:
        if not line.external_id:
            continue
        totals[line.external_id] = totals.get(line.external_id, 0) + line.quantity
    return totals


def mirrored_cart(
    interaction: ProductInteractionState, quantities: Mapping[int, int]
) -> ShopperCart:
    """Our cart holding exactly `quantities` - our product id to how many.

    The order keeps the cart's numbering steady ("the second one in my cart"):
    the products we already had stay in their places, and the ones the shopper
    added on the store's page follow, in the store's order. A quantity is
    stored only where it is more than one, as the cart always does, and never
    above what our cart can hold.
    """
    kept = [
        product_id for product_id in interaction.selected_product_ids if product_id in quantities
    ]
    already = set(kept)
    added = [product_id for product_id in quantities if product_id not in already]
    order = (*kept, *added)
    return ShopperCart(
        selected_product_ids=order,
        cart_quantities=tuple(
            CartQuantity(
                product_id=product_id, quantity=min(quantities[product_id], MAX_CART_QUANTITY)
            )
            for product_id in order
            if quantities[product_id] > 1
        ),
        # None keeps this conversation's liked list exactly as it is.
        liked_product_ids=None,
    )
