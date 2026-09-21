"""Semantic ranking contracts.

Ranking decides ORDER. Eligibility was already decided by PostgreSQL, and
nothing here may widen it: a product absent from the eligible set can never
appear in a ranked one.

Similarity is internal. It is a distance between two embeddings, not a claim
about a product, and it never reaches a customer-facing shape.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

QUERY_REPRESENTATION_VERSION = "query_semantic_text_v1"
"""The query document contract. Change the document, change this."""


class SemanticSkipReason(StrEnum):
    """Why an eligible set was returned in deterministic order instead."""

    NOT_CONFIGURED = "not_configured"
    """No Pinecone settings: semantic ranking is switched off, not broken."""

    NOTHING_TO_RANK = "nothing_to_rank"
    """No eligible candidates. No provider is called to order an empty list."""

    NO_SEMANTIC_INTENT = "no_semantic_intent"
    """Nothing fuzzy was asked for, so an embedding would invent an order."""

    EMBEDDING_UNAVAILABLE = "embedding_unavailable"
    INDEX_UNAVAILABLE = "index_unavailable"
    INTEGRITY_FAILURE = "integrity_failure"
    """The index returned an id outside the eligible set. Ranking is discarded
    whole rather than filtered down, because one wrong id means the scope
    cannot be trusted."""


class SemanticRankedCandidate(BaseModel):
    """One product's place in the ranking.

    `relaxation_depth` comes from M8 and outranks similarity absolutely: a
    product that satisfied the customer's exact request is never displaced by
    one that needed a widened bound, whatever an embedding thinks.
    """

    model_config = ConfigDict(frozen=True)

    product_id: int
    relaxation_depth: int = Field(ge=0)
    semantic_similarity: float | None = None
    """None when this product had no vector. Absence of a vector is an indexing
    gap, never evidence of low relevance."""

    semantic_rank: int = Field(ge=0)
    """Final position, zero-based, after every ordering rule has been applied."""


class SemanticRankingResult(BaseModel):
    """An ordering, and an honest account of how it was produced."""

    model_config = ConfigDict(frozen=True)

    candidates: tuple[SemanticRankedCandidate, ...]
    semantic_used: bool
    query_representation_version: str = QUERY_REPRESENTATION_VERSION
    embedding_model: str | None = None
    skip_reason: SemanticSkipReason | None = None
    ranked_count: int = Field(default=0, ge=0)
    """How many carried a similarity. The rest kept deterministic order."""

    @property
    def product_ids(self) -> tuple[int, ...]:
        return tuple(c.product_id for c in self.candidates)
