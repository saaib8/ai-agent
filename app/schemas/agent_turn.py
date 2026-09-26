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
    BundleInteractionOp,
    CustomerAgentDecision,
    FollowUpPolicy,
)
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_view import AgentStateView
from app.schemas.bundle import BundleOptimizationOutcome
from app.schemas.bundle_action import BundleActionRequest
from app.schemas.comparison import ProductComparisonResult
from app.schemas.conversation import ConversationContext
from app.schemas.design import DesignGuidance
from app.schemas.grounding import (
    GroundedProduct,
    SearchExecutionGrounding,
    SelectionGrounding,
    TurnFailure,
)
from app.schemas.resolution import DeterministicClarification
from app.schemas.retailer import RetailerContext
from app.schemas.room_opener import RoomQuestion
from app.schemas.search_action import SearchActionRequest
from app.schemas.seating_solution import SeatingSolution

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

    bundle_action: BundleActionRequest | None = None
    """A screen-driven room edit, when the turn is one. Present means the turn
    is deterministic — resolve the ordinals, force the choice, re-optimise — and
    no decision model runs. `message` is kept for the conversation record only
    (CLAUDE.md 3.6)."""

    search_action: SearchActionRequest | None = None
    """A screen-driven search follow-up, when the turn is one. Present means the
    turn is deterministic — re-run the search in progress while excluding what
    was already shown, or the one product turned down — and no decision model
    runs. `message` is kept for the conversation record only (CLAUDE.md 3.6)."""


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
    selection: SelectionGrounding | None = None
    """What they have chosen, when they asked to see it. Never a search."""

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

    design_guidance: tuple[DesignGuidance, ...] = ()
    """What the design specialist answered, when the turn was a design question.

    Carried whole rather than summarised: `DesignGuidance` is already a safe
    shape - a topic, a summary that contains no digits by validation, and any
    figures as tagged measurements - so projecting it again would be a second
    place the same words could be trimmed differently (CLAUDE.md 4, 41).

    It is design knowledge, true of rooms in general and of none in particular.
    Nothing here is a claim about a product, a price or this retailer's stock.
    """

    design_handoff_requested: bool = False
    """The turn **asked** for interior-design reasoning.

    Request metadata, and only that. It says what the customer wanted, never
    what came of it: a handoff that executed and one that failed before the
    optimiser both set it. What happened is `bundle_outcome`, and a reader
    deciding how to answer must look there rather than here (M12E-2).
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
        asking = self.clarification is not None or self.deterministic_clarification is not None
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

    bundle_change: BundleInteractionOp | None = None
    """The local room edit this turn made, if any.

    Application-only, beside `bundle_outcome` rather than inside it: locking a
    piece produces no `RoomBundle`, and manufacturing one would invent a status
    and a feasibility claim nobody established.
    """

    selected_kinds: tuple[str, ...] = ()
    """What kinds of thing the customer has chosen, in the order chosen.

    Customer words, read fresh from the catalog. Carried so the reply can name
    them instead of guessing: given a bare count and a screen full of sofas it
    once called a sofa and a centre table "2 sofas" (M19 2).
    """

    selection_added: bool = False
    """Whether this turn recorded a product the customer settled on.

    Computed by the coordinator from the state before and after, because a
    choice can be expressed several ways - an explicit interaction, or
    settling on the piece a complement is built around - and what the reply
    needs to know is whether anything was recorded, not which route recorded
    it.

    It exists so a reply cannot claim a choice that did not happen. One did
    (M17 2).
    """

    bundle_outcome: BundleOptimizationOutcome | None = None
    """What the whole-room execution produced, if it got that far.

    Here rather than on `TurnGrounding` for one concrete reason: it carries
    `ProductCandidate`, and a grounded product deliberately holds no product id
    so that prose can only cite a turn-local handle. Putting an id-bearing shape
    inside the grounding would break that, and a guard says so. This object
    already holds the whole state, ids included, and is never serialised
    anywhere near a model.

    Present for a bundle, a partial bundle, an infeasible one and a typed
    refusal alike: each is a different thing to say, and collapsing them would
    lose the distinction before anyone could use it. `None` alongside
    `grounding.design_handoff_requested` means execution stopped earlier, and
    `grounding.failure` says why. M12E-3 owns the model-safe projection.
    """

    seating_solution: SeatingSolution | None = None
    """A seating combination composed when no single product met a seat count.

    Here rather than on `TurnGrounding` for the same reason as `bundle_outcome`:
    it carries product ids for the application to render, and the grounding is
    kept free of ids so prose can only cite a turn-local handle. The response
    model is given a count-only projection of it, never this (CLAUDE.md 20.4).
    """

    offered_instead_of: str | None = None
    """The seating type they asked for, when it never seats that many and the
    cards are another type that does."""

    room_seats: int | None = Field(default=None, ge=1)
    """How many the room's seating really seats, counted from its pieces - so
    a reply never says "seating for all nine" about a room that seats eight."""

    room_question: RoomQuestion | None = None
    """This turn's question about a room being designed, and the pieces offered
    as chips when it asks for them (CLAUDE.md 10.1)."""


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
