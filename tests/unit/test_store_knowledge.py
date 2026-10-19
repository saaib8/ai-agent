"""Store knowledge retrieval: scoped twice, filtered by relevance, read safely.

The adapter is built without `__init__` so no Pinecone SDK is needed; the
index handle is the only thing it would have created.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.core.config import StoreKnowledgeSettings
from app.core.exceptions import EmbeddingUnavailableError, StoreKnowledgeUnavailableError
from app.integrations.store_knowledge import PineconeKnowledgeIndex
from app.schemas.retailer import RetailerContext
from app.schemas.store_knowledge import KnowledgePassage
from app.services.store_knowledge import StoreKnowledgeService
from pydantic import SecretStr, ValidationError

VECTOR = (0.1, 0.2, 0.3)
STORE = RetailerContext(store_id=50)
DELIVERY = "Delivery to Jeddah takes 4 to 7 working days."
RETURNS = "Returns are accepted within 14 days."


def settings(**overrides: Any) -> StoreKnowledgeSettings:
    return StoreKnowledgeSettings(api_key=SecretStr("test-key"), index_name="ai-agent", **overrides)


def hit(text: str, score: float = 0.8, store_id: int = 50, **meta: Any) -> dict[str, Any]:
    metadata = {"store_id": store_id, "title": "faq.pdf", "page": 2, "text": text, **meta}
    return {"id": "doc-u#0", "score": score, "metadata": metadata}


class FakeHandle:
    def __init__(self, matches: list[dict[str, Any]], error: Exception | None = None) -> None:
        self.matches = matches
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"matches": self.matches}


class FakeEmbedder:
    model = "text-embedding-3-large"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    async def embed_query(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return VECTOR


def adapter(handle: FakeHandle) -> PineconeKnowledgeIndex:
    index = object.__new__(PineconeKnowledgeIndex)
    index._index = handle  # type: ignore[assignment]
    index._index_name = "ai-agent"
    return index


def service(
    handle: FakeHandle, embedder: FakeEmbedder | None = None, **overrides: Any
) -> StoreKnowledgeService:
    return StoreKnowledgeService(embedder or FakeEmbedder(), adapter(handle), settings(**overrides))


def test_the_namespace_is_the_stores_own_and_never_a_product_namespace() -> None:
    assert settings().namespace_for(50) == "knowledge-store-50"
    assert settings().namespace_for(50) != "store-50"


def test_a_namespace_template_must_name_the_store() -> None:
    with pytest.raises(ValidationError):
        settings(namespace_template="knowledge")


async def test_the_search_is_scoped_to_the_store_twice() -> None:
    handle = FakeHandle([hit(DELIVERY)])

    await service(handle).lookup(STORE, "how long is delivery to jeddah?")

    request = handle.requests[0]
    assert request["namespace"] == "knowledge-store-50"
    assert request["filter"] == {"store_id": {"$eq": 50}}
    assert request["include_metadata"] is True


async def test_close_passages_are_returned_with_their_source() -> None:
    handle = FakeHandle([hit(DELIVERY, 0.9), hit(RETURNS, 0.7, page=3)])
    embedder = FakeEmbedder()

    found = await service(handle, embedder).lookup(STORE, "delivery?")

    assert embedder.calls == ["delivery?"]
    assert found.question == "delivery?"
    assert found.passages == (
        KnowledgePassage(title="faq.pdf", page=2, text=DELIVERY),
        KnowledgePassage(title="faq.pdf", page=3, text=RETURNS),
    )


async def test_passages_below_the_relevance_floor_are_not_shown() -> None:
    found = await service(FakeHandle([hit(DELIVERY, 0.9), hit(RETURNS, 0.1)])).lookup(
        STORE, "delivery?"
    )
    assert [p.text for p in found.passages] == [DELIVERY]


async def test_nothing_relevant_is_an_empty_answer_not_a_failure() -> None:
    found = await service(FakeHandle([])).lookup(STORE, "do you have a showroom in dammam?")
    assert found.passages == ()


async def test_a_passage_tagged_for_another_store_is_dropped() -> None:
    found = await service(FakeHandle([hit(DELIVERY, store_id=51), hit(RETURNS)])).lookup(
        STORE, "returns?"
    )
    assert [p.text for p in found.passages] == [RETURNS]


async def test_an_unreadable_passage_is_dropped() -> None:
    found = await service(FakeHandle([hit(""), hit(RETURNS, page="x")])).lookup(STORE, "x")
    assert found.passages == ()


async def test_long_passages_are_cut_to_the_configured_length() -> None:
    found = await service(FakeHandle([hit("a" * 500)]), max_passage_chars=100).lookup(STORE, "x")
    assert len(found.passages[0].text) == 100


async def test_the_same_passage_twice_is_shown_once() -> None:
    found = await service(FakeHandle([hit(DELIVERY), hit(DELIVERY)])).lookup(STORE, "x")
    assert len(found.passages) == 1


async def test_an_index_failure_is_reported() -> None:
    with pytest.raises(StoreKnowledgeUnavailableError):
        await service(FakeHandle([], error=RuntimeError("down"))).lookup(STORE, "x")


async def test_an_embedding_failure_is_reported_before_any_search() -> None:
    handle = FakeHandle([hit(DELIVERY)])
    embedder = FakeEmbedder(error=EmbeddingUnavailableError(reason="down"))
    with pytest.raises(EmbeddingUnavailableError):
        await service(handle, embedder).lookup(STORE, "x")
    assert handle.requests == []
