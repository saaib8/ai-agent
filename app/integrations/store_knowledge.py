"""The store-documents index boundary. Read-only.

The main platform writes each store's passages into that store's own
namespace; this port only searches them. It never chooses a store - the
namespace and the `store_id` filter both come from RetailerContext - and it is
never the source of a product fact (CLAUDE.md 16, 20.2).

Scope is applied twice, as for products: a namespace mistake alone cannot
reach another store's documents.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from app.core.config import StoreKnowledgeSettings
from app.core.exceptions import ConfigurationError, StoreKnowledgeUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class KnowledgeIndex(Protocol):
    async def search(
        self,
        *,
        vector: tuple[float, ...],
        namespace: str,
        store_id: int,
        top_k: int,
    ) -> list[tuple[float, dict[str, Any]]]: ...


class PineconeKnowledgeIndex:
    """Adapter over the Pinecone client. One index handle per process."""

    def __init__(self, settings: StoreKnowledgeSettings) -> None:
        try:
            from pinecone import Pinecone  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:
            raise ConfigurationError(
                detail=(
                    "store_knowledge settings are configured but the 'semantic' "
                    "extra is not installed; install it or unset ZORY_STORE_KNOWLEDGE__*"
                ),
                public_message="Store knowledge is not installed.",
            ) from exc
        self._index = Pinecone(api_key=settings.api_key.get_secret_value()).Index(
            settings.index_name
        )
        self._index_name = settings.index_name

    async def search(
        self,
        *,
        vector: tuple[float, ...],
        namespace: str,
        store_id: int,
        top_k: int,
    ) -> list[tuple[float, dict[str, Any]]]:
        """(score, metadata) per hit, best first, from this store only."""
        try:
            response = await asyncio.to_thread(
                self._index.query,
                vector=list(vector),
                top_k=top_k,
                namespace=namespace,
                filter={"store_id": {"$eq": store_id}},
                include_metadata=True,
                include_values=False,
            )
        except Exception as exc:
            logger.warning(
                "store_knowledge_query_failed",
                index=self._index_name,
                error_type=type(exc).__name__,
            )
            raise StoreKnowledgeUnavailableError(reason=type(exc).__name__) from exc
        return _read(response)


def _read(response: Any) -> list[tuple[float, dict[str, Any]]]:
    matches = response["matches"] if isinstance(response, dict) else response.matches
    hits: list[tuple[float, dict[str, Any]]] = []
    for match in matches:
        score = match["score"] if isinstance(match, dict) else match.score
        metadata = match.get("metadata") if isinstance(match, dict) else match.metadata
        hits.append((float(score), dict(metadata or {})))
    return hits


__all__ = ["KnowledgeIndex", "PineconeKnowledgeIndex"]
