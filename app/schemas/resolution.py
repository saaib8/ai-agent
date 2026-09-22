"""What the product-dependent services return.

**Application-only.** These are the first M11 contracts allowed to carry a
`product_id`, because they are what application code produces *after* resolving
a model's safe selector against state it wrote itself. Nothing here is ever
serialised into a prompt, and guard tests prove none of it is reachable from a
model-facing type.

Every failure is a reason code. A deterministic service knows *that* "the beige
one" matched two products; it does not know how to say so to a customer, and a
service that authored the sentence would be writing conversation into the
search layer (CLAUDE.md 3.3).

The distinction the failure vocabulary protects: "we could not tell which
product you meant" is a question, and "that product is gone" is a fact. Both
stop the operation, and they lead to different replies.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import BlockingClarificationReason
from app.schemas.comparison import ProductComparisonResult
from app.schemas.grounding import SearchExecutionGrounding
from app.schemas.product import ProductCandidate
from app.schemas.query import ResolvedSearch
from app.schemas.refinement import PriceRefinement
from app.schemas.relaxation import StopReason


class ReferenceFailureReason(StrEnum):
    """Why a selector did not become exactly one product."""

    NO_PRESENTED_RESULTS = "no_presented_results"
    """Nothing has been presented, so there is no list to count into."""

    ORDINAL_OUT_OF_RANGE = "ordinal_out_of_range"

    NO_COMPARISON = "no_comparison"
    """Nothing is being compared, so there are no columns to count into."""

    COMPARED_ORDINAL_OUT_OF_RANGE = "compared_ordinal_out_of_range"
    """Column four of a two-column table. Distinct from the list being too
    short, because the customer is counting in a different place."""

    KIND_MISMATCH = "kind_mismatch"
    """The position resolved, and to the wrong kind of thing.

    "Sofa five" while five centre tables are on screen: the position exists,
    so nothing was out of range, and the product at it is not what they named.
    Refused rather than taken, because taking it silently selected a coffee
    table for a customer who was talking about a sofa (M15 2).
    """
    NO_FOCUSED_PRODUCT = "no_focused_product"
    NO_SELECTED_PRODUCT = "no_selected_product"

    SEVERAL_SELECTED_PRODUCTS = "several_selected_products"
    """"The one I liked" when several are selected. Ambiguous, not empty."""

    NO_ATTRIBUTE_MATCH = "no_attribute_match"
    SEVERAL_ATTRIBUTE_MATCHES = "several_attribute_matches"
    UNAPPROVED_ATTRIBUTE_VALUE = "unapproved_attribute_value"
    """A colour or style the registry does not contain."""

    TIED_EXTREMUM = "tied_extremum"
    """Two products share the lowest price. Taking the first would be a guess."""

    MIXED_CURRENCY_PRESENTATION = "mixed_currency_presentation"
    """"The cheapest" across currencies has no answer without conversion."""

    PRESENTED_SET_INCOMPLETE = "presented_set_incomplete"
    """A product the customer was shown can no longer be read.

    Resolution stops rather than interpreting their words against a shorter
    list: dropping a member before reading "the beige one" could change which
    product they meant.
    """

    PRODUCT_UNAVAILABLE = "product_unavailable"
    """Deleted, deactivated, or owned by another retailer - indistinguishable
    on purpose, so a refusal reveals nothing about another store's catalog."""


class ResolvedProductReference(BaseModel):
    """Exactly one product, verified present in the current retailer's catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: int


class ReferenceUnresolved(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: ReferenceFailureReason


ReferenceOutcome = ResolvedProductReference | ReferenceUnresolved


class RelativePriceFailureReason(StrEnum):
    REFERENCE_UNRESOLVED = "reference_unresolved"
    """The product the comparison is against could not be identified."""

    CURRENCY_CONFLICT = "currency_conflict"
    """The search and the reference are denominated differently.

    No conversion exists, and replacing the customer's stated currency with
    another would change what they asked for without telling them.
    """

    MALFORMED_PERCENT = "malformed_percent"


class ResolvedRelativePrice(BaseModel):
    """A relative relation turned into an absolute bound.

    `price` is what the composer consumes. It is an ordinary absolute
    operation by the time it leaves here, so the composer never learns that a
    product was involved.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    price: PriceRefinement
    reference_product_id: int
    reference_price_amount: str
    reference_price_unit: str


class RelativePriceUnresolved(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: RelativePriceFailureReason
    reference_reason: ReferenceFailureReason | None = None

    @model_validator(mode="after")
    def _detail_matches_the_reason(self) -> Self:
        unresolved = self.reason is RelativePriceFailureReason.REFERENCE_UNRESOLVED
        if unresolved and self.reference_reason is None:
            raise ValueError("an unresolved reference carries the reason it failed")
        if not unresolved and self.reference_reason is not None:
            raise ValueError("only an unresolved reference carries a reference reason")
        return self


RelativePriceOutcome = ResolvedRelativePrice | RelativePriceUnresolved


class ComparisonFailureReason(StrEnum):
    TOO_FEW_PRODUCTS = "too_few_products"
    TOO_MANY_PRODUCTS = "too_many_products"

    DUPLICATE_PRODUCT = "duplicate_product"
    """Two references resolved to the same product. Comparing it with itself
    answers nothing, and quietly continuing with fewer would answer a
    different question from the one asked."""

    PRODUCT_UNAVAILABLE = "product_unavailable"
    """One of the products cannot be read. A partial comparison is not one."""


class ComparisonUnavailable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: ComparisonFailureReason
    requested_count: int = Field(ge=0)
    allowed_maximum: int = Field(ge=2)


ComparisonOutcome = ProductComparisonResult | ComparisonUnavailable


class SimilarSearchFailureReason(StrEnum):
    NO_COMMERCE_CATEGORY = "no_commerce_category"
    """The reference carries no reviewed classification, so there is nothing
    to search for. Deriving one from its name is exactly what the agent
    service must not do (CLAUDE.md 6.1)."""

    UNAPPROVED_COMMERCE_CATEGORY = "unapproved_commerce_category"
    UNAPPROVED_COMMERCE_SUBCATEGORY = "unapproved_commerce_subcategory"


class SimilarSearchUnavailable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: SimilarSearchFailureReason


class SimilarSearchSeed(BaseModel):
    """A new-task search derived from one verified product."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolved: ResolvedSearch
    reference_product_id: int


SimilarSearchOutcome = SimilarSearchSeed | SimilarSearchUnavailable


class SearchRequirementClarificationReason(StrEnum):
    """Why a search could run but would not answer what was asked.

    **Application-only, and deliberately a separate enum from
    `BlockingClarificationReason`.** Every member of that one is emittable by
    the decision model; these are not, and must not be. Whether the catalog can
    execute a requirement is established by query understanding, from the
    taxonomy and the dimension registry - never by a model reading language
    and a safe state view. Sharing one enum would let a decision claim an
    unsupported requirement before any search had run.

    Each member is a different thing to tell the customer, so they stay
    distinct rather than collapsing into one "cannot honour that" (CLAUDE.md
    12.3, 15.1).
    """

    UNSUPPORTED_REQUIREMENT = "unsupported_requirement"
    """Structured search cannot filter on it at all.

    Material, for instance: the catalog records it and no query can restrict by
    it yet. The search would run and simply not honour this part, and
    presenting the results as if it had would be a false claim.
    """

    UNSUPPORTED_DIMENSION_REQUIREMENT = "unsupported_dimension_requirement"
    """A clear measurement this product type's stored axes cannot answer.

    Not a missing role or unit. Those mean the words were ambiguous and one
    question settles them; this means the words were perfectly clear and the
    data cannot be trusted, so asking again changes nothing.
    """

    UNRESOLVED_STRICT_REQUIREMENT = "unresolved_strict_requirement"
    """They were strict about a colour or style the vocabulary does not hold.

    "It must be crimson" can be neither filtered nor promised, and softening it
    to a preference would ignore the word "must".
    """


class DeterministicClarification(BaseModel):
    """A deterministic service stopped, and only the customer can settle it.

    Reason codes, never wording. A service knows *that* "the beige one" matched
    two products; phrasing that as a question is the response layer's job, and
    a coordinator that authored the sentence would be writing conversation into
    the execution layer (CLAUDE.md 3.3).

    Kept apart from `BlockingClarification`, which is what the *model* chose
    when it decided the turn could not proceed. This one is discovered
    afterwards, by application code, and carries no `question` field at all -
    so there is nowhere for invented prose to go.

    **Not every stop is a question.** Three lines are drawn here, and the
    vocabulary enforces them rather than leaving them to a comment:

    * A `CompositionDefect` is the caller's bug - a relative price that reached
      the composer still relative. There is no field for one, because asking a
      customer to fix a sequencing mistake is not a clarification.
    * "That product is gone" is a *fact*, not a question. The two reference
      reasons that mean unavailability belong to `TurnFailure`, and the
      validator below refuses them.
    * A malformed percentage or amount is a validation defect, and likewise has
      no representation here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: BlockingClarificationReason | SearchRequirementClarificationReason
    """What the customer must supply or disambiguate.

    Two families, because they have two different authorities. The
    model-facing one is reused where a deterministic finding genuinely means
    the same thing the model could have meant - an ambiguous reference, a
    missing currency. The other holds what only query understanding can
    establish. The member sets are disjoint, so a reason always names its own
    origin.
    """

    reference_reason: ReferenceFailureReason | None = None
    """Why a selector did not become one product, when that was the cause."""

    bundle_reason: BundleReferenceFailureReason | None = None
    need_reason: DesignNeedFailureReason | None = None
    """Why a furnishing role did not settle on one. A third list with a third
    lifetime: roles outlive the products that fill them."""
    """Why a reference to a piece of the room did not settle on one card.

    A separate field from `reference_reason` because the two name different
    lists: one counts into products a search presented, the other into the
    cards a room is made of, and a reader has to be able to tell which.
    """

    relative_price_reason: RelativePriceFailureReason | None = None
    """Only `CURRENCY_CONFLICT`.

    An unresolved reference is already carried by `reference_reason`, and a
    malformed percentage is a defect rather than a question, so neither may
    appear here.
    """

    @model_validator(mode="after")
    def _only_customer_resolvable_reasons(self) -> Self:
        if self.reference_reason in _UNAVAILABILITY_REASONS:
            raise ValueError(
                "an unavailable product is a failure, not a clarification"
            )
        if (
            self.relative_price_reason is not None
            and self.relative_price_reason
            is not RelativePriceFailureReason.CURRENCY_CONFLICT
        ):
            raise ValueError(
                "only a currency conflict is a customer-resolvable relative price"
            )
        return self


_UNAVAILABILITY_REASONS = frozenset(
    {
        ReferenceFailureReason.PRODUCT_UNAVAILABLE,
        ReferenceFailureReason.PRESENTED_SET_INCOMPLETE,
    }
)
"""Reference failures that state a fact rather than ask a question.

Both mean a product the customer was shown can no longer be read. No answer
they could give would change that, so they belong to `TurnFailure`.
"""


class RankedProductCandidate(BaseModel):
    """One verified product, in the position ranking gave it.

    `relaxation_depth` is provenance, exactly as it is on
    :class:`~app.schemas.relaxation.RelaxedCandidate`: depth 0 satisfied the
    request as asked, depth 1 or more needed a widening. It is not a score and
    must never be presented as relevance.

    There is deliberately **no similarity**. Ranking's job ended when it
    produced this order; carrying its numbers forward would invite a later
    layer to blend them with a price or a fit into one relevance figure, which
    is what CLAUDE.md 16.1 forbids. The order is the output.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product: ProductCandidate
    relaxation_depth: int = Field(ge=0)


class CandidatePoolResult(BaseModel):
    """Every eligible product for one internal search, ranked and hydrated.

    **Application-only, and not a customer-facing result.** The difference from
    :class:`ProductSearchExecutionResult` is the whole reason this exists: that
    one is a page chosen for display and carries grounding and ordinals, while
    this is the complete pool a deterministic optimiser reasons over. It has no
    ordinal, no `grounding_ref` and no grounding at all, so an internal
    candidate cannot be mistaken for a product the customer was shown.

    Unbounded by design. M8 and M9 already produce and order the complete
    eligible pool, and truncating it here would be a new selection policy
    deciding a room's options before anything had judged them - the defect
    M11B-1 removed one layer up. The presentation limit is customer display
    policy and is never read on this path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[RankedProductCandidate, ...] = ()
    """Ranked order, survivors only."""

    eligible_count: int = Field(ge=0)
    """How many products the search made eligible, before hydration.

    Recorded rather than derived: it is the count ranking actually saw, so a
    gap between it and `len(candidates)` stays visible as staleness instead of
    disappearing.
    """

    was_relaxed: bool
    stop_reason: StopReason
    semantic_used: bool

    @model_validator(mode="after")
    def _hydration_only_ever_drops(self) -> Self:
        """A hydrated pool cannot be larger than the pool that was ranked.

        Nothing may backfill or substitute: a stale row leaves the pool
        smaller, and a pool that grew would mean a product entered without
        passing eligibility.
        """
        if len(self.candidates) > self.eligible_count:
            raise ValueError("a hydrated pool cannot exceed the eligible pool")
        if len({c.product.product_id for c in self.candidates}) != len(self.candidates):
            raise ValueError("a product appears at most once in a candidate pool")
        return self

    @property
    def stale_dropped_count(self) -> int:
        """Eligible products the catalog would no longer return."""
        return self.eligible_count - len(self.candidates)


class BundleReferenceFailureReason(StrEnum):
    """Why a selector did not become exactly one visible card.

    Separate from `ReferenceFailureReason`, which is about products presented
    by a search. A room's cards are a different list with a different lifetime,
    and sharing one vocabulary would make "there is no fourth option" and
    "there is no fourth piece in your room" indistinguishable.
    """

    NO_BUNDLE = "no_bundle"
    """There is no room yet, so there is nothing to count into."""

    ORDINAL_OUT_OF_RANGE = "ordinal_out_of_range"

    NO_COMPARISON = "no_comparison"
    """Nothing is being compared, so there are no columns to count into."""

    COMPARED_ORDINAL_OUT_OF_RANGE = "compared_ordinal_out_of_range"
    """Column four of a two-column table. Distinct from the list being too
    short, because the customer is counting in a different place."""

    KIND_MISMATCH = "kind_mismatch"
    """The position resolved, and to the wrong kind of thing.

    "Sofa five" while five centre tables are on screen: the position exists,
    so nothing was out of range, and the product at it is not what they named.
    Refused rather than taken, because taking it silently selected a coffee
    table for a customer who was talking about a sofa (M15 2).
    """
    UNAPPROVED_COMMERCE_TYPE = "unapproved_commerce_type"
    """A product type the vocabulary does not contain. It names nothing, and is
    refused rather than matched to the nearest thing that looks like it."""

    NO_CATEGORY_MATCH = "no_category_match"
    SEVERAL_CATEGORY_MATCHES = "several_category_matches"
    """Two chairs, and "the chair". Taking the first would be a guess."""

    TARGET_PRODUCT_UNAVAILABLE = "target_product_unavailable"
    """The piece they named is no longer in the catalog.

    A fact about their target, not a question: naming it again would not make
    it readable. Distinct from the reason below, which is about the room rather
    than about the piece.
    """

    BUNDLE_NOT_VERIFIABLE = "bundle_not_verifiable"
    """Something else in the room could not be re-read, so a match cannot be
    proven unique.

    Fails closed on purpose. An unreadable piece might have been of the kind
    they described, and treating it as though it could not have matched would
    turn "the lamp" into a guess between two lamps - one of which we simply
    could not see.
    """

    STALE_BUNDLE_REFERENCE = "stale_bundle_reference"
    """The room changed between resolving the reference and acting on it, so
    the card they named may no longer be the card that would be changed."""


class ResolvedBundleReference(BaseModel):
    """Exactly one visible card, with everything needed to act on it.

    **Application-only.** It carries line ids, a product id and the revision it
    was resolved against; none of that reaches a model, which named a position
    or a product type and nothing more.

    `bundle_revision` is the pre-turn value. Requiring it to still hold before
    the mutation is what stops a reference resolved against one room from being
    applied to another within the same turn.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_ids: tuple[int, ...] = Field(min_length=1)
    need_ids: tuple[int, ...] = ()
    product_id: int = Field(ge=1)
    quantity: int = Field(ge=1)
    acquisition: BundleAcquisition
    locked: bool
    ordinal: int = Field(ge=1)
    """Which card it was, counted over what the customer could see."""

    bundle_revision: int = Field(ge=0)


class BundleReferenceUnresolved(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: BundleReferenceFailureReason


BundleReferenceOutcome = ResolvedBundleReference | BundleReferenceUnresolved


class DesignNeedFailureReason(StrEnum):
    """Why a furnishing role could not be identified."""

    NO_PLAN = "no_plan"
    UNAPPROVED_COMMERCE_TYPE = "unapproved_commerce_type"
    NO_NEED_MATCH = "no_need_match"
    SEVERAL_NEED_MATCHES = "several_need_matches"
    """Two roles of that kind. Removing the first would take out a piece of the
    room they did not name."""

    CARD_SPANS_SEVERAL_NEEDS = "card_spans_several_needs"
    """One visible card filling more than one role, or none at all. Which role
    they meant is a question, and product identity cannot answer it."""

    PRESERVED_ROLE_EXCLUDED = "preserved_role_excluded"
    """They asked to keep a piece whose role they also asked to remove.

    Not an ambiguity but a contradiction: both instructions are clear, and
    honouring either silently discards the other. Detected before anything is
    locked or excluded, so neither half is half-applied."""


class ResolvedDesignNeed(BaseModel):
    """Exactly one durable role. Application-only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    need_id: int = Field(ge=1)


class DesignNeedUnresolved(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: DesignNeedFailureReason


DesignNeedOutcome = ResolvedDesignNeed | DesignNeedUnresolved


class ProductSearchExecutionResult(BaseModel):
    """One executed search: what to commit, and what may be explained.

    Application-only, and the reason it exists: `SearchExecutionGrounding`
    deliberately carries no product id, while `commit_search_results` needs
    exactly the rendered ids in rendered order. Splitting them keeps the
    grounding safe to serialise and still lets the application record what the
    customer saw.

    The validator below checks arity and uniqueness. It cannot check that
    `presented_product_ids[i]` and `grounding.products[i]` describe the same
    product - the grounded shape holds no id to compare. **That guarantee is
    structural**: the pipeline builds both lists in one pass over one hydrated
    sequence, so there is no second derivation to disagree.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    presented_product_ids: tuple[int, ...] = ()
    grounding: SearchExecutionGrounding

    @model_validator(mode="after")
    def _ids_account_for_every_grounded_product(self) -> Self:
        if len(self.presented_product_ids) != len(self.grounding.products):
            raise ValueError("every presented product is grounded, and vice versa")
        if len(set(self.presented_product_ids)) != len(self.presented_product_ids):
            raise ValueError("a product is presented at most once")
        if any(product_id < 1 for product_id in self.presented_product_ids):
            raise ValueError("a product id is 1 or greater")
        return self
