"""A guest who signs in keeps their chat, its photos and their cart.

The platform decides who may: it checks the guest's own sign-in before it asks,
so this service never judges ownership. It holds the two names to the one rule
a hand-over has - the same chat, from one shopper to another - moves the chat,
and adds the guest's cart to the account's. Deterministic, and harmless to ask
twice: a chat already moved is not moved again, and a cart merged twice is the
same cart.
"""

from __future__ import annotations

from app.core.exceptions import InvalidRequestError
from app.core.logging import get_logger
from app.repositories.conversation_moves import ConversationMoves
from app.repositories.sessions import SessionStore, shopper_of
from app.schemas.agent_state import MAX_LIKED, MAX_SEEN, MAX_TURNED_DOWN, CartQuantity
from app.schemas.retailer import RetailerContext
from app.schemas.session import ShopperCart
from app.schemas.session_adoption import ConversationAdoption, ConversationAdoptionRequest

logger = get_logger(__name__)


class ConversationAdoptionService:
    def __init__(self, sessions: SessionStore, moves: ConversationMoves, *, max_likes: int) -> None:
        self._sessions = sessions
        self._moves = moves
        self._max_likes = max_likes

    async def adopt(
        self, context: RetailerContext, request: ConversationAdoptionRequest
    ) -> ConversationAdoption:
        guest, account = request.from_session_id, request.to_session_id
        _check_hand_over(guest, account)
        outcome = await self._moves.move(context.store_id, guest, account)
        added = await self._merge_carts(context.store_id, guest, account)
        logger.info(
            "conversation_adopted",
            store_id=context.store_id,
            outcome=outcome.value,
            cart_lines_added=added,
        )
        return ConversationAdoption(outcome=outcome, cart_lines_added=added)

    async def _merge_carts(self, store_id: int, guest: str, account: str) -> int:
        """Add the guest's cart and likes to the account's, the ones every chat
        loads. The count is of cart lines only."""
        guest_cart = await self._sessions.shopper_cart(store_id, guest)
        if guest_cart is None or not (
            guest_cart.selected_product_ids
            or guest_cart.liked_product_ids
            or guest_cart.turned_down_product_ids
            or guest_cart.seen_product_ids
        ):
            return 0
        account_cart = await self._sessions.shopper_cart(store_id, account)
        merged, added = merged_cart(account_cart, guest_cart, max_likes=self._max_likes)
        if merged != account_cart:
            await self._moves.keep_cart(store_id, account, merged)
        return added


def merged_cart(
    account: ShopperCart | None, guest: ShopperCart, *, max_likes: int = MAX_LIKED
) -> tuple[ShopperCart, int]:
    """The account's cart with the guest's products after its own, and how many
    were new. A product in both keeps the larger quantity, so a cart merged
    twice never doubles a line (CLAUDE.md 36). The guest's likes follow the
    account's, each once, the newest `max_likes` kept - the most any chat
    holds."""
    base = account or ShopperCart()
    quantities = {line.product_id: line.quantity for line in base.cart_quantities}
    guest_quantities = {line.product_id: line.quantity for line in guest.cart_quantities}
    products = list(base.selected_product_ids)
    added = 0
    for product_id in guest.selected_product_ids:
        if product_id not in products:
            products.append(product_id)
            added += 1
        quantity = max(quantities.get(product_id, 1), guest_quantities.get(product_id, 1))
        if quantity > 1:
            quantities[product_id] = quantity
    lines = tuple(
        CartQuantity(product_id=product_id, quantity=quantities[product_id])
        for product_id in products
        if quantities.get(product_id, 1) > 1
    )
    return (
        ShopperCart(
            selected_product_ids=tuple(products),
            cart_quantities=lines,
            liked_product_ids=_merged_likes(
                base.liked_product_ids, guest.liked_product_ids, max_likes
            ),
            # The guest's newer: what they turned down and saw as a guest stays
            # theirs once signed in.
            turned_down_product_ids=_merged_likes(
                base.turned_down_product_ids, guest.turned_down_product_ids, MAX_TURNED_DOWN
            ),
            seen_product_ids=_merged_likes(base.seen_product_ids, guest.seen_product_ids, MAX_SEEN),
        ),
        added,
    )


def _merged_likes(
    account: tuple[int, ...] | None, guest: tuple[int, ...] | None, max_likes: int
) -> tuple[int, ...] | None:
    if account is None and guest is None:
        return None
    liked = tuple(dict.fromkeys((*(account or ()), *(guest or ()))))
    return liked[-max_likes:]


def _check_hand_over(guest: str, account: str) -> None:
    """The same chat, named for two different shoppers - nothing else moves."""
    guest_shopper, account_shopper = shopper_of(guest), shopper_of(account)
    same_chat = guest.partition(".")[2] == account.partition(".")[2]
    if (
        guest_shopper is None
        or account_shopper is None
        or guest_shopper == account_shopper
        or not same_chat
    ):
        raise InvalidRequestError(
            public_message="Only the same chat can move from a guest to an account.",
            reason="not_a_hand_over",
        )
