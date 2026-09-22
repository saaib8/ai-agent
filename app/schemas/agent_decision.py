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

from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_state import MAX_SEMANTIC_INTENT_CHARS, PurchaseStage
from app.schemas.bundle_reference import (
    BundleReferenceSelector,
    DesignNeedCategoryMatch,
)
from app.schemas.geometry import RoomMeasurementRole
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
    BUNDLE_REFINE = "bundle_refine"
    """Change whether a piece already in the room is preserved.

    A room edit, not a search and not a design question, which is why it is its
    own action: the coordinator dispatches on one action per turn, and hiding a
    room mutation inside `ANSWER` would make a state change invisible at the
    branch that decides what a turn does.
    """

    DESIGN_HANDOFF = "design_handoff"
    """Interior-design reasoning: composing a room, or recomposing one.

    Executed since M12E-2. Whether it plans a room or revises an existing one
    is the application's decision, read from durable state - the model asks for
    design work and never says which kind.
    """


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


class FollowUpGoal(StrEnum):
    """What the optional question should be *about*.

    The split this exists for: the Customer/Commerce Agent knows which missing
    fact would most improve the next recommendation, and the response model
    knows how to ask for it in a sentence. Neither does the other's job - so
    this carries the subject and never the wording.

    A controlled list, because a free-text goal would be question prose by
    another name, and prose here would bypass the response layer's validation.
    """

    BUDGET = "budget"
    """What they want to spend. Asked for a room before building one; asked
    after results when the range shown is clearly wide of the mark."""

    ROOM_SIZE = "room_size"
    STYLE = "style"

    SEATING_REQUIREMENT = "seating_requirement"
    """How many people use the room, or need to sit on the piece. A
    requirement, never a quantity of furniture."""

    USE_CASE = "use_case"
    """How the piece is actually lived with - everyday family use, occasional
    guests, a reading corner. Often worth more than a style word."""

    PRODUCT_PREFERENCE = "product_preference"
    """Which way to narrow what is already on screen: colour, material,
    proportion. Only when the set shown genuinely divides on it."""

    ROOM_COMPLETION = "room_completion"
    """Whether they want help with the rest of the room. Asked only after
    they have shown real interest in something."""


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


class BundleInteractionOp(StrEnum):
    """What a bundle refinement does to the piece it names.

    Every member is the customer's intent, never our storage vocabulary. The
    reducer keeps a generic status operation, but `LOCK` and `UNLOCK` are what
    a person actually asks for; the application maps them. A model naming a
    status would be authoring the shape of our state (CLAUDE.md 3.3).
    """

    LOCK = "lock"
    """Keep this piece. Later optimisation may not replace it."""

    UNLOCK = "unlock"
    """This piece may change later. **Permission, not an instruction**: it does
    not replace anything now."""

    SET_ACQUISITION = "set_acquisition"
    """Say whether this piece is being bought or is already theirs.

    Reversible on purpose - "actually I do need to buy that" is an ordinary
    correction, and a one-way "mark as owned" could not express it.
    """

    REPLACE_PRODUCT = "replace_product"
    """Find a different product for this piece's role.

    The role stays; the product changes. Whether the replacement should be
    cheaper, dearer or simply different is on the replacement payload.
    """

    REMOVE_NEED = "remove_need"
    """Take a kind of thing out of the room's plan entirely.

    Deliberately not the same as replacing a product: one says "not this sofa",
    the other says "no sofa". Reading the second as the first would put a sofa
    back in a room they asked to have none in.
    """


class BundleReplacementMode(StrEnum):
    """What kind of different product they are asking for."""

    ALTERNATIVE = "alternative"
    """Just a different one. Implies nothing about price, kind or looseness."""

    CHEAPER = "cheaper"
    """Less than this one costs. The figure is the catalog's, read fresh."""

    MORE_EXPENSIVE = "more_expensive"
    """More than this one costs, and **only** that.

    Never "premium", "better" or "higher quality": price is not quality, and a
    customer asking for something better has not told us what better means.
    """

    SEMANTIC = "semantic"
    """A different character - lighter, more minimal, softer. Ranking, never a
    filter."""


class BundleReplacementIntent(BaseModel):
    """What sort of replacement to look for.

    No amount, no percentage, no bound. "Cheaper" names a direction; what it
    costs is read from the product itself, because the model has never been
    told a price and must not invent one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: BundleReplacementMode
    semantic_intent: SemanticIntentRefinement | None = None
    """The new character to look for, for `SEMANTIC` and nothing else.

    `SET` replaces this role's wording outright and `CLEAR` removes it -
    deliberately not appended, or a role refined three times would carry three
    generations of contradictory language.
    """

    @model_validator(mode="after")
    def _wording_belongs_to_the_semantic_mode(self) -> Self:
        if (self.mode is BundleReplacementMode.SEMANTIC) != (self.semantic_intent is not None):
            raise ValueError("only a semantic replacement carries new wording")
        return self


class BundleInteractionIntent(BaseModel):
    """One change to one piece of the room, or to one role in its plan.

    Carries no identity of any kind - no product, no line, no need, no revision
    - and no price. The model names a card or a kind of thing; the application
    works out which lines that is and what anything costs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: BundleInteractionOp
    selector: BundleReferenceSelector | None = None
    """The visible card this is about. Absent only when a role is named
    directly, which removal may do because a role need not be filled."""

    need_selector: DesignNeedCategoryMatch | None = None
    acquisition: BundleAcquisition | None = None
    replacement: BundleReplacementIntent | None = None

    @model_validator(mode="after")
    def _each_operation_carries_what_it_needs(self) -> Self:
        """Payloads are refused rather than ignored.

        An operation carrying something it cannot act on means the model meant
        something other than what it said, and quietly dropping the extra would
        execute the half we happened to understand.
        """
        if self.op is BundleInteractionOp.REMOVE_NEED:
            if (self.selector is None) == (self.need_selector is None):
                raise ValueError("a removal names a card or a role, and one of them")
        elif self.selector is None:
            raise ValueError(f"{self.op} names the piece it changes")
        elif self.need_selector is not None:
            raise ValueError("only a removal may name a role directly")

        wants_acquisition = self.op is BundleInteractionOp.SET_ACQUISITION
        if wants_acquisition != (self.acquisition is not None):
            raise ValueError("an acquisition change states the acquisition, and only it does")

        wants_replacement = self.op is BundleInteractionOp.REPLACE_PRODUCT
        if wants_replacement != (self.replacement is not None):
            raise ValueError("a replacement states what sort, and only it does")
        return self


class DesignAnchorIntent(BaseModel):
    """A piece the room is to be designed around, as the customer described it.

    Three facts, and the model may state all three because all three are things
    the customer said: which piece they mean, whether they already have it, and
    how many.

    It carries no product id. The selector is resolved by application code
    against the products this conversation actually presented, exactly as every
    other reference is (CLAUDE.md 20.2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: ProductReferenceSelector

    acquisition: BundleAcquisition | None = None
    """Only when they said so outright - "I already own this one".

    `None` means they did not say, and the application supplies its own
    default. It must not be filled in from "keep it", "design around it", a
    lock, a category or a price: those say the piece stays in the room, which
    is a different fact from who paid for it (CLAUDE.md 3.3).
    """

    quantity: int = Field(default=1, ge=1)
    """How many of this piece the room has. Only from what they said - "I
    already own two of these" - never from the kind of thing it is."""


class DesignScope(StrEnum):
    """How much of the space a design handoff is about.

    The same route serves two different requests, and answering one with the
    other is the mistake: a customer who likes a sofa has not asked to furnish
    a room, and replying with eight pieces and a total would be selling at them
    rather than helping. A customer who asked for a room is not served by one
    rug.
    """

    WHOLE_ROOM = "whole_room"
    """Furnish or recompose the room. Produces a plan and a chosen bundle."""

    COMPLEMENT = "complement"
    """The single furnishing role that would most complete the space around a
    piece they have settled on. Produces products, not a room."""

    ADVICE = "advice"
    """A design question, answered as design knowledge. Produces no products.

    "What colours work with walnut?" and "how big should a rug be for a
    three-seat sofa?" are questions about rooms in general, not requests to
    shop. Searching the catalog for them would answer something the customer
    did not ask and bury what they did (CLAUDE.md 36, 38).

    It is a scope rather than a separate action because it is the same
    capability - the design specialist - asked about a different extent: the
    whole room, one piece beside another, or neither.
    """


class DesignRevisionIntent(BaseModel):
    """The hard constraints on recomposing a room the customer already has.

    Only what the customer said outright and the application can resolve
    deterministically. Everything else about the new room - which roles it
    needs, how many, how important, what character - is the design specialist's
    reasoning, and this contract deliberately cannot express any of it.

    Both members reuse reference surfaces that already exist, and they are not
    interchangeable: a role lives in the plan and may have nothing filling it,
    while a card is a piece the customer can see. They are resolved against
    different surfaces and neither can name the other.

    No identity, no figure: no need id, line id, product id, revision or price.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    removed_needs: tuple[DesignNeedCategoryMatch, ...] = ()
    """Roles the customer explicitly wants gone from the revised room.

    A hard negative constraint on the new plan, not a deletion performed now:
    "replace the dining area with a reading corner" is one revision, and
    removing the dining first would leave them with neither if the redesign
    failed.
    """

    preserved_items: tuple[BundleReferenceSelector, ...] = ()
    """Pieces already in the room that must survive the recomposition.

    "Keep this sofa, but turn the dining area into a reading corner" is one
    sentence and one intent. The selector names a visible card; the application
    resolves it, verifies it and locks every line behind it.
    """


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


class RoomMeasurementProposal(BaseModel):
    """One room measurement the customer stated, before validation.

    Strings and a unit, exactly like `PriceProposal`: the figure never passes
    through a binary float, and the unit is theirs to state. Nothing is assumed
    to be centimetres - "my room is 5 by 4" with no unit is a question, not a
    guess (CLAUDE.md 15.1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: RoomMeasurementRole
    value: str = Field(min_length=1)
    unit: str | None = None
    label: str | None = Field(default=None, max_length=80)


class RoomGeometryProposal(BaseModel):
    """Room measurements from this turn. Only what they actually said."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    measurements: tuple[RoomMeasurementProposal, ...] = Field(min_length=1)


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
    room_geometry: RoomGeometryProposal | None = None
    clear_room_geometry: bool = False
    room_budget: PriceProposal | None = None
    clear_room_budget: bool = False
    design_preferences: PreferenceProposal | None = None

    regular_seating_count: int | None = Field(default=None, ge=1, le=30)
    """How many people regularly use the room, when they said so.

    "Family of five", "we're five people", "seating for four" - the same fact
    however it is phrased. A room requirement, never a product quantity: it
    says nothing about how many sofas to buy, and the design specialist decides
    the composition (CLAUDE.md 10.1).

    Only from an explicit statement about who uses the room. Never inferred
    from a room type, a product's seating capacity, or how many pieces are in
    their bundle.
    """

    clear_regular_seating_count: bool = False

    @model_validator(mode="after")
    def _setting_and_clearing_are_exclusive(self) -> Self:
        if self.clear_room_type and self.room_type is not None:
            raise ValueError("room_type cannot be set and cleared in one turn")
        if self.clear_room_budget and self.room_budget is not None:
            raise ValueError("room_budget cannot be set and cleared in one turn")
        if self.clear_room_geometry and self.room_geometry is not None:
            raise ValueError("room_geometry cannot be set and cleared in one turn")
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

    MISSING_ROOM_REQUIREMENTS = "missing_room_requirements"
    """A room was asked for without enough to plan one worth showing.

    The one place this service asks before delivering. A single product search
    can proceed on almost nothing - "show me sofas" is answerable - but a whole
    room commits the customer to a set of pieces and a total, and building that
    around a guessed budget produces a room they cannot buy.

    Budget first, because it constrains every other choice. Beyond it, only
    what the state view shows is still missing: the size of the room, the look
    they want, or how many people the seating is for.

    At most two questions, once. If they decline or answer partially, the room
    is built from what is known (CLAUDE.md 10).
    """

    CONTRADICTORY_ROOM_INSTRUCTIONS = "contradictory_room_instructions"
    """Two things they asked for in one turn cannot both hold.

    Model-facing, which this enum requires: "keep this sofa but get rid of all
    the seating" contradicts itself in the customer's own words, so a model can
    recognise it from language alone. The application also raises it when
    resolving revision constraints turns up the same contradiction."""


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
    A design handoff's anchor is deliberately **not** here: it carries an
    acquisition and a quantity alongside the selector, and two places able to
    name the anchor would be two answers to the same question. It lives on
    `design_anchor`.

    A search's reference is the *only* deterministic signal for the alternatives
    route. `commercial_reason` describes motive and never routes: an upsell and
    a request for alternatives are both searches, and reading intent out of a
    motive field would make the same decision execute two different ways.
    """

    comparison_references: tuple[ProductReferenceSelector, ...] = ()
    clarification: BlockingClarification | None = None

    interaction: ProductInteractionIntent | None = None
    bundle_interaction: BundleInteractionIntent | None = None
    """The one room change this turn makes, for `BUNDLE_REFINE` only.

    One per turn: two identity-changing edits from a single decision would need
    an order, and nothing establishes one.
    """

    design_anchor: DesignAnchorIntent | None = None
    """The piece a room is to be designed around, for `DESIGN_HANDOFF` only.

    Optional: a room may be planned from the project's own context with no
    anchor at all. It is the single source of anchor truth - the top-level
    `reference` no longer carries one - so there are never two answers to
    "which piece".
    """

    design_scope: DesignScope = DesignScope.WHOLE_ROOM
    """Whether this handoff is about the room or about one next piece.

    Defaulted to the whole room because that is what a design handoff has
    always meant; a complement is the narrower, newer case and says so
    explicitly.

    **Read only on a design handoff, and deliberately not refused elsewhere.**
    The provider's strict schema requires every field on every decision, so
    "absent" cannot mean "not applicable" - a search must still carry some
    value here. Refusing the ones that come back wrong turned an artefact of
    that into a failed turn, while the value itself changes nothing: no branch
    but the handoff ever reads it.

    This is the exception to refusing payloads rather than ignoring them. It
    holds only because the field cannot alter what a non-handoff turn does.
    """

    design_revision: DesignRevisionIntent | None = None
    """Hard constraints on recomposing an existing room, for `DESIGN_HANDOFF`.

    Optional, and absent for an ordinary room plan. Its absence never means
    "this is an initial plan": whether a plan exists is read from durable
    state, so "add a reading corner" carries no revision intent and is still
    executed as a revision.
    """

    state_proposal: CustomerStateProposal | None = None
    commerce_proposal: DerivedCommerceProposal | None = None
    follow_up_policy: FollowUpPolicy = FollowUpPolicy.OPTIONAL
    follow_up_goal: FollowUpGoal | None = None
    """What the optional question should be about, when one is worth asking.

    Absent means the turn offers no question - either because nothing useful is
    missing, or because they asked not to be asked. It is never the question
    itself: the wording is the response layer's, and a goal that carried prose
    would route around the checks that wording goes through.
    """

    @property
    def anchor_reference(self) -> ProductReferenceSelector | None:
        """Which piece a design handoff is about, however the model said it.

        `design_anchor` is the fuller shape - it can also record that they
        already own the piece, or want two of it - so it wins when both are
        present. A bare `reference` says only which piece, which is all a
        complement needs.

        One reader, so no branch has to remember that the same statement can
        arrive two ways, and so a slip between them changes nothing.
        """
        if self.design_anchor is not None:
            return self.design_anchor.reference
        if self.action is AgentAction.DESIGN_HANDOFF:
            return self.reference
        return None

    @model_validator(mode="after")
    def _payload_matches_the_action(self) -> Self:
        self._check_search_payloads()
        self._check_reference_payloads()
        self._check_clarification()
        self._check_follow_up()
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
            raise ValueError("a search for alternatives carries no new-search proposal")
        if self.action is AgentAction.REFINE_SEARCH:
            if self.refinement is None and not self.taxonomy_change_requested:
                raise ValueError("a refinement needs a delta or a taxonomy change")
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
        # Three groups, and the difference is whether anything acts on it.
        #
        # **Resolved and used.** A detail is about one product; a search may be
        # seeded from one; a design question may be about the piece on screen -
        # "would the second one work with a walnut table?" - and the specialist
        # is told that piece's design facts (CLAUDE.md 42). Only the advice
        # scope: a room plan takes its anchors from the bundle and a complement
        # from what they settled on, so a reference on either would be a
        # second, competing account of what the design is about.
        #
        # **Recorded and unread.** An answer and a clarification execute
        # nothing. A question about the second sofa naturally carries which
        # sofa, and the provider's strict schema puts the field on every
        # decision anyway - so refusing it turned "I need your room
        # measurements first" into a failed turn while changing nothing, which
        # is the `design_scope` mistake in another place. It is kept for the
        # trace and acted on nowhere.
        #
        # **Refused.** Everything else has its own reference field - a
        # comparison, a bundle edit - so a stray product reference there means
        # the model confused the two, and acting on the wrong one would edit
        # the wrong thing.
        references_used = (AgentAction.PRODUCT_DETAIL, AgentAction.SEARCH)
        # A complement is *about* the piece they settled on, so the model
        # naturally names it - sometimes on `design_anchor`, sometimes here.
        # Both are the same statement, and only `design_anchor` can also say
        # they already own it or want two. Refusing the plainer shape turned
        # "I like the second one" into a failed turn roughly one time in three;
        # `anchor_reference` reads whichever arrived, so there is one path
        # through the code rather than two.
        complement = (
            self.action is AgentAction.DESIGN_HANDOFF
            and self.design_scope is DesignScope.COMPLEMENT
        )
        references_inert = (AgentAction.ANSWER, AgentAction.CLARIFY)
        advice = (
            self.action is AgentAction.DESIGN_HANDOFF
            and self.design_scope is DesignScope.ADVICE
        )
        if (
            self.reference is not None
            and not advice
            and not complement
            and self.action not in references_used
            and self.action not in references_inert
        ):
            raise ValueError(f"{self.action} may not carry a product reference")
        if self.design_anchor is not None and self.action is not AgentAction.DESIGN_HANDOFF:
            raise ValueError("only a design handoff carries a design anchor")
        if self.design_revision is not None and self.action is not AgentAction.DESIGN_HANDOFF:
            raise ValueError("only a design handoff carries revision constraints")
        if (
            self.action is AgentAction.DESIGN_HANDOFF
            and self.design_scope is DesignScope.COMPLEMENT
            and self.design_revision is not None
        ):
            # A complement adds one piece beside what they have; a revision
            # rewrites the plan. Asking for both leaves it unclear which room
            # the answer describes. Checked only on a handoff, where both
            # fields mean something.
            raise ValueError("a complement revises no plan")
        self._check_bundle_interaction()

    def _check_bundle_interaction(self) -> None:
        """A room edit names exactly one piece and does nothing else.

        Every other payload is refused rather than ignored: a turn that both
        locked a piece and ran a search would be two identity-changing effects
        from one decision, and the reply could only describe one of them.
        """
        if self.action is not AgentAction.BUNDLE_REFINE:
            if self.bundle_interaction is not None:
                raise ValueError("only a bundle refinement carries a bundle change")
            return
        if self.bundle_interaction is None:
            raise ValueError("a bundle refinement needs the change it makes")
        if self.interaction is not None:
            raise ValueError("a bundle refinement makes one change, not two")

    def _check_follow_up(self) -> None:
        """A goal belongs to a turn that is actually offering a question.

        Refused rather than ignored in either direction: a goal on a silent
        turn means the model meant to ask and the policy says it may not, and
        that disagreement should surface here rather than become a question
        nobody sees or a silence nobody intended.
        """
        if self.follow_up_policy is FollowUpPolicy.NONE and self.follow_up_goal:
            raise ValueError("a turn offering no follow-up has nothing to ask about")

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
    "RoomGeometryProposal",
    "RoomMeasurementProposal",
    "SoleSelectedProduct",
]
