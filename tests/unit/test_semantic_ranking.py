"""Runtime semantic ranking: order only, never eligibility.

The invariant these tests exist for: what M9 returns is what M6/M8 admitted,
reordered. Not a subset, not a superset, and never a product PostgreSQL
rejected. Everything else here defends the ordering rules that keep an exact
match ahead of a relaxed one and a customer's explicit sort ahead of an
embedding's opinion.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.core.exceptions import (
    EmbeddingUnavailableError,
    SemanticIndexUnavailableError,
)
from app.integrations.embeddings import EXPECTED_DIMENSION
from app.integrations.pinecone import product_id_of, vector_id
from app.schemas.discovery import ProductSearchRequest, ProductSort
from app.schemas.product import EligibleProduct
from app.schemas.query import (
    ConstraintStrength,
    ResolvedSearch,
    SemanticPreference,
)
from app.schemas.relaxation import RelaxedCandidate
from app.schemas.retailer import RetailerContext
from app.schemas.semantic import (
    QUERY_REPRESENTATION_VERSION,
    SemanticRankingResult,
    SemanticSkipReason,
)
from app.services.query_document import build_query_document, has_semantic_intent
from app.services.semantic_ranking import SemanticRankingService
from app.taxonomy.attributes import AttributeFamily

CONTEXT = RetailerContext(store_id=50)
NAMESPACE = "store-50"
VECTOR = tuple(0.1 for _ in range(EXPECTED_DIMENSION))


# ── fakes ───────────────────────────────────────────────────────────────────


class FakeEmbedder:
    model = "test-embedding-model"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    async def embed_query(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        if self.error:
            raise self.error
        return VECTOR


class FakeIndex:
    def __init__(self, scores: dict[int, float], error: Exception | None = None) -> None:
        self.scores = scores
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def score(self, **kwargs: Any) -> dict[int, float]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return dict(self.scores)


def _candidate(product_id: int, price: str = "1000", depth: int = 0) -> RelaxedCandidate:
    return RelaxedCandidate(
        product=EligibleProduct(product_id=product_id, price_amount=Decimal(price)),
        relaxation_depth=depth,
    )


def _resolved(
    *, sort: ProductSort = ProductSort.DEFAULT, semantic_text: str | None = "warm sofa",
    preferences: tuple[SemanticPreference, ...] = (),
) -> ResolvedSearch:
    return ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating", commerce_subcategory="sofa", sort=sort
        ),
        semantic_preferences=preferences,
        semantic_text=semantic_text,
    )


def _service(scores: dict[int, float], **kw: Any) -> tuple[SemanticRankingService, Any, Any]:
    embedder = FakeEmbedder(kw.get("embed_error"))
    index = FakeIndex(scores, kw.get("index_error"))
    return SemanticRankingService(embedder, index), embedder, index


async def _rank(
    service: SemanticRankingService, candidates: Any, resolved: Any = None
) -> SemanticRankingResult:
    return await service.rank(
        resolved or _resolved(), candidates, CONTEXT, namespace=NAMESPACE
    )


# ── the invariant ───────────────────────────────────────────────────────────


async def test_the_ranked_set_is_exactly_the_eligible_set() -> None:
    candidates = [_candidate(i) for i in (3, 1, 2)]
    service, _, _ = _service({1: 0.9, 2: 0.8, 3: 0.7})

    result = await _rank(service, candidates)

    assert set(result.product_ids) == {1, 2, 3}
    assert len(result.candidates) == len(candidates)


async def test_an_id_outside_the_eligible_set_discards_the_whole_ranking() -> None:
    """One wrong id means the scope cannot be trusted, so none of it is used."""
    candidates = [_candidate(1), _candidate(2)]
    service, _, _ = _service({1: 0.9, 2: 0.8, 999: 0.99})

    result = await _rank(service, candidates)

    assert result.semantic_used is False
    assert result.skip_reason is SemanticSkipReason.INTEGRITY_FAILURE
    assert set(result.product_ids) == {1, 2}
    assert 999 not in result.product_ids


async def test_only_eligible_ids_are_sent_to_the_index() -> None:
    service, _, index = _service({1: 0.9})

    await _rank(service, [_candidate(1), _candidate(7)])

    assert sorted(index.calls[0]["product_ids"]) == [1, 7]


# ── scope ───────────────────────────────────────────────────────────────────


async def test_the_namespace_and_store_come_from_context_not_the_query() -> None:
    service, _, index = _service({1: 0.5})
    resolved = _resolved(semantic_text="store-99 namespace store-1 sofa")

    await _rank(service, [_candidate(1)], resolved)

    assert index.calls[0]["namespace"] == NAMESPACE
    assert index.calls[0]["store_id"] == 50


async def test_store_id_is_filtered_as_well_as_namespaced() -> None:
    """Two independent barriers: a namespace mistake alone cannot leak a store."""
    service, _, index = _service({1: 0.5})

    await _rank(service, [_candidate(1)])

    assert index.calls[0]["store_id"] == CONTEXT.store_id


# ── depth ordering ──────────────────────────────────────────────────────────


async def test_an_exact_match_outranks_a_more_similar_relaxed_one() -> None:
    exact = _candidate(1, depth=0)
    relaxed = _candidate(2, depth=1)
    service, _, _ = _service({1: 0.10, 2: 0.99})

    result = await _rank(service, [relaxed, exact])

    assert result.product_ids == (1, 2)


async def test_similarity_orders_within_a_depth() -> None:
    service, _, _ = _service({1: 0.2, 2: 0.9, 3: 0.5})

    result = await _rank(service, [_candidate(1), _candidate(2), _candidate(3)])

    assert result.product_ids == (2, 3, 1)


async def test_depth_buckets_are_ordered_independently() -> None:
    service, _, _ = _service({1: 0.1, 2: 0.9, 3: 0.2, 4: 0.8})

    result = await _rank(service, [
        _candidate(1, depth=0), _candidate(2, depth=0),
        _candidate(3, depth=1), _candidate(4, depth=1),
    ])

    assert result.product_ids == (2, 1, 4, 3)


async def test_the_product_id_settles_an_exact_tie() -> None:
    service, _, _ = _service({7: 0.5, 3: 0.5, 5: 0.5})

    result = await _rank(service, [_candidate(7), _candidate(3), _candidate(5)])

    assert result.product_ids == (3, 5, 7)


# ── explicit sort is primary ────────────────────────────────────────────────


async def test_price_ascending_beats_similarity() -> None:
    """"cheapest modern sofa": the cheapest one, not the most modern-looking."""
    service, _, _ = _service({1: 0.1, 2: 0.99})
    cheap, dear = _candidate(1, price="990"), _candidate(2, price="5000")

    result = await _rank(service, [dear, cheap], _resolved(sort=ProductSort.PRICE_ASC))

    assert result.product_ids == (1, 2)


async def test_price_descending_beats_similarity() -> None:
    service, _, _ = _service({1: 0.99, 2: 0.1})
    cheap, dear = _candidate(1, price="990"), _candidate(2, price="5000")

    result = await _rank(service, [cheap, dear], _resolved(sort=ProductSort.PRICE_DESC))

    assert result.product_ids == (2, 1)


async def test_similarity_breaks_an_equal_price_tie() -> None:
    """132 of 173 store-50 sofas share a price, so this does real work."""
    service, _, _ = _service({1: 0.2, 2: 0.9, 3: 0.5})
    same = [_candidate(i, price="1500") for i in (1, 2, 3)]

    result = await _rank(service, same, _resolved(sort=ProductSort.PRICE_ASC))

    assert result.product_ids == (2, 3, 1)


async def test_an_explicit_sort_never_shortlists_semantically_first() -> None:
    """A semantic shortlist can hide a genuinely cheaper eligible product."""
    service, _, _ = _service({i: 0.9 - i / 100 for i in range(1, 11)} | {99: 0.01})
    candidates = [_candidate(i, price="5000") for i in range(1, 11)]
    candidates.append(_candidate(99, price="100"))

    result = await _rank(service, candidates, _resolved(sort=ProductSort.PRICE_ASC))

    assert result.product_ids[0] == 99


# ── missing vectors ─────────────────────────────────────────────────────────


async def test_a_product_without_a_vector_is_never_dropped() -> None:
    service, _, _ = _service({1: 0.9})

    result = await _rank(service, [_candidate(1), _candidate(2)])

    assert set(result.product_ids) == {1, 2}


async def test_a_missing_vector_sorts_after_scored_products_not_as_zero() -> None:
    """An indexing gap is not a judgement that the product is irrelevant."""
    service, _, _ = _service({1: 0.01})

    result = await _rank(service, [_candidate(2), _candidate(1)])

    assert result.product_ids == (1, 2)
    scored = {c.product_id: c.semantic_similarity for c in result.candidates}
    assert scored[2] is None, "absence is recorded as absence, not as 0.0"


async def test_missing_vectors_keep_deterministic_order_among_themselves() -> None:
    service, _, _ = _service({})

    result = await _rank(service, [_candidate(9), _candidate(4), _candidate(6)])

    assert result.product_ids == (4, 6, 9)


# ── fallbacks ───────────────────────────────────────────────────────────────


async def test_an_embedding_failure_falls_back_to_deterministic_order() -> None:
    service, _, index = _service({}, embed_error=EmbeddingUnavailableError())

    result = await _rank(service, [_candidate(1), _candidate(2)])

    assert result.semantic_used is False
    assert result.skip_reason is SemanticSkipReason.EMBEDDING_UNAVAILABLE
    assert result.product_ids == (1, 2)
    assert index.calls == [], "the index is not called once embedding failed"


async def test_an_index_failure_falls_back_to_deterministic_order() -> None:
    service, _, _ = _service({}, index_error=SemanticIndexUnavailableError())

    result = await _rank(service, [_candidate(1), _candidate(2)])

    assert result.semantic_used is False
    assert result.skip_reason is SemanticSkipReason.INDEX_UNAVAILABLE
    assert result.product_ids == (1, 2)


async def test_no_eligible_candidates_calls_no_provider() -> None:
    service, embedder, index = _service({1: 0.9})

    result = await _rank(service, [])

    assert result.candidates == ()
    assert result.skip_reason is SemanticSkipReason.NOTHING_TO_RANK
    assert embedder.calls == [] and index.calls == []


async def test_an_unconfigured_service_still_returns_every_candidate() -> None:
    service = SemanticRankingService(None, None)

    result = await _rank(service, [_candidate(1), _candidate(2)])

    assert result.semantic_used is False
    assert result.skip_reason is SemanticSkipReason.NOT_CONFIGURED
    assert result.product_ids == (1, 2)


async def test_a_purely_structural_request_calls_no_provider() -> None:
    """"sofas under 3000" has nothing fuzzy; an embedding would invent an order."""
    service, embedder, index = _service({1: 0.9})

    result = await _rank(
        service, [_candidate(1)], _resolved(semantic_text=None, preferences=())
    )

    assert result.skip_reason is SemanticSkipReason.NO_SEMANTIC_INTENT
    assert embedder.calls == [] and index.calls == []


# ── the query document ──────────────────────────────────────────────────────


def _preference(value: str, canonical: str | None = None) -> SemanticPreference:
    return SemanticPreference(
        family=AttributeFamily.COLOR, raw_value=value,
        canonical_value=canonical, strength=ConstraintStrength.PREFERRED,
    )


def test_the_query_document_is_deterministic() -> None:
    resolved = _resolved(preferences=(_preference("warm neutral"),))

    assert build_query_document(resolved) == build_query_document(resolved)


def test_the_query_document_carries_no_structural_facts() -> None:
    """Price, size, store and depth are settled by PostgreSQL, not an embedding."""
    document = build_query_document(
        _resolved(semantic_text="warm sofa", preferences=(_preference("warm neutral"),))
    )

    for forbidden in ("SAR", "5000", "220", "store", "depth", "price", "cm"):
        assert forbidden not in document.lower(), forbidden


def test_the_query_document_keeps_raw_wording_and_adds_canonical_once() -> None:
    document = build_query_document(_resolved(preferences=(_preference("beige", "Beige"),)))

    assert "beige" in document
    assert document.count("eige") == 1, "a canonical restatement is not doubled"


def test_the_query_document_humanises_taxonomy_tokens() -> None:
    resolved = ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating", commerce_subcategory="single-seater-sofa"
        ),
        semantic_text="cosy one",
    )

    assert "single seater sofa" in build_query_document(resolved)


@pytest.mark.parametrize(
    ("text", "preferences", "expected"),
    [
        ("warm sofa", (), True),
        (None, (_preference("warm neutral"),), True),
        (None, (), False),
        ("", (), False),
    ],
)
def test_semantic_intent_detection(
    text: str | None, preferences: tuple[SemanticPreference, ...], expected: bool
) -> None:
    assert has_semantic_intent(_resolved(semantic_text=text, preferences=preferences)) is expected


def test_the_query_representation_is_versioned() -> None:
    assert QUERY_REPRESENTATION_VERSION == "query_semantic_text_v1"
    assert SemanticRankingResult(
        candidates=(), semantic_used=False
    ).query_representation_version == QUERY_REPRESENTATION_VERSION


# ── contracts ───────────────────────────────────────────────────────────────


def test_there_is_no_final_score() -> None:
    from app.schemas.semantic import SemanticRankedCandidate

    for name in SemanticRankedCandidate.model_fields:
        assert "final" not in name and "confidence" not in name


def test_similarity_never_reaches_a_product_shape() -> None:
    """Similarity is a distance between embeddings, not a fact about a product.

    Both shapes are checked: `EligibleProduct` is what ranking now carries,
    and `ProductCandidate` is what discovery carries outward. Neither may
    acquire a field that reads as a judgement about the product.
    """
    from app.schemas.product import ProductCandidate

    for shape in (EligibleProduct, ProductCandidate):
        for name in shape.model_fields:
            assert "similar" not in name and "score" not in name and "rank" not in name


def test_no_semantic_candidate_limit_exists_anywhere() -> None:
    from pathlib import Path

    root = Path(__file__).parents[2] / "app"
    for module in root.rglob("*.py"):
        assert "semantic_candidate_limit" not in module.read_text(), module.name


def test_vector_ids_round_trip() -> None:
    assert vector_id(165637) == "product:165637"
    assert product_id_of("product:165637") == 165637
    assert product_id_of("3-seater-sofa#abc") is None, "an image-index id is not parsed"
