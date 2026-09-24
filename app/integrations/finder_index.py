"""The product index Furniture Finder searches.

Built outside this service from a **text** document per product - name,
category, style, colour - embedded into one namespace. Each vector carries
`store_id`, `category` and `product_url` metadata; that is the whole contract
this adapter relies on.

This port *proposes* neighbours. It never decides what is sellable: every
match is resolved through the store-scoped repository, and whatever
PostgreSQL does not return is not shown (CLAUDE.md 16).

Scope is applied here as well, as a metadata filter on `store_id` taken from
`RetailerContext` - a second barrier, so one mistake cannot surface another
retailer's products even as candidates.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.config import FurnitureFinderSettings
from app.core.exceptions import ConfigurationError, VisualSearchUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IndexMatch:
    """One neighbour, as the index knows it.

    `product_url` is how a match is joined to the catalog when the index was
    built from a copy of it whose ids differ from this database's.
    """

    vector_id: str
    score: float
    product_url: str | None


class FinderIndex(Protocol):
    async def nearest(
        self,
        *,
        vector: tuple[float, ...],
        store_id: int,
        category: str,
        top_k: int,
    ) -> tuple[IndexMatch, ...]: ...


def category_key(label: str) -> str:
    """A detector label as the index's `category` metadata spells it."""
    return label.strip().lower().replace(" ", "-")


class PineconeFinderIndex:
    """Adapter over the Pinecone client. One index handle per process."""

    def __init__(self, settings: FurnitureFinderSettings) -> None:
        try:
            from pinecone import Pinecone  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:
            raise ConfigurationError(
                detail=(
                    "furniture_finder settings are configured but the 'semantic' extra "
                    "(the Pinecone SDK) is not installed"
                ),
                public_message="Furniture Finder is not installed.",
            ) from exc
        self._index: Any = Pinecone(api_key=settings.index_api_key.get_secret_value()).Index(
            settings.index_name
        )
        self._index_name = settings.index_name
        self._namespace = settings.index_namespace

    async def nearest(
        self,
        *,
        vector: tuple[float, ...],
        store_id: int,
        category: str,
        top_k: int,
    ) -> tuple[IndexMatch, ...]:
        try:
            response = await asyncio.to_thread(
                self._index.query,
                vector=list(vector),
                top_k=top_k,
                namespace=self._namespace,
                filter={"store_id": {"$eq": store_id}, "category": {"$eq": category}},
                include_metadata=True,
                include_values=False,
            )
        except Exception as exc:
            logger.warning(
                "finder_index_query_failed",
                index=self._index_name,
                error_type=type(exc).__name__,
            )
            raise VisualSearchUnavailableError(reason=type(exc).__name__) from exc
        return read_matches(response)


def read_matches(response: Any) -> tuple[IndexMatch, ...]:
    """Matches in the index's order, best first."""
    matches = response["matches"] if isinstance(response, dict) else response.matches
    read: list[IndexMatch] = []
    for match in matches or ():
        identifier = match["id"] if isinstance(match, dict) else match.id
        score = match["score"] if isinstance(match, dict) else match.score
        metadata = (match.get("metadata") if isinstance(match, dict) else match.metadata) or {}
        url = metadata.get("product_url")
        read.append(
            IndexMatch(
                vector_id=str(identifier),
                score=float(score or 0.0),
                product_url=url if isinstance(url, str) and url else None,
            )
        )
    return tuple(read)
