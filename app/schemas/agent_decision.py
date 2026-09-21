"""What the Customer/Commerce Agent is allowed to decide.

The model reasons about the conversation and returns exactly this. Everything
authoritative is then produced by application code from what it said.

The boundary that matters most: **a decision never contains a product id.** A
customer saying "I like the second one" becomes a *selector* - position two of
what was presented - and the application resolves that against state it wrote
itself. A model that could emit `165645` could emit `165646`, and no membership
check afterwards can tell a real reference from a plausible one.

For the same reason a decision is not an `AgentStateUpdate`. State mutations
are built by the coordinator from resolved facts; the model proposes, and the
reducer decides whether the result is a state that may exist.

Bounded, not planned: one execution action, at most one interaction, at most
one clarification, no nesting. A decision cannot describe a sequence of steps,
so nothing here can grow into a tool loop.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.agent_state import MAX_SEMANTIC_INTENT_CHARS, PurchaseStage
from app.schemas.product_reference import (
    ExtremumDirection,
    FocusedProduct,
    PresentedAttributeMatch,
    PresentedExtremum,
    PresentedOrdinal,
    ProductReferenceSelector,
    SoleSelectedProduct,
)
from app.schemas.query import SemanticPreference
from app.schemas.refinement import SearchRefinementDelta, SemanticIntentRefinement

MAX_CLARIFICATION_CHARS = 300


class AgentAction(StrEnum):
    """What this turn executes. One per turn."""

    ANSWER = "answer"
    """Answerable from state and history. States no current catalog fact."""

    CLARIFY = "clarify"
    """Continuing would be incorrect, misleading or impossible."""

    SEARCH = "search"
    """A genuinely new product search; M7 interprets the language."""

    REFINE_SEARCH = "refine_search"
    PRODUCT_DETAIL = "product_detail"
    """A current fact about one product. Requires fresh hydration."""

    COMPARE = "compare"
    DESIGN_HANDOFF = "design_handoff"
    """A typed intent only. Nothing executes it in V1."""


class CommercialReason(StrEnum):
    """Why this turn is commercially worth doing.

    Kept apart from the action because the same capability serves several
    motives: an upsell and a plain request are both a search. Folding the
    motive into the action would mean a new execution path per sales concept.

    Transient turn reasoning - never persisted to `AgentStateV1`.
    """

    CUSTOMER_REQUEST = "customer_request"
    ALTERNATIVE = "alternative"
    UPSELL = "upsell"
    ADDRESS_OBJECTION = "address_objection"
    PURCHASE_PROGRESSION = "purchase_progression"


class FollowUpPolicy(StrEnum):
    """Whether an optional question after the results would be welcome.

    A directive, not a question: the highest-value thing to ask depends on what
    the search returned, so the wording is written after execution, not here.
    """

    NONE = "none"
    OPTIONAL = "optional"


# ── product interactions ────────────────────────────────────────────────────


class ProductInteractionOp(StrEnum):
    """State edits that need no execution beyond resolving the reference.

    `DETAIL` is deliberately absent: asking what a product costs requires
    fresh hydration and drives the whole reply, so it is the primary action
    `PRODUCT_DETAIL`, not a side effect (locked M11A). Focus follows from it
    as a derived consequence rather than as a second request.
    """

    SELECT = "select"
    DESELECT = "deselect"
    FOCUS = "focus"


class ProductInteractionIntent(BaseModel):
    """At most one per turn: two would need an order and could contradict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: ProductInteractionOp
    reference: ProductReferenceSelector


# ── proposals ───────────────────────────────────────────────────────────────


class PreferenceProposalOp(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"


class PreferenceProposal(BaseModel):
    """Reusable preferences the customer stated about themselves.

    Order-independent by construction, so no two proposals can disagree about
    which came first.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: PreferenceProposalOp
    preferences: tuple[SemanticPreference, ...] = ()

    @model_validator(mode="after")
    def _add_and_remove_need_values(self) -> Self:
        if self.op is not PreferenceProposalOp.REPLACE and not self.preferences:
            raise ValueError(f"a preference {self.op} operation needs values")
        return self


class PriceProposal(BaseModel):
    """A money figure the customer stated, before validation.

    Strings, so an amount never passes through a binary float. The currency is
    None when they named an amount without one; the application resolves it
    and never guesses.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_amount: str | None = None
    max_amount: str | None = None
    currency: str | None = None

    @model_validator(mode="after")
    def _needs_a_bound(self) -> Self:
        if self.min_amount is None and self.max_amount is None:
            raise ValueError("a price proposal needs at least one bound")
        return self


class CustomerStateProposal(BaseModel):
    """Facts the customer stated about themselves or their room.

    Every field here is something they said in their own words. That is why
    `semantic_intent` is absent - it is search-affecting and must be staged
    with the candidate search - and why `purchase_stage` is absent: it is the
    system's own read, and filing an inference beside stated facts would make
    the two indistinguishable later.

    "Show me modern sofas" belongs to the search, not here. Only a customer
    generalising about themselves - "I usually prefer Modern" - proposes a
    reusable preference.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_preferences: PreferenceProposal | None = None
    room_type: str | None = None
    clear_room_type: bool = False
    room_budget: PriceProposal | None = None
    clear_room_budget: bool = False
    design_preferences: PreferenceProposal | None = None

    @model_validator(mode="after")
    def _setting_and_clearing_are_exclusive(self) -> Self:
        if self.clear_room_type and self.room_type is not None:
            raise ValueError("room_type cannot be set and cleared in one turn")
        if self.clear_room_budget and self.room_budget is not None:
            raise ValueError("room_budget cannot be set and cleared in one turn")
        return self


class DerivedCommerceProposal(BaseModel):
    """The system's own commercial read. Inferred, never stated.

    Separate from `CustomerStateProposal` so the distinction survives in the
    contract rather than in a comment: an inference that became durable state
    beside the customer's own words would be impossible to tell apart from
    something they told us.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    purchase_stage: PurchaseStage | None = None
    clear_purchase_stage: bool = False

    @model_validator(mode="after")
    def _setting_and_clearing_are_exclusive(self) -> Self:
        if self.clear_purchase_stage and self.purchase_stage is not None:
            raise ValueError("purchase_stage cannot be set and cleared in one turn")
        return self


class NewSearchProposal(BaseModel):
    """What the model may add to a genuinely new search.

    Almost nothing, deliberately. M7 remains the authority on what the
    customer's words mean, including every taxonomy value, so the only thing
    left to propose is the durable fuzzy wording no structured field holds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    semantic_intent: SemanticIntentRefinement | None = None


# ── clarification ───────────────────────────────────────────────────────────


class BlockingClarificationReason(StrEnum):
    """Why nothing useful can run until the customer answers.

    Distinct from an optional follow-up: these are cases where proceeding
    would be incorrect, misleading or impossible, not cases where a question
    would merely be interesting.

    **Model-facing.** Every member here is emittable by the decision model, so
    a reason belongs in this enum only if the model can legitimately know it
    from language and safe state alone. Whether a requirement is executable
    against this catalog is not such a thing: only query understanding
    establishes it, and only after it has run. Those reasons live in
    `SearchRequirementClarificationReason`, where no model can reach them.
    """

    INSUFFICIENT_PRODUCT_TYPE = "insufficient_product_type"
    MULTIPLE_PRODUCT_TYPES = "multiple_product_types"
    MISSING_PRICE_CURRENCY = "missing_price_currency"
    MISSING_REFINEMENT_CURRENCY = "missing_refinement_currency"
    MISSING_DIMENSION_ROLE = "missing_dimension_role"
    MISSING_DIMENSION_UNIT = "missing_dimension_unit"
    AMBIGUOUS_PRODUCT_REFERENCE = "ambiguous_product_reference"
    AMBIGUOUS_COMPARATIVE_REFERENCE = "ambiguous_comparative_reference"
    UNDEFINED_QUALITY_CRITERION = "undefined_quality_criterion"
    """"more premium" with no criterion the catalog can actually honour."""

    COMPARISON_TARGETS = "comparison_targets"


class BlockingClarification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: BlockingClarificationReason
    question: str = Field(min_length=1, max_length=MAX_CLARIFICATION_CHARS)


# ── the decision ────────────────────────────────────────────────────────────

MIN_COMPARISON_REFERENCES = 2
MAX_COMPARISON_REFERENCES = 4
"""A schema ceiling, not a product setting.

Configuration may choose a smaller maximum; nothing may choose a larger one.
A comparison of nine products is not a comparison, and an unbounded tuple is
a way to push an arbitrary payload through a validated contract."""


class CustomerAgentDecision(BaseModel):
    """One turn's decision. Validated into exactly one coherent shape.

    A single model with an action discriminator rather than a union of seven:
    the provider's structured-output schema takes one type, and this repository
    already validates shape-by-kind this way in `DimensionConstraint`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: AgentAction
    commercial_reason: CommercialReason | None = None

    new_search: NewSearchProposal | None = None
    refinement: SearchRefinementDelta | None = None
    taxonomy_change_requested: bool = False
    """The customer changed the product type. A boolean, not a value: naming
    the new type is M7's job, so the model cannot invent one here."""

    reference: ProductReferenceSelector | None = None
    """The one product this turn is about. What that means depends on the action.

    * `PRODUCT_DETAIL` - the product they want a current fact about.
    * `SEARCH` - **find structurally similar alternatives to this product.**
      "Something like the beige one" is a new search seeded from a product
      rather than from their words, so the reference is what distinguishes it
      from an ordinary new search, which carries none.
    * `DESIGN_HANDOFF` - the anchor piece a room is to be designed around.

    A search's reference is the *only* deterministic signal for the alternatives
    route. `commercial_reason` describes motive and never routes: an upsell and
    a request for alternatives are both searches, and reading intent out of a
    motive field would make the same decision execute two different ways.
    """

    comparison_references: tuple[ProductReferenceSelector, ...] = ()
    clarification: BlockingClarification | None = None

    interaction: ProductInteractionIntent | None = None
    state_proposal: CustomerStateProposal | None = None
    commerce_proposal: DerivedCommerceProposal | None = None
    follow_up_policy: FollowUpPolicy = FollowUpPolicy.OPTIONAL

    @model_validator(mode="after")
    def _payload_matches_the_action(self) -> Self:
        self._check_search_payloads()
        self._check_reference_payloads()
        self._check_clarification()
        self._check_interaction()
        return self

    # ── the per-action rules ────────────────────────────────────────────────

    def _check_search_payloads(self) -> None:
        if self.new_search is not None and self.action is not AgentAction.SEARCH:
            raise ValueError("only a search may carry a new-search proposal")
        if (
            self.action is AgentAction.SEARCH
            and self.reference is not None
            and self.new_search is not None
        ):
            # A search seeded from a product is built structurally from that
            # product; a durable intent proposed beside it would have to be
            # combined with the seed, and V1 has no composite planner to
            # decide how. Refused rather than silently dropping one of them.
            raise ValueError(
                "a search for alternatives carries no new-search proposal"
            )
        if self.action is AgentAction.REFINE_SEARCH:
            if self.refinement is None and not self.taxonomy_change_requested:
                raise ValueError(
                    "a refinement needs a delta or a taxonomy change"
                )
            if self.refinement is not None and self.refinement.is_empty():
                raise ValueError("a refinement delta that changes nothing is not one")
        elif self.refinement is not None or self.taxonomy_change_requested:
            raise ValueError("only a refinement may carry a refinement payload")

    def _check_reference_payloads(self) -> None:
        if self.action is AgentAction.PRODUCT_DETAIL and self.reference is None:
            raise ValueError("a product detail needs a reference")
        if self.action is AgentAction.COMPARE:
            if len(self.comparison_references) < MIN_COMPARISON_REFERENCES:
                raise ValueError(
                    f"a comparison needs at least {MIN_COMPARISON_REFERENCES} references"
                )
            if len(self.comparison_references) > MAX_COMPARISON_REFERENCES:
                raise ValueError(
                    f"a comparison covers at most {MAX_COMPARISON_REFERENCES} products"
                )
            if len(set(self.comparison_references)) != len(self.comparison_references):
                raise ValueError("a comparison must not repeat a reference")
        elif self.comparison_references:
            raise ValueError("only a comparison may carry comparison references")
        references_allowed = (
            AgentAction.PRODUCT_DETAIL,
            AgentAction.SEARCH,
            AgentAction.DESIGN_HANDOFF,
        )
        if self.reference is not None and self.action not in references_allowed:
            raise ValueError(f"{self.action} may not carry a product reference")

    def _check_clarification(self) -> None:
        if self.action is AgentAction.CLARIFY:
            if self.clarification is None:
                raise ValueError("a clarification action needs a question")
            if self.follow_up_policy is not FollowUpPolicy.NONE:
                # Nothing executed, so there are no results to follow up on.
                raise ValueError("a clarification turn has no optional follow-up")
            if self.interaction is not None:
                raise ValueError("a clarification turn changes nothing")
        elif self.clarification is not None:
            raise ValueError("only a clarification action may carry a question")

    def _check_interaction(self) -> None:
        if self.interaction is None:
            return
        if (
            self.interaction.op is ProductInteractionOp.DESELECT
            and self.action is AgentAction.PRODUCT_DETAIL
        ):
            # Both resolve against the pre-turn state, so they can name the
            # same product: deselecting it removes the only thing making it
            # known, and the detail's implied focus would then point at a
            # product that is neither presented nor selected - a state that
            # cannot exist. Forbidding the pair is the smallest fix that keeps
            # every other locked rule intact.
            raise ValueError("a product detail cannot also deselect a product")
        if self.interaction.op is ProductInteractionOp.FOCUS and self.action in (
            AgentAction.SEARCH,
            AgentAction.REFINE_SEARCH,
        ):
            # Committing results clears focus by design, so the pair is
            # contradictory rather than merely ordered.
            raise ValueError("focus cannot accompany a search that replaces results")


__all__ = [
    "MAX_CLARIFICATION_CHARS",
    "MAX_COMPARISON_REFERENCES",
    "MAX_SEMANTIC_INTENT_CHARS",
    "MIN_COMPARISON_REFERENCES",
    "AgentAction",
    "BlockingClarification",
    "BlockingClarificationReason",
    "CommercialReason",
    "CustomerAgentDecision",
    "CustomerStateProposal",
    "DerivedCommerceProposal",
    "ExtremumDirection",
    "FocusedProduct",
    "FollowUpPolicy",
    "NewSearchProposal",
    "PreferenceProposal",
    "PreferenceProposalOp",
    "PresentedAttributeMatch",
    "PresentedExtremum",
    "PresentedOrdinal",
    "PriceProposal",
    "ProductInteractionIntent",
    "ProductInteractionOp",
    "ProductReferenceSelector",
    "SoleSelectedProduct",
]
