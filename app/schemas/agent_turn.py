"""The reply a customer reads, as the client receives it."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.text_choice import (
    TextReplyChoice,
    require_a_question,
    validate_text_choices,
)

MAX_RESPONSE_CHARS = 4000


class RoomSwapContext(BaseModel):
    """Which room piece a list of alternatives is for, so a tap replaces it.

    Set when the customer asked for cheaper options for an over-budget swap: the
    alternatives are a search, but selecting one must swap that role, not pick a
    fresh product (which would cross-sell a piece the room already holds). The
    client reads this to send a `swap` for `bundle_ordinal` rather than a pick
    (CLAUDE.md 27). No product id crosses the boundary - only the ordinal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle_ordinal: int = Field(ge=1)
    role: str = Field(min_length=1)


class CustomerResponse(BaseModel):
    """What the response model returns: prose, and which products it meant.

    It cites `grounding_ref` handles rather than stating facts, because the
    application renders the card. That removes the model's *reason* to state a
    price, which is a stronger position than checking afterwards whether the
    price it stated was right (CLAUDE.md 20.6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str = Field(min_length=1, max_length=MAX_RESPONSE_CHARS)
    referenced_grounding_refs: tuple[int, ...] = ()
    follow_up_question: str | None = Field(default=None, max_length=300)
    """At most one, and only when the policy allowed it. Validating it against
    the policy needs the grounding, so that check lives with the caller."""
    choices: tuple[TextReplyChoice, ...] = Field(default=(), max_length=6)
    """Answers authored with the question, never inferred by the client."""
    _distinct_choices = field_validator("choices")(validate_text_choices)

    @model_validator(mode="after")
    def _choices_answer_a_question(self) -> Self:
        require_a_question(self.choices, self.message, self.follow_up_question)
        return self

    @field_validator("referenced_grounding_refs")
    @classmethod
    def _refs_are_real_handles(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        """Shape only. Whether each handle exists is checked against the
        grounding by the caller, which is the check that matters."""
        if any(ref < 1 for ref in value):
            raise ValueError("a grounding ref is 1 or greater")
        if len(set(value)) != len(value):
            raise ValueError("a response must not cite the same product twice")
        return value
