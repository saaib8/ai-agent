"""Query vectors fetched ahead, for ranking to use instead of one call each.

A room's pieces are ranked one search at a time, and each would embed its own
query text. Room discovery embeds them all in one request first and makes the
vectors visible to the searches it then runs (a context variable, which the
tasks searching side by side inherit). Ranking takes a vector from here only
for exactly the text it would have embedded; any other text is embedded as
before, so a prefetch can save calls but never change an order.

When the batch itself failed, the provider is down: the searches skip their
own calls and keep deterministic order at once (CLAUDE.md 16.1), rather than
each waiting out the same timeout.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

_PREFETCHED: ContextVar[Mapping[str, tuple[float, ...]] | None] = ContextVar(
    "prefetched_query_vectors", default=None
)
_FAILED: ContextVar[bool] = ContextVar("prefetch_embedding_failed", default=False)


@contextmanager
def prefetched(vectors: Mapping[str, tuple[float, ...]], *, failed: bool = False) -> Iterator[None]:
    """Within this block, ranking reads these vectors by their exact text -
    or, when the batch `failed`, embeds nothing at all."""
    token = _PREFETCHED.set(dict(vectors))
    failed_token = _FAILED.set(failed)
    try:
        yield
    finally:
        _FAILED.reset(failed_token)
        _PREFETCHED.reset(token)


def embedding_failed() -> bool:
    """Whether this block's batch failed: the provider is down, so no search
    in it should call it again."""
    return _FAILED.get()


def prefetched_vector(document: str) -> tuple[float, ...] | None:
    """The vector fetched ahead for exactly this text, or None."""
    vectors = _PREFETCHED.get()
    return vectors.get(document) if vectors else None
