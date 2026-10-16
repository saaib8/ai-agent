"""The cart: what the customer added, how many, and what it comes to.

Transport only (CLAUDE.md 3.2). Reading changes nothing. Changing the cart goes
through `POST /v1/picks` (select, deselect, quantity) or a turn of the
conversation, and every one of those replies carries the cart too.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import PicksRuntimeDep, RetailerContextProviderDep
from app.schemas.cart import CartQuery, CartResponse

router = APIRouter(tags=["cart"])


@router.get("/cart", response_model=CartResponse)
async def read_cart(
    query: Annotated[CartQuery, Query()],
    runtime: PicksRuntimeDep,
    retailers: RetailerContextProviderDep,
) -> CartResponse:
    """The cart for one conversation, priced fresh."""
    context = await retailers.resolve(query.store_id)
    return await runtime.read_cart(query, context)
