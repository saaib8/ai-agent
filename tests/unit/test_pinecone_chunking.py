"""`MAX_FILTER_IDS` is transport, not relevance.

A pool larger than one filter request must be split across requests and merged
back whole. The danger is that a transport detail quietly becomes a candidate
cap: chunk, score the first thousand, and return. That would be the bounded
pool again, arriving by a different route.

The adapter is built without `__init__` so the test needs no Pinecone SDK; the
index handle is the only thing it would have created.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.integrations.pinecone import (
    MAX_FILTER_IDS,
    PineconeSemanticIndex,
    vector_id,
)

VECTOR = (0.1, 0.2, 0.3)
NAMESPACE = "store-50"
STORE_ID = 50


class FakeIndexHandle:
    """Scores every id it is handed, and records each request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        ids = kwargs["filter"]["product_id"]["$in"]
        return {
            "matches": [
                {"id": vector_id(i), "score": 0.5 + (i / 1_000_000)} for i in ids
            ]
        }


def _adapter(handle: FakeIndexHandle) -> PineconeSemanticIndex:
    index = object.__new__(PineconeSemanticIndex)
    index._index = handle
    index._index_name = "ai-agent"
    return index


async def _score(handle: FakeIndexHandle, ids: list[int]) -> dict[int, float]:
    return await _adapter(handle).score(
        vector=VECTOR, namespace=NAMESPACE, store_id=STORE_ID, product_ids=ids
    )


@pytest.mark.parametrize(
    ("pool_size", "expected_requests"),
    [(10, 1), (MAX_FILTER_IDS, 1), (MAX_FILTER_IDS + 1, 2), (2500, 3)],
)
async def test_a_large_pool_is_split_across_requests(
    pool_size: int, expected_requests: int
) -> None:
    handle = FakeIndexHandle()

    await _score(handle, list(range(1, pool_size + 1)))

    assert len(handle.requests) == expected_requests


async def test_every_chunk_is_merged_and_nothing_is_dropped() -> None:
    ids = list(range(1, 2501))
    handle = FakeIndexHandle()

    scores = await _score(handle, ids)

    assert set(scores) == set(ids)
    assert len(scores) == 2500


async def test_each_chunk_is_scored_with_the_same_query_vector() -> None:
    """Chunking splits the transport, never the question being asked."""
    handle = FakeIndexHandle()

    await _score(handle, list(range(1, 2501)))

    assert {tuple(r["vector"]) for r in handle.requests} == {VECTOR}


async def test_every_chunk_keeps_the_retailer_scope() -> None:
    """Defence in depth: namespace and metadata filter on every request."""
    handle = FakeIndexHandle()

    await _score(handle, list(range(1, 2501)))

    assert all(r["namespace"] == NAMESPACE for r in handle.requests)
    assert all(
        r["filter"]["store_id"] == {"$eq": STORE_ID} for r in handle.requests
    )


async def test_top_k_never_truncates_a_chunk() -> None:
    """`top_k` below the chunk size would silently drop candidates."""
    handle = FakeIndexHandle()

    await _score(handle, list(range(1, 2501)))

    for request in handle.requests:
        assert request["top_k"] == len(request["filter"]["product_id"]["$in"])


def test_the_chunk_size_is_not_a_candidate_limit() -> None:
    """Named checks, so a future change cannot repurpose it as a cap."""
    from pathlib import Path

    source = (
        Path(__file__).parents[2] / "app/integrations/pinecone.py"
    ).read_text()

    assert "transport hygiene" in source
    assert "semantic_candidate_limit" not in source
