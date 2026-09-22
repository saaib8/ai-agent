"""The semantic index boundary.

A narrow port: score a set of already-eligible product ids against one query
vector. It never decides which products exist, never chooses a store, and is
never the source of product truth (CLAUDE.md 16).

Scope is applied twice on purpose - the namespace comes from RetailerContext,
and `store_id` is filtered in metadata as well. Two independent barriers, so a
namespace mistake alone cannot leak another retailer's catalog.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from app.core.config import PineconeSettings
from app.core.exceptions import ConfigurationError, SemanticIndexUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

VECTOR_ID_PREFIX = "product:"
MAX_FILTER_IDS = 1000
"""Ids per `$in` filter. M9B-1 verified 1,036 in one request against the live
index, so this is transport hygiene for larger catalogs rather than a measured
ceiling. Chunking splits the transport only: every chunk is scored with the
same query vector, and no candidate is dropped."""


def vector_id(product_id: int) -> str:
    return f"{VECTOR_ID_PREFIX}{product_id}"


def product_id_of(vector: str) -> int | None:
    if not vector.startswith(VECTOR_ID_PREFIX):
        return None
    try:
        return int(vector.removeprefix(VECTOR_ID_PREFIX))
    except ValueError:
        return None


class SemanticIndex(Protocol):
    async def score(
        self,
        *,
        vector: tuple[float, ...],
        namespace: str,
        store_id: int,
        product_ids: list[int],
    ) -> dict[int, float]: ...


class PineconeSemanticIndex:
    """Adapter over the Pinecone client. One index handle per process."""

    def __init__(self, settings: PineconeSettings) -> None:
        # Lazy and optional, like boto3: a deployment with semantic ranking
        # switched off never imports the SDK and need not install it.
        #
        # But *configured* and *installed* are two different things, and only
        # one of them is visible in the settings. Configuring Pinecone without
        # the `semantic` extra installed used to kill startup with a bare
        # ModuleNotFoundError naming a package the operator never mentioned;
        # now it says which install is missing (CLAUDE.md 21).
        try:
            from pinecone import Pinecone  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ConfigurationError(
                detail=(
                    "pinecone settings are configured but the 'semantic' extra "
                    "is not installed; install it or unset ZORY_PINECONE__*"
                ),
                public_message="Semantic ranking is not installed.",
            ) from exc

        self._index = Pinecone(api_key=settings.api_key.get_secret_value()).Index(
            settings.index_name
        )
        self._index_name = settings.index_name

    async def score(
        self,
        *,
        vector: tuple[float, ...],
        namespace: str,
        store_id: int,
        product_ids: list[int],
    ) -> dict[int, float]:
        """Similarity per product id, for the ids given and no others.

        Returns only what the index scored. Ids with no vector are simply
        absent, which the caller reads as an indexing gap rather than as a low
        score.
        """
        scores: dict[int, float] = {}
        for start in range(0, len(product_ids), MAX_FILTER_IDS):
            chunk = product_ids[start : start + MAX_FILTER_IDS]
            try:
                response = await asyncio.to_thread(
                    self._index.query,
                    vector=list(vector),
                    top_k=len(chunk),
                    namespace=namespace,
                    filter={
                        "product_id": {"$in": chunk},
                        "store_id": {"$eq": store_id},
                    },
                    include_metadata=False,
                    include_values=False,
                )
            except Exception as exc:
                logger.warning(
                    "semantic_index_query_failed",
                    index=self._index_name,
                    error_type=type(exc).__name__,
                )
                raise SemanticIndexUnavailableError(reason=type(exc).__name__) from exc
            scores.update(self._read(response))
        return scores

    @staticmethod
    def _read(response: Any) -> dict[int, float]:
        matches = response["matches"] if isinstance(response, dict) else response.matches
        scored: dict[int, float] = {}
        for match in matches:
            raw = match["id"] if isinstance(match, dict) else match.id
            identifier = product_id_of(raw)
            if identifier is None:
                # An id shaped by another pipeline. Ignored rather than parsed
                # into something plausible.
                continue
            scored[identifier] = float(match["score"] if isinstance(match, dict) else match.score)
        return scored
