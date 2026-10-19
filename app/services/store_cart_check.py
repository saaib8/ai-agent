"""Whether the store's own cart will take what our cart is about to hold.

The agent used to say "it's in your cart" before the store had been asked, and
on a Salla store the widget then had to take back what Salla refused - a
product of another store's, one out of stock, more than one order may take
(CLAUDE.md 36). So before a product enters the cart, or its quantity rises,
Salla's storefront API is asked, and the cart holds only what the store's cart
will.

Deterministic: a store not on Salla is not asked, and a Salla that does not
answer even when asked again keeps the product out (`unconfirmed`) rather than
let our cart hold what the store's may not.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from app.core.logging import get_logger
from app.integrations.salla_storefront import SallaProduct, SallaStorefrontClient
from app.repositories.products import ProductRepository, StoreProductIdentity
from app.repositories.stores import StoreRepository
from app.schemas.cart import CartRefusalReason
from app.schemas.language import ReplyLanguage
from app.schemas.retailer import RetailerContext
from app.services.product_display import arabic_name

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CartCheck:
    """What the store's cart will take of one product."""

    product_id: int
    name_english: str
    name_arabic: str | None
    takes: int
    """How many it will take - 0 when it takes none."""

    reason: CartRefusalReason
    max_quantity: int | None = None
    product_url: str = ""

    def name(self, language: ReplyLanguage) -> str:
        if language is ReplyLanguage.EN:
            return self.name_english
        return arabic_name(self.name_english, self.name_arabic)


class StoreCartCheck:
    """Asks the store's own cart, once per product a turn wants more of."""

    def __init__(
        self,
        stores: StoreRepository,
        products: ProductRepository,
        salla: SallaStorefrontClient,
        *,
        refuse_unconfirmed: bool = True,
    ) -> None:
        self._stores = stores
        self._products = products
        self._salla = salla
        self._refuse_unconfirmed = refuse_unconfirmed

    async def refusals(
        self, wanted: Mapping[int, int], context: RetailerContext
    ) -> dict[int, CartCheck]:
        """For each product and how many they want in the cart, the ones the
        store's cart will not take as asked. Empty when every one fits, when
        the store is not on Salla, or when Salla could not be asked."""
        if not wanted:
            return {}
        salla_store_id = await self._stores.salla_store_id(context.store_id)
        if salla_store_id is None:
            return {}
        identities = {
            identity.product_id: identity
            for identity in await self._products.store_identities(tuple(wanted), context)
        }
        asked = [
            (product_id, quantity, identities[product_id])
            for product_id, quantity in wanted.items()
            if product_id in identities
        ]
        answers = await asyncio.gather(
            *(self._answer(salla_store_id, identity) for _, _, identity in asked)
        )
        refused: dict[int, CartCheck] = {}
        for (product_id, quantity, identity), answer in zip(asked, answers, strict=True):
            check = _refusal(
                identity, quantity, answer, refuse_unconfirmed=self._refuse_unconfirmed
            )
            if check is not None:
                refused[product_id] = check
        if refused:
            logger.info(
                "cart_refused_by_store",
                store_id=context.store_id,
                refused=len(refused),
                reasons=sorted({str(check.reason) for check in refused.values()}),
            )
        return refused

    async def _answer(
        self, salla_store_id: int, identity: StoreProductIdentity
    ) -> SallaProduct | None:
        if identity.salla_product_id is None:
            return SallaProduct(in_store=False, available=False, max_quantity=None)
        return await self._salla.product(salla_store_id, identity.salla_product_id)


def _refusal(
    identity: StoreProductIdentity,
    quantity: int,
    answer: SallaProduct | None,
    *,
    refuse_unconfirmed: bool,
) -> CartCheck | None:
    """Why the store's cart will not take `quantity` of it, or None when it will.

    Salla unable to tell, even after asking again, is a refusal of its own
    (`unconfirmed`) where `refuse_unconfirmed` is on: what our cart holds that
    Salla's never took is a line the widget cannot take back."""

    def check(takes: int, reason: CartRefusalReason, limit: int | None = None) -> CartCheck:
        return CartCheck(
            product_id=identity.product_id,
            name_english=identity.name_english,
            name_arabic=identity.name_arabic,
            takes=takes,
            reason=reason,
            max_quantity=limit,
            product_url=identity.product_url,
        )

    if answer is None:
        return check(0, CartRefusalReason.UNCONFIRMED) if refuse_unconfirmed else None
    if not answer.in_store:
        return check(0, CartRefusalReason.NOT_IN_STORE)
    if not answer.available:
        return check(0, CartRefusalReason.OUT_OF_STOCK)
    if answer.max_quantity is not None and quantity > answer.max_quantity:
        return check(answer.max_quantity, CartRefusalReason.OVER_LIMIT, answer.max_quantity)
    return None
