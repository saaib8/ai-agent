"""Verified facts a response may be built from.

Everything the customer is eventually told about a product comes from here,
and everything here came from PostgreSQL this turn. That is the whole point:
the response model explains and recommends, and the application renders the
facts, so a fabricated price has nowhere to enter (CLAUDE.md 20.4, 20.6).

What is deliberately absent is as important as what is present. No
`ControlledSearchResult`, no `ProductRow`, no `SemanticRankingResult`, no
similarity score, no Pinecone metadata, no namespace, no `store_id`, no SQL,
no exception. A service object passed to a model is a service object whose
next field is in the prompt too.

**No product id, either.** The response model addresses products by
`grounding_ref`, a handle that means nothing outside this turn. Which id each
ref denotes is held by application code, so prose can cite a product without
ever naming one.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.dimensions import NormalisedDimensions
from app.schemas.product import CommerceClassification
from app.schemas.query import ConstraintStrength
from app.schemas.relaxation import RelaxableField, StopReason
from app.schemas.semantic import SemanticSkipReason
from app.taxonomy.dimensions import DimensionRole, UnsupportedDimensionReason


class SearchOutcome(StrEnum):
    RESULTS = "results"
    ZERO_RESULTS = "zero_results"
    """A search that ran and matched nothing. Not a failure (CLAUDE.md 13.5)."""


class TurnFailureCode(StrEnum):
    """A named outcome the response layer can render safely.

    Codes rather than messages: an exception's text may echo a prompt, a DSN
    or a provider's internals, and none of that may reach a customer.
    """

    SEARCH_UNAVAILABLE = "search_unavailable"
    PRODUCT_UNAVAILABLE = "product_unavailable"
    COMPARISON_TARGET_UNAVAILABLE = "comparison_target_unavailable"
    REFERENCE_UNRESOLVED = "reference_unresolved"
    RESPONSE_UNAVAILABLE = "response_unavailable"

    LOCKED_PRODUCT_UNAVAILABLE = "locked_product_unavailable"
    """A piece the customer asked to keep could not be verified.

    Deliberately not `PRODUCT_UNAVAILABLE`: that one means a product someone
    referred to could not be read, and the turn continues around it. This means
    a room cannot be planned at all without either dropping a lock or inventing
    facts about it, and neither is allowed (CLAUDE.md 10).
    """

    BUNDLE_NOT_VERIFIABLE = "bundle_not_verifiable"
    """The room's current contents could not all be confirmed.

    Deliberately not `PRODUCT_UNAVAILABLE`: that one says the piece they meant
    is gone, and saying it here would be false - their piece may be perfectly
    fine, and it is something else in the room we could not read.
    """

    NO_REPLACEMENT_CANDIDATE = "no_replacement_candidate"
    """Nothing else of that kind, once what they turned down is excluded."""

    REPLACEMENT_NOT_FEASIBLE = "replacement_not_feasible"
    """Something exists; it does not fit alongside the rest of the room.

    Deliberately distinct from the above: a budget that would not stretch must
    never be reported as a catalog with nothing in it.
    """

    DESIGN_UNAVAILABLE = "design_unavailable"
    """No authoritative room plan could be produced.

    Infrastructure, not inventory: the capability lookup, the design provider
    or the design output itself failed. It must never be reported as the
    retailer having nothing suitable, which is a fact about the catalog rather
    than about us.
    """


class TurnFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: TurnFailureCode


class GroundedProduct(BaseModel):
    """One verified product, as this turn may talk about it.

    Two handles, because they answer different questions. `grounding_ref` is
    what the response model cites and is always present. `presented_ordinal`
    is a position in *this* turn's result list, and is None for a product
    grounded by another route - answering "what did the one I selected cost?"
    grounds a product that has no current position, and inventing one would
    claim a place on screen it does not have.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    grounding_ref: int = Field(ge=1)
    presented_ordinal: int | None = Field(default=None, ge=1)

    name_english: str
    price_amount: Decimal
    price_unit: str
    image_url: str
    product_url: str
    commerce: CommerceClassification
    dimensions: NormalisedDimensions
    main_color: str | None = None
    styles: tuple[str, ...] = ()

    relaxation_depth: int | None = Field(default=None, ge=0)
    """How far a search widened before this product became eligible, or None
    when that is not known.

    Provenance, never a score. Zero satisfied the customer's own request;
    above zero needed a widened bound, which the reply must say out loud.

    None is the honest answer for a product grounded by any route other than
    the current search - a comparison, a detail question, a selection made
    three turns ago. PostgreSQL can re-read what a product costs; it cannot
    reconstruct which widening once made it eligible. Defaulting to zero would
    assert `matched_exactly` about a search nobody ran.
    """

    @property
    def matched_exactly(self) -> bool | None:
        """Whether this product satisfied the customer's own request.

        Derived rather than stored, so it cannot disagree with the depth, and
        three-valued rather than two, so "we do not know" is sayable. A bare
        False would claim the product needed widening; a bare True would claim
        it did not.
        """
        if self.relaxation_depth is None:
            return None
        return self.relaxation_depth == 0


class RelaxationSummaryItem(BaseModel):
    """One bound that moved, and the permission that allowed it.

    Enough for a reply to say "I widened your 5,000 budget to 5,500" without
    the model computing anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: RelaxableField
    role: DimensionRole | None = None
    strength: ConstraintStrength
    original_value: str
    applied_value: str

    @model_validator(mode="after")
    def _a_measurement_names_its_role(self) -> Self:
        if self.field is RelaxableField.DIMENSION and self.role is None:
            raise ValueError("a widened measurement must say which one")
        if self.field is not RelaxableField.DIMENSION and self.role is not None:
            raise ValueError("only a measurement carries a role")
        return self


class DroppedConstraint(BaseModel):
    """Something the customer asked for that this search could not keep.

    Produced when a product type changes and the new one cannot support a
    measurement the old one did. Surfaced so the reply says so; a silent drop
    would present results as satisfying a request they do not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: DimensionRole | None = None
    reason: UnsupportedDimensionReason


class SearchExecutionGrounding(BaseModel):
    """What one executed search produced, verified and safe to explain.

    The counts are kept apart on purpose, and narrow in one direction:

        eligible == ranked -> selected -> presented

    Ranking reorders and never filters, so the first two are equal; after that
    each step can only lose products, and each loses them for a different
    reason. Collapsing any two would make a stale catalog row indistinguishable
    from a display limit, or hide the bounded-pool truncation this milestone
    removed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: SearchOutcome
    products: tuple[GroundedProduct, ...] = ()

    eligible_count: int = Field(default=0, ge=0)
    """What the catalog holds for this request, across every attempt."""

    ranked_count: int = Field(default=0, ge=0)
    """What ordering considered. No candidate is dropped by ranking."""

    selected_count: int = Field(default=0, ge=0)
    """What the presentation limit chose, before hydration.

    The field that keeps two different reductions apart: a product omitted by
    the limit never reached hydration, and a product lost at hydration was
    already chosen. Without this, a stale catalog row would be indistinguishable
    from a display truncation.
    """

    presented_count: int = Field(default=0, ge=0)
    """What survived hydration and was rendered."""

    exact_candidate_count: int = Field(default=0, ge=0)
    was_relaxed: bool = False
    relaxations: tuple[RelaxationSummaryItem, ...] = ()
    stop_reason: StopReason
    dropped_constraints: tuple[DroppedConstraint, ...] = ()

    semantic_used: bool = False
    semantic_skip_reason: SemanticSkipReason | None = None

    @property
    def stale_dropped_count(self) -> int:
        """Selected products the catalog could no longer supply.

        Derived, so it cannot disagree with the counts it is computed from.
        An expected race - a product is deactivated between the search and the
        read - and never a failure of the search itself.
        """
        return self.selected_count - self.presented_count

    @property
    def truncated_for_presentation(self) -> bool:
        """Products omitted **by the presentation limit**, and nothing else.

        Not staleness, not M8 stopping, not semantic degradation, not
        chunking. Derived rather than stored because a boolean somebody sets
        by hand is a boolean that eventually disagrees with the counts - which
        is exactly how a disappearing catalog row would come to be reported as
        a display truncation.
        """
        return self.selected_count < self.ranked_count

    @model_validator(mode="after")
    def _counts_describe_the_products(self) -> Self:
        if self.presented_count != len(self.products):
            raise ValueError("presented_count must equal the products carried")
        if (self.outcome is SearchOutcome.ZERO_RESULTS) != (self.presented_count == 0):
            raise ValueError("a zero-result outcome presents nothing, and vice versa")
        if self.was_relaxed != bool(self.relaxations):
            raise ValueError("was_relaxed must match the relaxations recorded")
        if self.semantic_used and self.semantic_skip_reason is not None:
            raise ValueError("ranking either happened or was skipped, not both")
        # One chain, narrowing at each stage: the limit chooses some of what
        # was ranked, and hydration keeps some of those.
        if not (self.presented_count <= self.selected_count <= self.ranked_count):
            raise ValueError(
                "counts must narrow: presented <= selected <= ranked"
            )
        # Ranking decides order, never eligibility, so every eligible product
        # is ranked. A smaller ranked count would mean candidates were lost
        # between the catalog and the ordering (CLAUDE.md 16.1).
        if self.ranked_count != self.eligible_count:
            raise ValueError("ranking considers every eligible product")
        return self

    @model_validator(mode="after")
    def _search_results_know_their_provenance(self) -> Self:
        """Every product here came from the search this object describes.

        So its first-seen relaxation depth is known, and losing it would mean
        a pipeline had discarded provenance M8 established. Unknown depth is
        legitimate elsewhere - a comparison, a detail - but not here.
        """
        if any(p.relaxation_depth is None for p in self.products):
            raise ValueError(
                "a searched product carries the depth at which it became eligible"
            )
        return self

    @model_validator(mode="after")
    def _grounding_refs_are_unique_and_ordered(self) -> Self:
        refs = [p.grounding_ref for p in self.products]
        if refs != list(range(1, len(refs) + 1)):
            raise ValueError("grounding refs must be 1..n in order")
        ordinals = [
            p.presented_ordinal for p in self.products if p.presented_ordinal is not None
        ]
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise ValueError("presented ordinals must be 1..n in order")
        return self
