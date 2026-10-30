"""The customer's cart and liked list, changed the one way every agent does.

The picks are the cart. Adding goes through the same rules however it is
asked for: at most `max_picks` products, at most `max_quantity` of one, and -
on a store with its own cart - only what that cart would take. A refusal is
reported, never worked around.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.core.exceptions import IntegrationUnavailableError
from app.core.logging import get_logger
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import ProductInteractionOp
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_updates import (
    AddItems,
    AgentStateUpdate,
    ProductInteractionUpdate,
    RemoveItems,
    SetCartQuantity,
)
from app.schemas.retailer import RetailerContext
from app.services.agent_state import apply_update
from app.services.product_interaction import interaction_update
from app.services.store_cart_check import StoreCartCheck

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CartRefused:
    product_id: int
    reason: str
    kept: int


@dataclass(slots=True)
class CartChange:
    state: AgentStateV1
    added: list[int] = field(default_factory=list)
    already: list[int] = field(default_factory=list)
    refused: list[CartRefused] = field(default_factory=list)
    full: bool = False


class CartOperations:
    def __init__(
        self,
        *,
        max_picks: int,
        max_quantity: int,
        max_likes: int,
        check: StoreCartCheck | None,
        refuse_unconfirmed: bool = True,
    ) -> None:
        self._refuse_unconfirmed = refuse_unconfirmed
        self._max_picks = max_picks
        self.max_quantity = max_quantity
        self._max_quantity = max_quantity
        self._max_likes = max_likes
        self._check = check

    async def add(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        items: Sequence[tuple[int, int | None]],
        *,
        whole: bool = False,
    ) -> CartChange:
        """Each product, with how many, in the order asked; the ones that fit go
        in, and what could not is said. `whole` is a room or a seating
        combination: it goes in whole, even past `max_picks` - half a room is
        not a choice they made - and the store's own cart is still asked."""
        change = CartChange(state=state)
        for product_id, quantity in items:
            interaction = change.state.product_interaction
            held = product_id in interaction.selected_product_ids
            had = interaction.quantity_of(product_id) if held else 0
            if not whole and not held and len(interaction.selected_product_ids) >= self._max_picks:
                change.full = True
                break
            wants = min(quantity if quantity is not None else max(had, 1), self._max_quantity)
            if held and wants == had:
                change.already.append(product_id)
                continue
            if wants > had:
                taken = await self._store_takes(product_id, wants, had, context, change)
                if taken is None:
                    continue
                wants = taken
            change.state = self._select(change.state, product_id, wants)
            change.added.append(product_id)
        return change

    def remove(self, state: AgentStateV1, product_id: int) -> AgentStateV1:
        update = interaction_update(ProductInteractionOp.DESELECT, product_id, state)
        return apply_update(state, AgentStateUpdate(product_interaction=update))

    def like(self, state: AgentStateV1, product_id: int, *, liked: bool) -> AgentStateV1:
        ids = state.product_interaction.liked_product_ids
        if liked and product_id not in ids and len(ids) >= self._max_likes:
            # The oldest like goes: a like is a signal, not a commitment.
            state = apply_update(
                state,
                AgentStateUpdate(
                    product_interaction=ProductInteractionUpdate(
                        liked_product_ids=RemoveItems(items=(ids[0],))
                    )
                ),
            )
        change = AddItems(items=(product_id,)) if liked else RemoveItems(items=(product_id,))
        return apply_update(
            state,
            AgentStateUpdate(
                product_interaction=ProductInteractionUpdate(liked_product_ids=change)
            ),
        )

    def _select(self, state: AgentStateV1, product_id: int, quantity: int) -> AgentStateV1:
        update = interaction_update(ProductInteractionOp.SELECT, product_id, state)
        update = update.model_copy(
            update={"cart_quantities": (SetCartQuantity(product_id=product_id, quantity=quantity),)}
        )
        return apply_update(state, AgentStateUpdate(product_interaction=update))

    async def _store_takes(
        self,
        product_id: int,
        wants: int,
        had: int,
        context: RetailerContext,
        change: CartChange,
    ) -> int | None:
        """How many the store's own cart takes, or None for none at all."""
        if self._check is None:
            return wants
        try:
            refused = (await self._check.refusals({product_id: wants}, context)).get(product_id)
        except IntegrationUnavailableError:
            logger.warning("cart_check_unavailable", store_id=context.store_id)
            if not self._refuse_unconfirmed:
                return wants
            # The store's own cart could not be asked: never promised, since
            # the widget cannot take back what that cart then refuses.
            change.refused.append(CartRefused(product_id, "unconfirmed", had))
            return None
        if refused is None:
            return wants
        keep = max(refused.takes, had)
        change.refused.append(CartRefused(product_id, str(refused.reason), keep))
        return keep if keep > had else None


def room_pieces_to_buy(state: AgentStateV1) -> list[tuple[int, int]]:
    """The room's pieces they are buying, with how many - a piece they own is
    not bought again, and a product already in the cart keeps the larger of
    its two quantities, so adding the room twice never doubles it."""
    room = state.room_project
    if room is None:
        return []
    wanted: dict[int, int] = {}
    for line in room.bundle_items:
        if line.acquisition is BundleAcquisition.TO_BUY:
            wanted[line.product_id] = wanted.get(line.product_id, 0) + line.quantity
    interaction = state.product_interaction
    return [
        (
            product_id,
            max(
                quantity,
                interaction.quantity_of(product_id)
                if product_id in interaction.selected_product_ids
                else 0,
            ),
        )
        for product_id, quantity in wanted.items()
    ]


def as_room_in_cart(before: AgentStateV1, after: AgentStateV1) -> AgentStateV1:
    """From now on the cart is the room: what they pick next is for it."""
    room = after.room_project
    if room is None or room.in_cart:
        return after
    pieces = {line.product_id for line in room.bundle_items}
    return after.model_copy(
        update={
            "room_project": room.model_copy(
                update={
                    "in_cart": True,
                    "picks_before_cart": tuple(
                        p
                        for p in before.product_interaction.selected_product_ids
                        if p not in pieces
                    ),
                }
            )
        }
    )
