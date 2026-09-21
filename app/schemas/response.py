"""What the response layer may see, and how a turn reaches it.

The response model writes conversation. It does not state facts, so it is not
given any: no product name, price, dimension, colour, style or URL reaches it.
Everything the customer is told about a product is rendered by the application
from verified grounding.

That is an authority decision before it is a safety one. A model with a price
in its context has a reason to mention the price, and checking afterwards
whether the figure it mentioned was right is a weaker position than never
giving it one (CLAUDE.md 20.4, 20.6). It also removes an entire injection
surface: catalog-controlled text never enters a prompt.

What remains is enough to write a sentence with - what kind of turn this is,
how many things are on screen, whether the search was widened, and which
reason a question is being asked for.

**Not every branch calls a model.** A clarification the decision model already
worded is passed through; a handled failure and a design handoff are worded
deterministically. Those branches are absent from `ResponseOutcomeKind`
entirely, so the model-facing enum cannot describe a job the model does not do.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.agent_decision import BlockingClarificationReason
from app.schemas.comparison import MIN_COMPARED_PRODUCTS, ComparisonField
from app.schemas.conversation import ConversationContext
from app.schemas.grounding import TurnFailureCode
from app.schemas.relaxation import RelaxableField
from app.schemas.resolution import (
    DeterministicClarification,
    ReferenceFailureReason,
    RelativePriceFailureReason,
    SearchRequirementClarificationReason,
)
from app.taxonomy.dimensions import DimensionRole


class ResponseOutcomeKind(StrEnum):
    """The response jobs a model is actually asked to do.

    Model-facing, so membership is an authority decision: a branch appears here
    only if the model writes its words. The three that do not - a pass-through
    clarification, a handled failure, a design handoff - are deliberately
    absent rather than listed for symmetry.
    """

    ANSWER = "answer"
    SEARCH_RESULTS = "search_results"
    ZERO_RESULTS = "zero_results"
    PRODUCT_DETAIL = "product_detail"
    COMPARISON = "comparison"
    DETERMINISTIC_CLARIFICATION = "deterministic_clarification"


class DeterministicResponseKind(StrEnum):
    """Branches answered without a model call.

    **Application-only.** Each has a reason it needs no model: the wording
    already exists, or there is nothing to say that a fixed sentence does not
    say better and more safely.
    """

    MODEL_CLARIFICATION = "model_clarification"
    """The decision model already wrote the question. Re-wording it could only
    change what was asked."""

    HANDLED_FAILURE = "handled_failure"
    """"We could not do that just now" has no conversational nuance to gain,
    and a model call would add hallucination surface for none."""

    DESIGN_HANDOFF = "design_handoff"
    """Nothing ran. A model given an empty grounding here would be invited to
    improvise about a capability that does not exist yet."""


class DeterministicResponse(BaseModel):
    """A branch that answers without reaching a model. Application-only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: DeterministicResponseKind
    failure_code: TurnFailureCode | None = None
    """Never model-visible. It selects fixed wording, and that is all."""

    @model_validator(mode="after")
    def _only_a_failure_carries_a_code(self) -> Self:
        if (self.kind is DeterministicResponseKind.HANDLED_FAILURE) != (
            self.failure_code is not None
        ):
            raise ValueError("a failure carries its code, and only a failure does")
        return self


class ResponseGroundingView(BaseModel):
    """What one turn's outcome looks like to the response model.

    Counts and enum members. No value from the catalog appears - a field *name*
    like `ComparisonField.PRICE` says which axis differed, never by how much.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ResponseOutcomeKind

    presented_count: int = Field(default=0, ge=0)
    """How many products are on screen.

    The one number that makes ordinals sayable: without it the model cannot
    know whether "the second one" refers to anything.
    """

    was_relaxed: bool = False
    relaxed_fields: tuple[RelaxableField, ...] = ()
    dropped_roles: tuple[DimensionRole, ...] = ()
    """Which axes moved or were lost - never by how much.

    The figures are real and the customer should hear them, but a widened
    5,000 becoming 5,500 is rendered by the application from the authoritative
    relaxation summary. The model says it broadened the search; it does not
    say the numbers (CLAUDE.md 13.4).
    """

    compared_count: int = Field(default=0, ge=0)
    comparison_differs_on: tuple[ComparisonField, ...] = ()
    """Which fields differ. Never a cell, so "they differ mainly on width" is
    sayable and "one is 20 cm wider" is not."""

    clarification_reason: (
        BlockingClarificationReason | SearchRequirementClarificationReason | None
    ) = None
    reference_reason: ReferenceFailureReason | None = None
    relative_price_reason: RelativePriceFailureReason | None = None
    """Why a question is being asked, in reason codes. The words are the
    model's job; which question to ask is not."""

    @model_validator(mode="after")
    def _the_kind_and_its_evidence_agree(self) -> Self:
        # A question may be the whole job, or it may accompany one. What it may
        # never be is a reason with no question behind it: the response layer
        # reports what the coordinator found and invents nothing.
        if (
            self.kind is ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION
            and self.clarification_reason is None
        ):
            raise ValueError("a clarification names the reason it is being asked")
        if self.clarification_reason is None and (
            self.reference_reason is not None
            or self.relative_price_reason is not None
        ):
            raise ValueError("a detail reason needs the reason it details")

        if self.kind is ResponseOutcomeKind.ZERO_RESULTS and self.presented_count:
            raise ValueError("a zero-result search presents nothing")
        if self.kind is ResponseOutcomeKind.SEARCH_RESULTS and not self.presented_count:
            raise ValueError("a results outcome presents something")

        comparing = self.kind is ResponseOutcomeKind.COMPARISON
        if comparing and self.compared_count < MIN_COMPARED_PRODUCTS:
            raise ValueError("a comparison covers at least two products")
        if not comparing and (self.compared_count or self.comparison_differs_on):
            raise ValueError("only a comparison carries comparison detail")

        if self.was_relaxed != bool(self.relaxed_fields):
            raise ValueError("was_relaxed must match the fields recorded")
        return self


class ResponseInput(BaseModel):
    """Everything the response model is given, and nothing else.

    No `AgentStateV1`, no `CustomerTurnResult`, no `RetailerContext`, no
    `GroundedProduct`, no `ProductComparisonResult`. The customer's words and
    the prior turns are untrusted data and travel as the user turn, never
    merged into the instructions (CLAUDE.md 20.1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str = Field(min_length=1)
    conversation: ConversationContext = ConversationContext()
    grounding: ResponseGroundingView
    follow_up_allowed: bool = False
    """Whether one optional question may be offered.

    A boolean rather than the policy enum: the model decides the wording, not
    whether the turn is allowed to ask.
    """


ResponseRouting = ResponseGroundingView | DeterministicResponse
"""Either the model writes this turn's words, or the application does."""


class SideEffectNotice(StrEnum):
    """An optional interaction that did not complete, as a fixed notice.

    A turn can succeed at what the customer asked for and still fail at a side
    effect they asked for in the same breath. "Select the beige one and show me
    coffee tables" can find the tables and fail to resolve the selection, and
    answering only "here are some coffee tables" would tell them the whole
    request worked.

    Application-owned and wordless: the failure never reaches the response
    model, because a model told about a failed selection would start explaining
    it, and there is nothing to explain that a fixed sentence does not say
    better. Each member selects wording that carries no product fact, no
    number, no internal detail and no question.
    """

    SELECTION_NOT_UPDATED = "selection_not_updated"
    SELECTION_NOT_REMOVED = "selection_not_removed"
    FOCUS_NOT_CHANGED = "focus_not_changed"


class ResponseRoute(BaseModel):
    """How one turn is answered: the outcome, and anything else it owes.

    Application-only, and lossless by design. A turn can succeed at what the
    customer asked for *and* owe them a question about something else they
    said in the same breath - "select the beige one and show me coffee tables"
    can find the tables and still not know which sofa was meant. Three
    independent facts, so three fields: neither the results nor the question
    may displace the other.

    `side_notice` and `required_clarification` are kept out of
    `ResponseGroundingView` where they are not needed: the view is model-facing,
    and a notice about a failed side effect is composed after generation rather
    than explained by a model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: ResponseRouting

    required_clarification: DeterministicClarification | None = None
    """The one question this turn owes, beside a primary outcome that succeeded.

    Already chosen by the coordinator's priority - primary over proposal over
    optional interaction - so this is that single question, not a queue and not
    a second channel. When the clarification *is* the whole job, it is the
    primary route instead and this stays None.
    """

    side_notice: SideEffectNotice | None = None

    follow_up_allowed: bool = False
    """Whether an *optional* question may still be offered.

    Never when a question is already owed: one question per turn, and a
    required one outranks an invitation.
    """


class ResponseViolationKind(StrEnum):
    """Why a generated response may not be returned.

    The distinction that matters: only an unsupported *number* earns the one
    correction call. Every other violation means the model produced something
    semantically wrong, and asking it again is not the remedy - the response
    layer falls back instead.
    """

    UNSUPPORTED_NUMBER = "unsupported_number"
    UNKNOWN_GROUNDING_REF = "unknown_grounding_ref"
    FOLLOW_UP_NOT_ALLOWED = "follow_up_not_allowed"


class ResponseViolation(BaseModel):
    """A generated response that must not reach the customer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ResponseViolationKind
    detail: str = ""
    """The offending token, for logging and tests. Never sent back to the
    model: re-injecting an invented figure is how it gets reused."""

    field: str | None = None

    @property
    def permits_correction(self) -> bool:
        """Whether the one extra model call is allowed for this violation.

        Reserved for numeric failures alone. A citation of a product that was
        never grounded is not a wording problem.
        """
        return self.kind is ResponseViolationKind.UNSUPPORTED_NUMBER
