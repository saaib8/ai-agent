"""The cart: the customer's picks with how many of each, priced fresh.

Deterministic arithmetic over verified records (CLAUDE.md 3.6): the lines come
from the picks just read from the catalog, the quantities from the session, and
the totals are summed here - never by a model, and never across currencies.

What changed is worked out from the session before and after, whatever route
changed it - a tap, a typed "add the 7th one", a seating combination or a whole
room - so the client copying the cart into the store's own sees every change
exactly once.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.core.logging import get_logger
from app.repositories.products import ProductRepository, StoreProductIdentity
from app.schemas.agent_state import AgentStateV1, ProductInteractionState
from app.schemas.cart import (
    CartChange,
    CartLineView,
    CartProductRef,
    CartTotal,
    CartView,
)
from app.schemas.language import ReplyLanguage
from app.schemas.picks import PickView
from app.schemas.retailer import RetailerContext
from app.services.click_tracking import tracking_url
from app.services.product_display import arabic_name, price_words

logger = get_logger(__name__)


class CartService:
    """Builds the cart a reply carries, from the picks it already read."""

    def __init__(self, repository: ProductRepository) -> None:
        self._repository = repository

    async def view(
        self,
        state: AgentStateV1,
        picks: Sequence[PickView],
        context: RetailerContext,
        language: ReplyLanguage,
        *,
        before: AgentStateV1 | None = None,
    ) -> CartView:
        """The cart after this turn, and what changed since `before`.

        `picks` are the session's picks as just read and numbered, so a line
        is exactly a pick the catalog still returns, with its number. With no
        `before` nothing is reported as changed: a plain read of the cart.
        """
        after = state.product_interaction
        changed = _changed_quantities(before.product_interaction, after) if before else {}
        wanted = tuple(dict.fromkeys((*after.selected_product_ids, *changed)))
        identities = {
            identity.product_id: identity
            for identity in await self._repository.store_identities(wanted, context)
        }
        lines = self._lines(after, picks, identities, language)
        totals = self._totals(lines, language)
        changes = tuple(
            _change(identities[product_id], quantities, language)
            for product_id, quantities in changed.items()
            if product_id in identities
        )
        if len(changes) != len(changed):
            # A product that left the catalog cannot be named to the store;
            # the store's own cart will refuse it at checkout regardless.
            logger.info(
                "cart_change_unnamed",
                store_id=context.store_id,
                dropped=len(changed) - len(changes),
            )
        return CartView(
            lines=lines,
            item_count=sum(line.quantity for line in lines),
            totals=totals,
            unavailable=len(after.selected_product_ids) - len(lines),
            changes=changes,
        )

    def _lines(
        self,
        interaction: ProductInteractionState,
        picks: Sequence[PickView],
        identities: dict[int, StoreProductIdentity],
        language: ReplyLanguage,
    ) -> tuple[CartLineView, ...]:
        selected = interaction.selected_product_ids
        lines: list[CartLineView] = []
        for pick in picks:
            if pick.pick > len(selected):
                continue
            product_id = selected[pick.pick - 1]
            identity = identities.get(product_id)
            if identity is None:
                continue
            quantity = interaction.quantity_of(product_id)
            line_total = pick.price_amount * quantity
            lines.append(
                CartLineView(
                    pick=pick.pick,
                    quantity=quantity,
                    name_english=pick.name_english,
                    image_url=pick.image_url,
                    unit_price=pick.price_amount,
                    price_unit=pick.price_unit,
                    line_total=line_total,
                    kind=pick.kind,
                    product=_ref(identity),
                    display=pick.display,
                    line_total_display=price_words(line_total, pick.price_unit, language),
                )
            )
        return tuple(lines)

    @staticmethod
    def _totals(lines: Sequence[CartLineView], language: ReplyLanguage) -> tuple[CartTotal, ...]:
        """One total per currency, in the order each currency first appears."""
        sums: dict[str, Decimal] = {}
        for line in lines:
            sums[line.price_unit] = sums.get(line.price_unit, Decimal(0)) + line.line_total
        return tuple(
            CartTotal(price_unit=unit, amount=amount, display=price_words(amount, unit, language))
            for unit, amount in sums.items()
        )


def cart_quantities_of(interaction: ProductInteractionState) -> dict[int, int]:
    """How many of each product the cart holds, in cart order."""
    return {
        product_id: interaction.quantity_of(product_id)
        for product_id in interaction.selected_product_ids
    }


def _changed_quantities(
    before: ProductInteractionState, after: ProductInteractionState
) -> dict[int, tuple[int, int]]:
    """Each product whose quantity moved, as (before, after): the cart's own
    order first, then what left it."""
    was = cart_quantities_of(before)
    now = cart_quantities_of(after)
    changed: dict[int, tuple[int, int]] = {}
    for product_id, quantity in now.items():
        if was.get(product_id, 0) != quantity:
            changed[product_id] = (was.get(product_id, 0), quantity)
    for product_id, quantity in was.items():
        if product_id not in now:
            changed[product_id] = (quantity, 0)
    return changed


def _ref(identity: StoreProductIdentity) -> CartProductRef:
    return CartProductRef(
        uuid=identity.uuid,
        salla_id=identity.salla_product_id,
        url=tracking_url(identity.product_url, identity.uuid),
    )


def _change(
    identity: StoreProductIdentity, quantities: tuple[int, int], language: ReplyLanguage
) -> CartChange:
    before, after = quantities
    name = (
        identity.name_english
        if language is ReplyLanguage.EN
        else arabic_name(identity.name_english, identity.name_arabic)
    )
    return CartChange(
        product=_ref(identity),
        name=name.strip() or identity.name_english.strip() or identity.uuid,
        quantity_before=before,
        quantity_after=after,
    )
