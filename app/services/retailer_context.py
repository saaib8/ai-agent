"""Resolves the catalog scope for a request.

This is the only way a :class:`RetailerContext` is created in application code.
A store id that does not resolve to an active store never becomes a context, so
downstream repositories cannot be handed a scope that was never checked.

Note (CLAUDE.md 20.7): store scoping prevents accidental and model-driven
cross-store access. V1 has no authentication or authorization, and this is not
a substitute for either.
"""

from __future__ import annotations

from app.core.exceptions import StoreNotFoundError
from app.repositories.stores import StoreRepository
from app.schemas.retailer import RetailerContext


class RetailerContextProvider:
    def __init__(self, stores: StoreRepository) -> None:
        self._stores = stores

    async def resolve(self, store_id: int) -> RetailerContext:
        """Build the request's scope, or refuse.

        Raises :class:`StoreNotFoundError` for an unknown *or* inactive store —
        the caller is told the same thing either way, so the response does not
        reveal which stores exist.
        """
        store = await self._stores.get_active(store_id)
        if store is None:
            raise StoreNotFoundError(requested_store_id=store_id)
        return RetailerContext(store_id=store.id)
