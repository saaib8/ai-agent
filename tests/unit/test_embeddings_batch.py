"""One embeddings request for several texts: order, counts and sizes checked,
failures answered exactly as a single text's are."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from app.core.exceptions import EmbeddingUnavailableError
from app.integrations.embeddings import EXPECTED_DIMENSION, OpenAIQueryEmbedder
from openai import APIConnectionError

from tests.conftest import build_settings


class FakeEmbeddings:
    def __init__(self, data: list[Any] | None = None, error: Exception | None = None) -> None:
        self.data = data or []
        self.error = error
        self.inputs: list[list[str]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.inputs.append(kwargs["input"])
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=self.data)


def _embedder(fake: FakeEmbeddings) -> OpenAIQueryEmbedder:
    settings = build_settings(
        llm={
            "api_key": "test-key-not-real",
            "model": "test-model",
            "embedding_model": "text-embedding-3-large",
        }
    ).llm
    embedder = OpenAIQueryEmbedder(settings)
    embedder._client = SimpleNamespace(embeddings=fake)  # type: ignore[assignment]
    return embedder


def _item(index: int, value: float, size: int = EXPECTED_DIMENSION) -> Any:
    return SimpleNamespace(index=index, embedding=[value] * size)


async def test_vectors_come_back_in_the_order_of_the_texts() -> None:
    fake = FakeEmbeddings([_item(1, 0.2), _item(0, 0.1)])

    vectors = await _embedder(fake).embed_queries(["rug", "lamp"])

    assert fake.inputs == [["rug", "lamp"]]
    assert vectors[0][0] == 0.1 and vectors[1][0] == 0.2


async def test_a_missing_vector_is_refused() -> None:
    with pytest.raises(EmbeddingUnavailableError):
        await _embedder(FakeEmbeddings([_item(0, 0.1)])).embed_queries(["rug", "lamp"])


async def test_a_vector_of_the_wrong_size_is_refused() -> None:
    fake = FakeEmbeddings([_item(0, 0.1), _item(1, 0.2, size=8)])
    with pytest.raises(EmbeddingUnavailableError):
        await _embedder(fake).embed_queries(["rug", "lamp"])


async def test_an_unreachable_provider_is_unavailable_as_for_one_text() -> None:
    error = APIConnectionError(request=SimpleNamespace())  # type: ignore[arg-type]
    with pytest.raises(EmbeddingUnavailableError):
        await _embedder(FakeEmbeddings(error=error)).embed_queries(["rug", "lamp"])


async def test_one_text_still_works_and_nothing_is_sent_for_none() -> None:
    fake = FakeEmbeddings([_item(0, 0.3)])
    embedder = _embedder(fake)

    assert (await embedder.embed_query("rug"))[0] == 0.3
    assert await embedder.embed_queries([]) == ()
    assert len(fake.inputs) == 1
