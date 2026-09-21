"""Turning a ranked list of ids back into products.

PostgreSQL is the source of product truth; the index supplied an order and
nothing else. So every customer-visible value is re-read here, and a ranked id
the database no longer returns is dropped rather than served from whatever the
index still remembers about it (CLAUDE.md 16).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.logging import get_logger
from app.repositories.products import ProductRepository
from app.schemas.product import ProductCandidate
from app.schemas.retailer import RetailerContext
from app.schemas.semantic import SemanticRankingResult
from app.services.discovery import to_candidate

logger = get_logger(__name__)


class ProductHydrationService:
    """Loads ranked products from PostgreSQL, in the ranked order."""

    def __init__(self, repository: ProductRepository) -> None:
        self._repository = repository

    async def hydrate(
        self, ranking: SemanticRankingResult, context: RetailerContext
    ) -> tuple[ProductCandidate, ...]:
        """Fresh product records for a whole ranking, in the order it produced."""
        return await self.hydrate_ids(ranking.product_ids, context)

    async def hydrate_ids(
        self, product_ids: Sequence[int], context: RetailerContext
    ) -> tuple[ProductCandidate, ...]:
        """Fresh product records for these ids, in the order asked for.

        The conversational path selects the few products it will show and
        hydrates only those, so this takes ids rather than a ranking. One
        implementation either way, so there is a single place a stale row is
        dropped and a single place scope is applied.

        Scope is applied again on the way out: the repository only returns
        products this store owns, so an id belonging to another retailer
        resolves to nothing rather than to a product.

        A missing row is dropped, never substituted and never backfilled from
        further down the ranking - the customer is shown what still exists,
        and the ids recorded are the ids rendered.
        """
        ordered_ids = tuple(product_ids)
        if not ordered_ids:
            return ()

        rows = await self._repository.get_by_ids(list(ordered_ids), context)
        by_id = {row.id: row for row in rows}

        stale = [i for i in ordered_ids if i not in by_id]
        if stale:
            # A vector outlived its product, or the product stopped being
            # eligible. Either way the catalog is right and the index is
            # behind, so the customer sees neither it nor a stale copy of it.
            logger.warning(
                "ranked_products_dropped_as_stale",
                store_id=context.store_id,
                dropped_count=len(stale),
            )
        return tuple(to_candidate(by_id[i]) for i in ordered_ids if i in by_id)
