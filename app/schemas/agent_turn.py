"""The two inputs of a turn, and what a turn hands to the response layer.

The pair of input types is the authority boundary made structural.
`CustomerTurnInput` is what application code holds: the customer's words, the
history a caller selected, the authoritative state, and the retailer scope.
`DecisionInput` is what a model is given, and it is a different type precisely
so that giving it the wrong thing is a type error rather than a review
oversight.

`RetailerContext` appears in the first and cannot appear in the second. There
is no decision a model makes better for knowing which store it is serving, and
every scoped call already receives the context directly (CLAUDE.md 8, 20.2).
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.agent_decision import (
    BlockingClarification,
    CustomerAgentDecision,
    FollowUpPolicy,
)
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_view import AgentStateView
from app.schemas.comparison import ProductComparisonResult
from app.schemas.conversation import ConversationContext
from app.schemas.grounding import (
    GroundedProduct,
    SearchExecutionGrounding,
    TurnFailure,
)
from app.schemas.resolution import DeterministicClarification
from app.schemas.retailer import RetailerContext

MAX_RESPONSE_CHARS = 4000


class CustomerTurnInput(BaseModel):
    """Everything application code needs to handle one turn.

    `message` is the turn being handled and is deliberately not inside
    `conversation`: history is prior turns, so nothing downstream has to work
    out which entry is "now".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str = Field(min_length=1)
    conversation: ConversationContext = ConversationContext()
    state: AgentStateV1 = AgentStateV1()
    context: RetailerContext


class DecisionInput(BaseModel):
    """Everything the decision model is given, and nothing else.

    No `RetailerContext`, no `AgentStateV1`, no product id. The projection
    that produces `state_view` is built by the layer that calls the model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str = Field(min_length=1)
    conversation: ConversationContext = ConversationContext()
    state_view: AgentStateView = AgentStateView()


class TurnGrounding(BaseModel):
    """Verified outcomes of one turn, for the response layer to render.

    An envelope rather than one shape per action: a turn can ground a search
    and a comparison, or nothing at all, and the response layer should read
    one object either way.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    search: SearchExecutionGrounding | None = None
    product_detail: GroundedProduct | None = None
    comparison: ProductComparisonResult | None = None

    clarification: BlockingClarification | None = None
    """What the *model* decided to ask, wording included.

    Set only for a `CLARIFY` action. The coordinator carries it through and
    never writes one of its own.
    """

    deterministic_clarification: DeterministicClarification | None = None
    """What *application code* discovered it must ask, as reason codes only.

    A separate field rather than a second use of the one above, because the two
    differ in exactly the way that matters: this one has no wording yet, and
    filling `BlockingClarification.question` with coordinator prose is how a
    service starts writing conversation. The response layer phrases it.

    Never set alongside `clarification`. A turn asks one question, and which
    one is chosen by priority before this object is built: a model's own
    clarification outranks anything a service discovered afterwards.
    """

    failure: TurnFailure | None = None
    design_handoff_requested: bool = False
    """The turn asked for interior-design reasoning.

    A marker, not a request object: `InteriorDesignRequest` requires catalog
    capabilities that do not exist yet, and constructing a half-built one would
    be inventing the M12 boundary early. Execution is M12's.
    """

    follow_up_policy: FollowUpPolicy = FollowUpPolicy.NONE

    @model_validator(mode="after")
    def _one_question_at_most(self) -> Self:
        """A turn asks the customer for at most one thing.

        A turn can genuinely discover several unresolved details - an ambiguous
        selection, a budget with no currency - but asking about all of them is
        an interrogation, and choosing between them is a policy the coordinator
        applies before building this object, by priority rather than by which
        branch happened to run first.

        Enforced here rather than left to the response layer: by the time prose
        is written, two questions in hand is already the wrong input. An
        unasked question is not lost - the conversation continues, and a later
        turn can raise it again.
        """
        if self.clarification is not None and self.deterministic_clarification is not None:
            raise ValueError("a turn asks at most one question")
        asking = (
            self.clarification is not None
            or self.deterministic_clarification is not None
        )
        if asking and self.follow_up_policy is not FollowUpPolicy.NONE:
            # Nothing to follow up on: the turn is waiting for an answer.
            raise ValueError("a turn that asks a question offers no follow-up")
        if self.failure is not None and self.follow_up_policy is not FollowUpPolicy.NONE:
            # The operation did not complete, so there are no results to
            # invite a question about.
            raise ValueError("a failed turn offers no follow-up")
        return self


class CustomerTurnResult(BaseModel):
    """One handled turn: the state it produced, and what happened.

    **Application-only**, and `state` is deliberately not optional. Without
    persistence, an immutable state that is not returned is a state that is
    lost - so a turn whose search failed must still hand back the selection the
    customer made and the preferences they stated before it failed. Raising
    instead would discard them.

    That is the whole transaction: *old state + decision + whatever
    deterministically succeeded = new state*. There is no database transaction
    because there is nothing yet to persist.

    It carries no prose. What the customer is told is built from `grounding` by
    the response layer, which is why there is no message field for a coordinator
    to fill in.

    A provider failure *before* a valid decision exists produces no result at
    all: there is no decision to report and no operation to have succeeded, so
    that error propagates instead (CLAUDE.md 21).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: AgentStateV1
    decision: CustomerAgentDecision
    grounding: TurnGrounding


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
