"""Query embedding, and nothing more.

One narrow capability: turn one query document into one vector. It does not
embed products - that is an offline indexing concern - and it is not a general
AI abstraction.

Failure here is never fatal. A caller that cannot embed falls back to
deterministic order, so every provider fault becomes a typed exception the
ranking service can absorb (CLAUDE.md 21).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
    omit,
)

from app.core.config import LLMSettings
from app.core.exceptions import EmbeddingUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

EXPECTED_DIMENSION = 3072
"""The width `text-embedding-3-large` returns, and the width the index was
built at. A mismatch means the model or the index changed underneath us, and
comparing across it would produce silently meaningless distances."""


class QueryEmbedder(Protocol):
    @property
    def model(self) -> str: ...

    async def embed_query(self, text: str) -> tuple[float, ...]: ...


class BatchQueryEmbedder(Protocol):
    """Embeds several texts in one request (a room's pieces together)."""

    async def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


class OpenAIQueryEmbedder:
    """Adapter over the OpenAI embeddings API.

    One client per process, created in lifespan and reused (CLAUDE.md 24).
    Timeout and retries come from the same bounded settings the chat client
    uses; nothing retries forever.
    """

    def __init__(
        self,
        settings: LLMSettings,
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> None:
        """`model` and `dimensions` serve an index built otherwise.

        By default this embeds for the semantic ranking index, with the
        configured embedding model at its native width. A caller querying
        another index names that index's model and, when it was built at a
        reduced width, that width - which is then both requested from the
        model and the width every answer is checked against.
        """
        chosen = model or settings.embedding_model
        if not chosen:
            raise EmbeddingUnavailableError(reason="no embedding model configured")
        self._model = chosen
        self._dimensions = dimensions
        self._expected = dimensions or EXPECTED_DIMENSION
        self._client = AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=settings.base_url,
            timeout=settings.timeout_s,
            max_retries=settings.max_retries,
        )

    @property
    def model(self) -> str:
        return self._model

    async def embed_query(self, text: str) -> tuple[float, ...]:
        (vector,) = await self.embed_queries((text,))
        return vector

    async def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """One vector per text, in order, from one request: a room's pieces
        are embedded together rather than one call each."""
        if not texts:
            return ()
        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=list(texts),
                dimensions=omit if self._dimensions is None else self._dimensions,
            )
        except (APITimeoutError, APIConnectionError) as exc:
            logger.warning("embedding_unreachable", error_type=type(exc).__name__)
            raise EmbeddingUnavailableError(reason=type(exc).__name__) from exc
        except APIStatusError as exc:
            # Status and exception class only: a response body may echo the
            # query text back at us.
            logger.warning(
                "embedding_provider_error",
                status_code=exc.status_code,
                error_type=type(exc).__name__,
            )
            raise EmbeddingUnavailableError(status_code=exc.status_code) from exc
        except OpenAIError as exc:
            logger.warning("embedding_call_failed", error_type=type(exc).__name__)
            raise EmbeddingUnavailableError(reason=type(exc).__name__) from exc

        ordered = sorted(response.data, key=lambda item: item.index)
        if len(ordered) != len(texts):
            logger.error("embedding_count_mismatch", asked=len(texts), returned=len(ordered))
            raise EmbeddingUnavailableError(reason="one vector per text was not returned")
        vectors = tuple(tuple(item.embedding) for item in ordered)
        for vector in vectors:
            if len(vector) != self._expected:
                logger.error(
                    "embedding_dimension_mismatch",
                    model=self._model,
                    returned=len(vector),
                    expected=self._expected,
                )
                raise EmbeddingUnavailableError(
                    reason=f"expected {self._expected} dimensions, got {len(vector)}"
                )
        return vectors

    async def close(self) -> None:
        await self._client.close()
