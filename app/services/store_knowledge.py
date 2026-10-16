"""Finding what the store's own documents say about one question.

Deterministic retrieval, no reasoning: the question is embedded with the model
the platform indexed the passages with, the store's namespace is searched, and
the passages close enough to be about it are returned. Whether they answer the
question is the reply's job, written from these passages and nothing else.

Store scope comes only from RetailerContext. Every hit is checked against it
once more before it is used: a passage tagged for another store is dropped and
logged, never shown (CLAUDE.md 8, 16).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.core.config import StoreKnowledgeSettings
from app.core.logging import get_logger
from app.integrations.embeddings import QueryEmbedder
from app.integrations.store_knowledge import KnowledgeIndex
from app.schemas.retailer import RetailerContext
from app.schemas.store_knowledge import KnowledgePassage, StoreKnowledgeGrounding

logger = get_logger(__name__)


class StoreKnowledgeService:
    """Searches one store's documents. Holds no state between questions."""

    def __init__(
        self,
        embedder: QueryEmbedder,
        index: KnowledgeIndex,
        settings: StoreKnowledgeSettings,
    ) -> None:
        self._embedder = embedder
        self._index = index
        self._settings = settings

    async def lookup(self, context: RetailerContext, question: str) -> StoreKnowledgeGrounding:
        """The passages about `question` in this store's documents, best first.

        Raises `EmbeddingUnavailableError` or `StoreKnowledgeUnavailableError`
        when the search could not run; an empty result means it ran and the
        documents say nothing about it.
        """
        vector = await self._embedder.embed_query(question)
        hits = await self._index.search(
            vector=vector,
            namespace=self._settings.namespace_for(context.store_id),
            store_id=context.store_id,
            top_k=self._settings.top_k,
        )
        passages: list[KnowledgePassage] = []
        for score, metadata in hits:
            if score < self._settings.min_score:
                continue
            passage = self._passage(metadata, context.store_id)
            if passage is not None and passage not in passages:
                passages.append(passage)
        logger.info(
            "store_knowledge_looked_up",
            store_id=context.store_id,
            hits=len(hits),
            passages=len(passages),
        )
        return StoreKnowledgeGrounding(question=question, passages=tuple(passages))

    def _passage(self, metadata: dict[str, Any], store_id: int) -> KnowledgePassage | None:
        """One hit as a passage, or None when it cannot be trusted or read."""
        tagged = metadata.get("store_id")
        if not isinstance(tagged, int | float) or int(tagged) != store_id:
            logger.warning("store_knowledge_foreign_passage", store_id=store_id)
            return None
        text = str(metadata.get("text") or "").strip()[: self._settings.max_passage_chars]
        try:
            return KnowledgePassage(
                title=str(metadata.get("title") or "").strip()[:255],
                page=int(metadata.get("page") or 1),
                text=text,
            )
        except (ValidationError, TypeError, ValueError):
            logger.warning("store_knowledge_unreadable_passage", store_id=store_id)
            return None


__all__ = ["StoreKnowledgeService"]
