"""Query embedding, and nothing more.

One narrow capability: turn one query document into one vector. It does not
embed products - that is an offline indexing concern - and it is not a general
AI abstraction.

Failure here is never fatal. A caller that cannot embed falls back to
deterministic order, so every provider fault becomes a typed exception the
ranking service can absorb (CLAUDE.md 21).
"""

from __future__ import annotations

from typing import Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
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


class OpenAIQueryEmbedder:
    """Adapter over the OpenAI embeddings API.

    One client per process, created in lifespan and reused (CLAUDE.md 24).
    Timeout and retries come from the same bounded settings the chat client
    uses; nothing retries forever.
    """

    def __init__(self, settings: LLMSettings) -> None:
        if not settings.embedding_model:
            raise EmbeddingUnavailableError(reason="no embedding model configured")
        self._model = settings.embedding_model
        self._client = AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            timeout=settings.timeout_s,
            max_retries=settings.max_retries,
        )

    @property
    def model(self) -> str:
        return self._model

    async def embed_query(self, text: str) -> tuple[float, ...]:
        try:
            response = await self._client.embeddings.create(
                model=self._model, input=[text]
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

        vector = tuple(response.data[0].embedding)
        if len(vector) != EXPECTED_DIMENSION:
            logger.error(
                "embedding_dimension_mismatch",
                model=self._model,
                returned=len(vector),
                expected=EXPECTED_DIMENSION,
            )
            raise EmbeddingUnavailableError(
                reason=f"expected {EXPECTED_DIMENSION} dimensions, got {len(vector)}"
            )
        return vector

    async def close(self) -> None:
        await self._client.close()
