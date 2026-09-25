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

from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import BlockingClarificationReason, FollowUpGoal
from app.schemas.bundle import BundleStatus, BundleUnavailableReason, UnmetReason
from app.schemas.comparison import MIN_COMPARED_PRODUCTS, ComparisonField
from app.schemas.conversation import ConversationContext
from app.schemas.design import DesignGuidance
from app.schemas.grounding import TurnFailureCode
from app.schemas.relaxation import RelaxableField, SetAsideOption
from app.schemas.resolution import (
    DeterministicClarification,
    ReferenceFailureReason,
    RelativePriceFailureReason,
    SearchRequirementClarificationReason,
)
from app.schemas.screen import CustomerVisibleScreenView
from app.schemas.seating_solution import SeatingSolutionOutcome
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
    ROOM_BUNDLE = "room_bundle"
    """A whole room was selected - complete, partial or infeasible alike.

    Only a real `RoomBundle`. A refusal to compute one carries no package to
    frame, so it is answered deterministically instead.
    """

    SEATING_COMBINATION = "seating_combination"
    """No single product met a seat count, so pieces were combined to reach it.

    The salesperson move made a turn: a customer who asked for one sofa that
    seats eight, where none does, is shown combinations that together do rather
    than a dead end (CLAUDE.md 27). Only the two outcomes there is something to
    say about reach here - real combinations, or an honest "the closest is over
    budget" - so the model always has either cards to frame or a shortfall to
    own.
    """

    SELECTION = "selection"
    """The customer's own choices, shown again.

    Not `SEARCH_RESULTS`: nothing was searched for, so a reply must not talk
    about what it found or how it narrowed (M17 3).
    """

    DESIGN_ADVICE = "design_advice"
    """A design question, answered from the specialist's reasoning.

    Words and no cards. The guidance on the view is what the reply is written
    from, and it is general knowledge rather than anything about a product this
    retailer sells (CLAUDE.md 36, 41).
    """

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
    """A handoff that produced no bundle. A model given an empty grounding here
    would be invited to improvise about a room nobody planned."""

    BUNDLE_KEPT = "bundle_kept"
    """A piece of the room was locked. Fixed wording: nothing was searched,
    nothing was priced, and a model given this would be inventing a change."""

    BUNDLE_UNLOCKED = "bundle_unlocked"
    """A piece may now change later. **Permission, not a change** - wording a
    model chose could easily promise a replacement that did not happen."""

    BUNDLE_ACQUISITION_SET = "bundle_acquisition_set"
    """The customer said whether they already have a piece, or still need it.

    Its own branch because it is its own fact. Before this existed, anything
    that was not a lock fell through to `BUNDLE_UNLOCKED` - so "I already own
    the rug" was answered with "that piece can change in later refinements",
    which was the opposite of what had just been recorded: the line was marked
    owned *and* locked.

    The wording is selected by `acquisition`, the way `BUNDLE_UNAVAILABLE`
    selects on its reason, rather than by a second enum member per value.
    """

    BUNDLE_CHANGED_NOT_REFRESHED = "bundle_changed_not_refreshed"
    """The change was made; the room could not be worked out again.

    Two truths in one turn, and the wording has to carry both. A model given
    this would have to guess which half mattered, and could easily report a
    change that happened as one that did not.
    """

    BUNDLE_UNAVAILABLE = "bundle_unavailable"
    """The optimiser refused to compute, and said exactly why.

    A deterministic outcome with a controlled reason, not an infrastructure
    failure and not a catalog verdict. The reason selects fixed wording; a
    model asked to explain it would start guessing at remedies.
    """


class DeterministicResponse(BaseModel):
    """A branch that answers without reaching a model. Application-only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: DeterministicResponseKind
    failure_code: TurnFailureCode | None = None
    """Never model-visible. It selects fixed wording, and that is all."""

    acquisition: BundleAcquisition | None = None
    """Which way the customer settled it. Selects fixed wording, nothing more."""

    bundle_reason: BundleUnavailableReason | None = None
    """Why no bundle could be computed. Selects fixed wording, nothing more.

    Deliberately the optimiser's own enum rather than a parallel one: a second
    vocabulary would have to be kept in step with the first, and the two would
    eventually disagree about what a refusal meant.
    """

    @model_validator(mode="after")
    def _each_kind_carries_its_own_detail(self) -> Self:
        if (self.kind is DeterministicResponseKind.HANDLED_FAILURE) != (
            self.failure_code is not None
        ):
            raise ValueError("a failure carries its code, and only a failure does")
        if (self.kind is DeterministicResponseKind.BUNDLE_ACQUISITION_SET) != (
            self.acquisition is not None
        ):
            # Required rather than defaulted: a missing acquisition would have
            # to pick a sentence, and either choice would be a guess about what
            # the customer said.
            raise ValueError("an acquisition update states which way, and only it does")
        if (self.kind is DeterministicResponseKind.BUNDLE_UNAVAILABLE) != (
            self.bundle_reason is not None
        ):
            raise ValueError("an unavailable bundle carries its reason, and only it does")
        return self


class BundleGroundingView(BaseModel):
    """What one whole-room outcome looks like to the response model.

    Enums, bools and counts. No price, no total, no budget figure, no product,
    no identity, no rank, no relaxation depth and no need index - the same rule
    the rest of `ResponseGroundingView` follows, because the model frames and
    the application states facts.

    What is deliberately **absent**: fulfilled counts per priority. A
    `RoomBundle` line does not carry the priority of the need it filled, and
    recovering it would mean re-reading a design plan this layer does not have.
    Inferring it from a category, a position or a lock would be inventing it.
    Unmet counts *are* provable - `UnmetNeed` carries its own priority - so
    those are here and their complement is not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: BundleStatus

    bundle_line_count: int = Field(default=0, ge=0)
    locked_line_count: int = Field(default=0, ge=0)
    already_owned_line_count: int = Field(default=0, ge=0)
    """Lines, never units: four of one product is one line. The names say so."""

    required_unmet_count: int = Field(default=0, ge=0)
    recommended_unmet_count: int = Field(default=0, ge=0)
    optional_unmet_count: int = Field(default=0, ge=0)

    unmet_reasons: tuple[UnmetReason, ...] = ()
    """Why pieces are missing, deduplicated in first-seen order.

    Reason codes, so "the shop stocks none" and "the budget would not stretch"
    stay different things to say. No need index and no product type: `UnmetNeed`
    carries neither a category nor a label, and naming the missing piece would
    mean inventing one.
    """

    budget_supplied: bool = False
    within_budget: bool | None = None
    """Whether the package obeys a budget the customer actually gave.

    `None` when they gave none - there is nothing to be inside, and saying
    "within budget" about an absent budget would be a claim from nowhere.
    """

    relaxed_line_count: int = Field(default=0, ge=0)
    """How many selected pieces needed a widened search. A count, never a
    depth: "some pieces required a wider search" is sayable, and by how much
    is not."""

    @model_validator(mode="after")
    def _budget_claims_need_a_budget(self) -> Self:
        if not self.budget_supplied and self.within_budget is not None:
            raise ValueError("no budget was given, so nothing can be within it")
        if self.budget_supplied and self.within_budget is None:
            raise ValueError("a supplied budget is either met or not")
        if self.status is BundleStatus.INFEASIBLE and self.within_budget is not False:
            raise ValueError("an infeasible package does not fit its budget")
        return self

    @model_validator(mode="after")
    def _counts_fit_inside_the_bundle(self) -> Self:
        for name, count in (
            ("locked_line_count", self.locked_line_count),
            ("already_owned_line_count", self.already_owned_line_count),
            ("relaxed_line_count", self.relaxed_line_count),
        ):
            if count > self.bundle_line_count:
                raise ValueError(f"{name} cannot exceed bundle_line_count")
        return self

    @model_validator(mode="after")
    def _completeness_means_no_required_gap(self) -> Self:
        """The one thing a reader must not be able to get wrong.

        Complete means every required need was satisfied; it does not mean
        nothing is missing, which is why recommended and optional gaps are
        reported beside it rather than folded into the status.
        """
        if self.status is BundleStatus.COMPLETE and self.required_unmet_count:
            raise ValueError("a package missing a required piece is not complete")
        if self.status is BundleStatus.PARTIAL and not self.required_unmet_count:
            raise ValueError("a partial package is short of a required piece")
        return self


class SeatingSolutionGroundingView(BaseModel):
    """What a composed seating combination looks like to the response model.

    Counts and one enum. No price, no total, no per-piece detail and no product:
    the pieces of each combination and what they cost are rendered by the
    application, exactly as a whole room's are, so none of it passes through
    here (CLAUDE.md 20.4).

    `target_seats` is the count the customer asked for, carried so the reply can
    name it - "a set that seats eight" - without the number being an invention.
    It is their own figure, and the numeric guard admits it as such.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: SeatingSolutionOutcome
    target_seats: int = Field(ge=1)
    bundle_count: int = Field(default=0, ge=0)
    """How many combinations are on screen. Zero when none fit the budget - the
    honest outcome, where the reply owns the shortfall and shows nothing."""

    budget_supplied: bool = False

    @model_validator(mode="after")
    def _only_the_outcomes_worth_wording(self) -> Self:
        """Two outcomes reach the model, and each pairs with its evidence.

        `SINGLE_PIECE_SUFFICES` and `NO_SEATING` are handled before a model is
        ever involved: the first is an ordinary search, the second an ordinary
        zero result. A view carrying either would ask the model to frame a
        combination that was never composed.
        """
        if self.outcome not in (
            SeatingSolutionOutcome.BUNDLES,
            SeatingSolutionOutcome.NONE_WITHIN_BUDGET,
        ):
            raise ValueError("only a composed or over-budget outcome reaches the model")
        if (self.outcome is SeatingSolutionOutcome.BUNDLES) != (self.bundle_count > 0):
            raise ValueError("combinations are on screen exactly when the outcome is BUNDLES")
        return self


_SEARCH_KINDS = frozenset({ResponseOutcomeKind.SEARCH_RESULTS, ResponseOutcomeKind.ZERO_RESULTS})
"""The two outcomes an executed search produces, either of which may carry
search provenance. A detail, a comparison or a room ran no search."""


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

    commerce_category: str | None = None
    commerce_subcategory: str | None = None
    """What kind of thing is on screen, in **customer-facing words**.

    Added so a reply can be about something. Without it the model knew only
    that five results existed, which is how every search came back as "here's
    what I found" - true, and no use to anyone.

    Application-owned and verified: it is the category the search actually
    executed against, validated against the registry, not a guess from a
    product name. It names a *kind*, never a product - no id, no name, no
    price - so nothing here can become a claim about an item (CLAUDE.md 20.4).

    Carried as words rather than as the stored taxonomy value. The registry
    key is an internal identifier, and a model shown `lounge-chair` writes
    `lounge-chair` - which is how a customer who had asked about sofas came to
    be told there were no matching "lounge-chair options". The transformation
    is mechanical, not a second vocabulary: hyphens become spaces and nothing
    is renamed, so the registry stays the one source of truth (CLAUDE.md 14.1)
    and no taxonomy value can be spelled a second way here.
    """

    wished_colour_matches: int | None = Field(default=None, ge=0)
    wished_style_matches: int | None = Field(default=None, ge=0)
    """How many cards on screen carry a colour (or style) the customer asked or
    wished for. None when they named none.

    Without it, a reply to "make them red" beside black, gold and white tables
    said red was "the deciding factor": the model had no way to tell that the
    closest products shown were not the colour asked for. A count is enough to
    stop that, and says nothing about which product or price.
    """

    exact_match_count: int = Field(default=0, ge=0)
    """How many products satisfied the customer's request *as they made it*.

    The one figure that makes a widened search explainable. Without it the
    reply could say only that something was broadened - a statement about
    machinery that the customer cannot check against the cards, and which read
    as a change when the five products on screen had not moved. With it, the
    reply can say the thing that is actually true of what they are looking at:
    that one piece meets the requirement exactly and the rest are near it
    (CLAUDE.md 13.4).

    Catalog-wide for this request, not a count of what is presented. A count,
    never a bound: the figure the customer named is theirs, and this is only
    how many products met it.
    """

    search_was_suggested: bool = False
    """Whether this set is something we proposed rather than something they
    asked for.

    A complementary suggestion runs the same pipeline as any other search, so
    by the time a reply is worded the two are indistinguishable - which is how
    an errand the customer never sent came to be reported to them as a failed
    search. A proposed set has to be introduced; a proposed set that found
    nothing is a passing remark at most, because there was no request for it to
    have failed.
    """

    selected_count: int = Field(default=0, ge=0)
    """How many products the customer has settled on, after this turn.

    Fact, not memory. Asked "what have I selected?", the reply used to answer
    from the conversation - and told a customer they had chosen a sofa and a
    rug when only the sofa was ever recorded (M17 2).

    A count, so it names no product: which ones they are is rendered from
    verified records when they ask to see them.
    """

    selected_kinds: tuple[str, ...] = ()
    """What kinds of thing those choices are, in customer words.

    A count alone is not sayable. Given "2 choices" and a screen full of sofas,
    the reply said **"you now have 2 sofas recorded"** when it was one sofa and
    one centre table - the model had a number and no nouns, so it borrowed the
    nearest ones (M19 2).

    One entry per choice, in the order they were chosen, so a repeated kind
    appears twice and the count and the kinds always agree. Registry words,
    never keys, and never a product name.
    """

    selection_changed: bool = False
    """Whether this turn added one.

    The difference between "I've got that as your choice" and a claim with
    nothing behind it. A turn that recorded nothing may not say it did.
    """

    seating_requirement_known: bool = False
    """Whether the customer has already said how many people use the room.

    A bool, not the number: it exists so the reply does not ask again, and
    stating the figure is the application's job.
    """

    follow_up_goal: FollowUpGoal | None = None
    """What the optional question should be about, chosen by the decision step.

    The subject only. The model writes the sentence, which is why this is an
    enum and not prose.
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

    earlier_sizes_applied: bool = False
    """Sizes the customer gave earlier for this product type were applied
    again. A flag, not the figures: the customer said them, and the reply only
    has to remind them the limit is still in force."""

    would_find_without: tuple[SetAsideOption, ...] = ()
    """Nothing met everything together: how many products each requirement,
    set aside alone, would find. Counts and a field name - never a product or
    a bound - so the reply can offer a real next step without inventing one."""

    compared_count: int = Field(default=0, ge=0)
    comparison_differs_on: tuple[ComparisonField, ...] = ()
    """Which fields differ. Never a cell, so "they differ mainly on width" is
    sayable and "one is 20 cm wider" is not."""

    bundle: BundleGroundingView | None = None
    """The whole-room outcome, for `ROOM_BUNDLE` and nothing else."""

    seating: SeatingSolutionGroundingView | None = None
    """The composed combination, for `SEATING_COMBINATION` and nothing else."""

    guidance: tuple[DesignGuidance, ...] = ()
    """The design specialist's answer, for `DESIGN_ADVICE`.

    The one place the response model is given something to *say* rather than
    something to frame. It carries no product, no price and no stock claim, so
    a reply written from it is design knowledge and never a statement about
    what this retailer has (CLAUDE.md 41).
    """

    screen: CustomerVisibleScreenView = CustomerVisibleScreenView()
    """What the customer is looking at while they read this reply.

    Projected from the same objects the presentation payload is built from, so
    a fact stated in prose and a fact printed on a card are the same fact
    (CLAUDE.md 2, 10).

    This is what lets a reply be *about* something: "the second one seats five"
    rather than "here are five options". It carries merchandise and no
    identity - no id, no store, no score, no url - so a model that reads it
    still cannot name a product to the backend (CLAUDE.md 6).

    Empty on a turn that shows nothing, which reads correctly: an answer with
    no cards beside it should not talk about cards.
    """

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
            self.reference_reason is not None or self.relative_price_reason is not None
        ):
            raise ValueError("a detail reason needs the reason it details")

        if (self.kind is ResponseOutcomeKind.ROOM_BUNDLE) != (self.bundle is not None):
            raise ValueError("a room bundle outcome carries its bundle, and only it does")

        if (self.kind is ResponseOutcomeKind.SEATING_COMBINATION) != (self.seating is not None):
            raise ValueError("a seating combination carries its solution, and only it does")

        if self.selected_kinds and len(self.selected_kinds) != self.selected_count:
            raise ValueError("every choice is one kind, so the two counts agree")

        if (self.kind is ResponseOutcomeKind.DESIGN_ADVICE) != bool(self.guidance):
            raise ValueError("design advice carries guidance, and only it does")

        if self.kind is ResponseOutcomeKind.ZERO_RESULTS and self.presented_count:
            raise ValueError("a zero-result search presents nothing")
        if self.kind is ResponseOutcomeKind.SELECTION and not self.presented_count:
            raise ValueError("a selection outcome shows something")
        if self.kind is ResponseOutcomeKind.SEARCH_RESULTS and not self.presented_count:
            raise ValueError("a results outcome presents something")

        comparing = self.kind is ResponseOutcomeKind.COMPARISON
        if comparing and self.compared_count < MIN_COMPARED_PRODUCTS:
            raise ValueError("a comparison covers at least two products")
        if not comparing and (self.compared_count or self.comparison_differs_on):
            raise ValueError("only a comparison carries comparison detail")

        if self.was_relaxed != bool(self.relaxed_fields):
            raise ValueError("was_relaxed must match the fields recorded")

        searched = self.kind in _SEARCH_KINDS
        if not searched and (self.exact_match_count or self.search_was_suggested):
            raise ValueError("only a search carries search provenance")
        # An unwidened search presented products from the exact pool, so the
        # exact count cannot be smaller than what is on screen. Widened, it
        # freely can - that gap is the whole reason the figure is carried.
        if (
            searched
            and not self.was_relaxed
            and self.exact_match_count < self.presented_count
        ):
            raise ValueError("an unwidened search presents only exact matches")

        for words in (self.commerce_category, self.commerce_subcategory):
            # The registry key is an internal identifier; a model shown one
            # writes it back verbatim.
            if words is not None and "-" in words:
                raise ValueError("a category reaches the model as words, not a key")
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
