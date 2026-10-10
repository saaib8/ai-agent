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

from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import SizeSettings
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import BlockingClarificationReason, FollowUpGoal
from app.schemas.agent_state import SwapBudgetOfferStage
from app.schemas.bundle import BundleStatus, BundleUnavailableReason, UnmetReason
from app.schemas.comparison import MIN_COMPARED_PRODUCTS, ComparisonField
from app.schemas.conversation import ConversationContext
from app.schemas.design import DesignCategoryNeed, DesignGuidance, DesignPriority
from app.schemas.grounding import TurnFailureCode
from app.schemas.next_step import NextStepKind
from app.schemas.query import RankingLean
from app.schemas.relaxation import RelaxableField, SetAsideOption
from app.schemas.resolution import (
    DeterministicClarification,
    ReferenceFailureReason,
    RelativePriceFailureReason,
    SearchRequirementClarificationReason,
)
from app.schemas.room_opener import RoomQuestionKind
from app.schemas.screen import CustomerVisibleScreenView, PresentedCardView
from app.schemas.seating_solution import (
    SeatingShape,
    SeatingShapeOption,
    SeatingSolutionOutcome,
)
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.briefs import OPENING_QUESTIONS, BriefQuestionKind
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

    ROOM_QUESTION = "room_question"
    """One question before a room is designed - its budget, its pieces (as
    chips), how many will sit, or the colours they like (CLAUDE.md 10.1)."""

    ROOM_SWAP_OFFER = "room_swap_offer"
    """A dearer swap that broke the budget, put to the customer as a yes/no.

    The salesperson beat: affirm the room warmly, be honest that it runs a
    little over budget (the card shows the figures - the reply names no
    number), and ask whether to stretch the budget or stay within it. Its
    second stage asks instead whether to look for a cheaper piece
    (CLAUDE.md 27)."""

    PRODUCT_BRIEF = "product_brief"
    """They stated a need, and a card of short questions is shown beneath the
    reply before anything is searched (CLAUDE.md 10.4). The card asks; the
    reply only introduces it."""

    GOES_WITH_OFFER = "goes_with_offer"
    """They picked a piece, and the kinds that go well with it are offered as
    chips beneath its card. Nothing was searched: the reply asks whether
    they would like anything to go with it, and a tap shows that kind
    (CLAUDE.md 10.4)."""

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


class MissingPieceView(BaseModel):
    """One piece the room is still without, named so the reply can say which.

    The type is a verified plan value, given in words. `cheapest_price` is set
    only when the budget stopped it: the lowest real total that would fill it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    piece: str = Field(min_length=1)
    priority: DesignPriority
    reason: UnmetReason
    cheapest_price: Decimal | None = Field(default=None, ge=0)


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
    stay different things to say.
    """

    missing_pieces: tuple[MissingPieceView, ...] = ()
    """Each missing piece by name, with its reason - so the reply says "the rug
    didn't fit the budget", never "1 needed piece couldn't be included"."""

    seating_for: int | None = Field(default=None, ge=1)
    """How many people the room's seating seats, when that is exactly the
    number they gave - counted from the real pieces, so the plan is never
    sized silently and never claimed for more than it seats."""

    seats_short_of: int | None = Field(default=None, ge=1)
    """Their head count, when the room's seating seats fewer - so the reply says
    plainly it is short rather than implying everyone has a seat."""

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

    stretched_from_budget: Decimal | None = None
    stretched_overage: Decimal | None = None
    """Set on the turn a stretch is confirmed: the budget the customer first set,
    and how far the room's new total now runs over it. The room's ceiling has been
    raised to the new total, so `within_budget` reads true - these say plainly
    that the customer chose to go over their original figure, so the reply owns the
    stretch rather than calling the room "within budget" (CLAUDE.md 27). Both
    computed by the application; the reply states them and computes nothing."""

    @model_validator(mode="after")
    def _stretch_figures_travel_together(self) -> Self:
        if (self.stretched_from_budget is None) != (self.stretched_overage is None):
            raise ValueError("a stretch carries both the original budget and the overage")
        if self.stretched_overage is not None and self.stretched_overage <= 0:
            raise ValueError("a stretch runs over the original budget by a positive amount")
        return self

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


class SwapOfferGroundingView(BaseModel):
    """A held over-budget swap, as the reply may word it.

    The stage is which question is on the table; the yes/no is drawn as chips
    beside the reply. The three figures - the new total, the budget it broke, and
    by how much - are all computed by the deterministic swap, none by a model, so
    the reply may state them plainly and must not soften the overage to "a
    little": being honest about the money is the point of the turn (CLAUDE.md 27).
    They are the only figures a difference is included among, because the
    application computed that difference; the reply still computes nothing itself.
    `over_budget` is always true here, carried so the model is told plainly what
    to be honest about.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: SwapBudgetOfferStage
    over_budget: bool = True
    new_spend_total: Decimal
    budget_max: Decimal
    overage: Decimal
    currency: str = Field(min_length=1)


class RoomQuestionGroundingView(BaseModel):
    """The one room question this turn asks, as the reply may word it.

    Which question is the application's; the words are the model's. The pieces
    are drawn as chips beside the reply, so only how many start selected
    travels here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_kind: str = Field(min_length=1)
    question: RoomQuestionKind
    earlier_seat_count: int | None = Field(default=None, ge=1)
    """A head count they gave for a seating search earlier - to confirm, never
    to assume."""

    picked_seat_count: int | None = Field(default=None, ge=1)
    """How many the sofas and chairs they picked for this room seat together,
    counted from the products - to confirm as everyone, or to add to."""

    picked_pieces: int = Field(default=0, ge=0)
    """How many of the pieces offered are already their picks - shown on the
    chips as theirs, so the reply can say the room keeps them."""

    pieces_offered: int = Field(default=0, ge=0)
    pieces_preselected: int = Field(default=0, ge=0)


class ProductBriefGroundingView(BaseModel):
    """The card of questions shown beneath the reply, as the reply may know it.

    What it is about and what it asks - never its choices: the card shows them,
    and a reply that listed them would be a second copy that could disagree.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    looking_for: str = Field(min_length=1)
    """What they need, in customer words ("sofa")."""

    asks_about: tuple[BriefQuestionKind, ...] = Field(min_length=1)

    choose: int = Field(default=0, ge=0, le=OPENING_QUESTIONS)
    """How many of `asks_about` the reply asks, choosing the most useful for
    this customer - an opening. Zero for a card, which shows every question."""

    narrowing: bool = False
    """Narrow down for the results already on screen, opened because they
    asked to narrow them - not a new search's questions."""

    must_ask: tuple[BriefQuestionKind, ...] = ()
    """Of those, the ones an opening always asks: how much space the piece
    has, wherever it is offered - the size it must fit decides more than any
    other answer."""


class DirectionView(BaseModel):
    """The designer's direction for the suggested kind, as the reply may use
    it to say why these cards: words and approved values only, no figure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    colours: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    character: str | None = None
    avoid: tuple[str, ...] = ()
    size: Literal["smaller", "similar", "larger"] | None = None
    """Beside the pick, when a size proportion was used to order the cards."""
    sized_for_the_pick: bool = False
    """The first card is in the size that goes in the pick - a mattress for
    this bed, as the designer read it off the bed. Never when no card is: the
    ordering is then no claim the reply can make."""

    @classmethod
    def of(
        cls,
        need: DesignCategoryNeed,
        lean: RankingLean | None,
        size: SizeSettings,
        *,
        first_fits: bool = False,
    ) -> DirectionView:
        direction = need.direction
        assert direction is not None
        ratio = Decimal(str(direction.size_ratio)) if direction.size_ratio is not None else None
        sized = lean is not None and lean.size_target_cm is not None and ratio is not None
        return cls(
            colours=direction.colours,
            styles=direction.styles,
            character=need.semantic_intent,
            avoid=(*direction.avoid_colours, *direction.avoid_styles),
            size=(
                None
                if not sized or ratio is None
                else "smaller"
                if ratio < size.similar_low
                else "larger"
                if ratio > size.similar_high
                else "similar"
            ),
            sized_for_the_pick=first_fits and lean is not None and lean.fit_side_cm is not None,
        )


class TasteQuestionGroundingView(BaseModel):
    """The soft taste question this reply closes on (phase 5): the two cards
    by position, or the styles or values offered - words and positions only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["which", "style", "avoid", "space"]
    positions: tuple[int, ...] = ()
    options: tuple[str, ...] = ()


class RoomCarriedView(BaseModel):
    """What a room took from shopping this turn, so the reply can say it once
    instead of asking: the head count, the colours and styles they said, the
    wall they gave."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seats: int | None = Field(default=None, ge=1)
    colours: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    wall: Decimal | None = Field(default=None, gt=0)
    """The wall they gave, in centimetres."""


class SpaceFitView(BaseModel):
    """The space they gave, the width the designer would aim for in it, and
    why - worked out this turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    space_cm: Decimal = Field(gt=0)
    ideal_cm: Decimal = Field(gt=0)
    reason: str = Field(min_length=1)


class TasteAnsweredView(BaseModel):
    """What they just told us of their taste, and what the cards now lean to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    liked: tuple[str, ...] = ()
    avoided: tuple[str, ...] = ()
    neither: bool = False
    space: bool = False
    """They said how wide the spot is: the cards now fit it first."""


class ChosenSeatingPieceView(BaseModel):
    """One kind of piece in the combination they chose, and how many."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    seats_each: int = Field(ge=1)


class ChosenSeatingView(BaseModel):
    """The seating combination they chose this turn - its pieces, how many of
    each, and the seats they add up to, all counted by the application.

    Their picks list each product once, so without this a choice of two of
    the same sofa reads as one sofa and half the seats (CLAUDE.md 27.1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pieces: tuple[ChosenSeatingPieceView, ...] = Field(min_length=1)
    total_seats: int = Field(ge=1)
    target_seats: int | None = Field(default=None, ge=1)


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

    lifted: tuple[AttributeFamily, ...] = ()
    """A strict colour or style no combination could meet, set aside as the
    last resort: the reply must say none matched exactly, never imply it did."""
    not_size_limited: int = Field(default=0, ge=0)
    """Combinations on screen that their size did not limit - it was given for
    another kind of piece. The reply must not claim those are within it."""
    wishes_given: bool = False
    fully_wished: int = Field(default=0, ge=0)
    """Combinations whose every piece is a colour or style they wished for.
    Only when this equals `bundle_count` may the reply describe them all as
    their colour or look."""

    shape_options: tuple[SeatingShapeOption, ...] = ()
    """For a shape question: the ways the store can really reach the seat
    count, each with its lowest total. Sayable figures, from real products."""
    currency: str | None = None
    closest_total: Decimal | None = Field(default=None, ge=0)
    """When nothing fits the budget: the lowest real total that seats them with
    the budget set aside - the figure the reply offers as the next step."""

    ask_colour: bool = False
    already_seen: int = Field(default=0, ge=0)
    """Combinations shown before and left out. Above zero, every combination on
    screen is new - "show me more" was answered with ones they have not seen."""
    exhausted_shape: SeatingShape | None = None
    """For no_more: the shape they were paging through that has run out."""

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
            SeatingSolutionOutcome.CHOOSE_SHAPE,
            SeatingSolutionOutcome.NO_MORE,
        ):
            raise ValueError("only a composed, over-budget or question outcome reaches the model")
        if self.outcome is SeatingSolutionOutcome.CHOOSE_SHAPE and not self.shape_options:
            raise ValueError("a shape question carries its options")
        if self.shape_options and self.outcome not in (
            SeatingSolutionOutcome.CHOOSE_SHAPE,
            SeatingSolutionOutcome.NO_MORE,
        ):
            raise ValueError("only a question or a no-more reply offers shapes")
        if (self.outcome is SeatingSolutionOutcome.BUNDLES) != (self.bundle_count > 0):
            raise ValueError("combinations are on screen exactly when the outcome is BUNDLES")
        return self


_SEARCH_KINDS = frozenset({ResponseOutcomeKind.SEARCH_RESULTS, ResponseOutcomeKind.ZERO_RESULTS})
"""The two outcomes an executed search produces, either of which may carry
search provenance. A detail, a comparison or a room ran no search."""


class TypeMixView(BaseModel):
    """A search for one kind that also showed the kinds beside it - sofa sets
    and sectional sofas beside sofas - or would have, but for a size.

    Counts and kind words, never products: the reply says how many of the kind
    they asked for really meet their request, and names only kinds on screen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    asked_kind: str
    shown_beside: tuple[str, ...] = ()
    """The kinds searched with it, which may or may not be on screen."""

    asked_kind_matches: int | None = Field(default=None, ge=0)
    """How many products of the asked kind meet their request - and seat the
    head count, when they gave one. Fewer than the cards on screen means the
    rest are other kinds, there because this kind ran short."""

    on_screen: tuple[str, ...] = ()
    """The kinds the cards on screen are, in the order they first appear. No
    counts: how many cards of each are on screen says nothing about the shop."""
    left_out_for_size: tuple[str, ...] = ()
    """Kinds left out because they gave a size: these kinds' listed sizes
    cannot be checked, so only the asked kind is shown."""


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

    offered_instead_of: str | None = None
    """The seating type they asked for, in words, when it never seats that
    many and the cards are another type that does - offered as the best fit,
    never as a refusal."""

    unstocked_type: str | None = None
    """The type they asked for, in words, when the store stocks *none* of it and
    the cards are the closest type it does stock - offered instead of a dead end.

    Distinct from `offered_instead_of`, and the difference is the whole reason
    for two fields: there the asked type exists but cannot seat them; here the
    store simply does not carry it, so the reply says "we don't have that, but
    here's the closest" rather than "this is the best fit for your number"."""

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

    shopping_room: str | None = None
    """The room they said the piece is for, in their words or as they tapped
    it. Theirs to hear acknowledged; no card was filtered by it."""

    seats_for: int | None = None
    """How many they said usually sit there, when that ordered these cards:
    pieces reviewed to seat that many come first. Not a promise that every
    card does - `screen` shows which ones."""

    picked_kind: str | None = None
    """The kind of pick they just chose - "bed" - whose card is shown above
    the kinds that go with it.

    The kind, never the product: its facts are on its card, not in the reply.
    Without it the reply saw a customer asking about a product it had no facts
    for, and said so - beside the card that answered them.
    """

    goes_well_with: tuple[str, ...] = ()
    """The kinds offered as chips beneath their pick - "nightstands", "rugs" -
    for `GOES_WITH_OFFER` and nothing else. Offered, not searched: nothing
    about them is known beyond the store stocking them."""

    best_match_first: bool = False
    """The cards are ordered by how well they match what the customer
    described - their colours, styles, the feel they chose - so the first is
    the closest. False when the order is a price sort, or nothing they said
    could order it: then the first card is simply first."""

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
    unanswerable_sizes: tuple[DimensionRole, ...] = ()
    """Measurements they asked for this turn that this kind's listings cannot
    answer reliably: not applied, to be said plainly."""
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

    type_mix: TypeMixView | None = None
    """The kinds a search showed beside the one asked for, or left out for a
    size: say it as it is - "I have only one sofa that seats 5; these sofa
    sets seat you all" - and call each card by its own kind."""

    sized_for_their_pick: str | None = None
    """Their own results for what goes inside a piece they picked - a bed -
    and the first card is in the size it takes, as the designer read it off
    the pick: the ones in that size come first."""

    compared_count: int = Field(default=0, ge=0)
    comparison_differs_on: tuple[ComparisonField, ...] = ()
    """Which fields differ. Never a cell, so "they differ mainly on width" is
    sayable and "one is 20 cm wider" is not."""

    bundle: BundleGroundingView | None = None
    """The whole-room outcome, for `ROOM_BUNDLE` and nothing else."""

    seating: SeatingSolutionGroundingView | None = None
    """The composed combination, for `SEATING_COMBINATION` and nothing else."""

    chosen_seating: ChosenSeatingView | None = None
    """The combination they chose this turn: it seats everyone it was built
    for, and the reply never calls it incomplete."""

    taste_question: TasteQuestionGroundingView | None = None
    """A soft taste question to close on - the application's, worded by you."""

    taste_answered: TasteAnsweredView | None = None
    space_fit: SpaceFitView | None = None
    """What the designer would aim for in the space they gave, when it was
    worked out this turn - the cards are ordered by closeness to it."""
    room_carried: RoomCarriedView | None = None
    """What the room took from shopping this turn, said once, never asked."""

    """This turn answered a taste question: acknowledge it in a few words."""

    design_direction: DirectionView | None = None
    """Why these cards, after a pick: the designer's direction for the kind
    suggested, which ordered them."""

    narrowed: bool = False
    """They just changed what the search uses on Narrow down: the cards are
    the search as it is now, and what it used before is gone."""

    selection_liked: bool = False
    """The cards shown are their liked list, not their picks."""
    liked_also_picked: int = Field(default=0, ge=0)
    """Of the liked cards shown, how many are among their picks as well -
    the only figure the reply may give for that."""

    next_step: NextStepKind | None = None
    """The one next step to end on, when the turn asks nothing of its own:
    the application decided it, the chips beside the reply answer it, and the
    reply words it as its closing question (CLAUDE.md 10.2)."""

    room_question: RoomQuestionGroundingView | None = None
    """What to ask about the room, for `ROOM_QUESTION` and nothing else."""

    swap_offer: SwapOfferGroundingView | None = None
    """The held over-budget swap, for `ROOM_SWAP_OFFER` and nothing else."""

    brief: ProductBriefGroundingView | None = None
    """The card of questions shown with the reply: the whole turn for
    `PRODUCT_BRIEF`, or folded beneath results as "Narrow down" for
    `SEARCH_RESULTS`. Either way it is the turn's only question."""

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

    still_on_screen: tuple[PresentedCardView, ...] = ()
    """The cards from before this turn, still in front of the customer when
    this turn showed none of its own - what "which one do you recommend?" is
    about. Read fresh from the catalog, never remembered."""

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

        if (self.kind is ResponseOutcomeKind.ROOM_QUESTION) != (self.room_question is not None):
            raise ValueError("a room question carries its question, and only it does")

        if self.kind is ResponseOutcomeKind.PRODUCT_BRIEF and self.brief is None:
            raise ValueError("a product brief carries its card")
        if self.brief is not None and self.kind not in (
            ResponseOutcomeKind.PRODUCT_BRIEF,
            ResponseOutcomeKind.SEARCH_RESULTS,
        ):
            raise ValueError("a card of questions is shown only first or beside results")

        if (self.kind is ResponseOutcomeKind.GOES_WITH_OFFER) != bool(self.goes_well_with):
            raise ValueError("an offer of what goes with a pick names the kinds, and only it does")

        if self.best_match_first and self.kind is not ResponseOutcomeKind.SEARCH_RESULTS:
            raise ValueError("only results on screen have a best match")

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

        for words in (
            self.commerce_category,
            self.commerce_subcategory,
            self.offered_instead_of,
            self.unstocked_type,
        ):
            # The registry key is an internal identifier; a model shown one
            # writes it back verbatim.
            if words is not None and "-" in words:
                raise ValueError("a category reaches the model as words, not a key")
        if self.offered_instead_of is not None and self.unstocked_type is not None:
            # Two different substitutions cannot both own one turn: the seat-count
            # swap and the not-stocked swap are mutually exclusive recoveries.
            raise ValueError("a turn offers one substitution reason, not both")
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
    OPENING_NOT_OFFERED = "opening_not_offered"
    """The questions it asked are not the opening's to ask: not offered, not
    as many as it had to choose, or asked where nothing was to be chosen."""


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
