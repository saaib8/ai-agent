"""The store's own documents, as one turn may quote them.

A store's owner uploads its facts - delivery, returns, warranty, payment,
contact details - to the main platform, which cuts them into passages and
indexes them per store. A passage here is one of those, found for this turn's
question in this store's namespace and nowhere else.

Passages are the only source a store answer may use. They carry the document's
own words and where they came from, never a product, a price from the catalog
or an identifier: what a store says about itself is not a catalog fact, and is
never mixed with one (CLAUDE.md 3.3).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

MAX_STORE_QUESTION_CHARS = 300
"""A restated question is a question, not a transcript."""

MAX_TITLE_CHARS = 255


class KnowledgePassage(BaseModel):
    """One passage of one of the store's documents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    """The document's name as the owner uploaded it."""

    page: int = Field(ge=1)
    text: str = Field(min_length=1)


class StoreKnowledgeGrounding(BaseModel):
    """What the store's documents say about one question.

    No passages is a real outcome, not a failure: the documents were searched
    and say nothing about it, so the honest answer is that we do not know -
    never a guess.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1, max_length=MAX_STORE_QUESTION_CHARS)
    passages: tuple[KnowledgePassage, ...] = ()


__all__ = [
    "MAX_STORE_QUESTION_CHARS",
    "KnowledgePassage",
    "StoreKnowledgeGrounding",
]
