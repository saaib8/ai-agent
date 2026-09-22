"""Structured working memory for the future reasoning agents.

What belongs here is what a conversation needs to *remember*: the customer's
stated preferences, the task in progress, which products have been referred to,
and the system's own commercial read. Nothing else.

Three boundaries hold the design together, and each exists because crossing it
would create a second copy of something that already has an owner.

* **State remembers references; PostgreSQL remembers facts.** A product id here,
  never a name, price or URL. A remembered price is a wrong price.
* **Retailer scope is not state.** `store_id` lives on `RetailerContext`,
  supplied by application code, so no agent can change which catalog it sees.
* **Raw conversation is not state.** Messages belong to the conversation-history
  layer and reach an agent beside this object, never inside it.

Every model is frozen: a new state is built through the real constructors by
the reducer in `app/services/agent_state.py`, so every invariant is re-checked
on every transition rather than trusted to hold.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.acquisition import BundleAcquisition
from app.schemas.design import DesignPriority
from app.schemas.design_intent import (
    MAX_DESIGN_INTENT_CHARS,
    normalise_design_intent,
)
from app.schemas.discovery import (
    MAX_EXCLUDED_PRODUCT_IDS,
    PriceConstraint,
    ProductSearchRequest,
    SeatingCapacityConstraint,
)
from app.schemas.geometry import RoomGeometry
from app.schemas.query import (
    ConstraintSemantics,
    SemanticPreference,
    validate_dimension_correspondence,
)

AGENT_STATE_VERSION: Literal["agent_state_v5"] = "agent_state_v5"
"""The state contract. Change the shape, change this.

V2 replaced the room bundle's two id tuples with typed lines. V3 made the
furnishing plan itself durable and renamed a bundle line's `need_index` to
`need_id` - an execution-local position becoming a lasting identity. V4 adds
the room's regular seating requirement, which is a durable customer fact a
later turn must not have to re-ask for.

Each is a shape change a reader could get wrong, so the version moves with it.

A session written by an older version is refused rather than coerced: the store
validates through this contract, and an unreadable session is a controlled
failure (M13 7). No migration is written, because the product has not launched
and inventing one would be maintaining a path nobody has travelled."""

MAX_SEMANTIC_INTENT_CHARS = 200
"""A quality phrase is a few words. This bound is the only thing standing
between a durable search property and a smuggled transcript."""


def _no_duplicates(values: tuple[int, ...], field: str) -> tuple[int, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field} must not contain duplicate product ids")
    return values


class CustomerPreferenceState(BaseModel):
    """What the customer actually said they like, reusable across tasks.

    Only expressed preferences. A speculative inference that became durable
    state would be indistinguishable from something they told us.

    No global budget: a budget belongs to the task that has one, so there is
    exactly one per scope rather than three that can disagree. No material
    preference in V1 either - nothing can filter on it, so representing it
    would promise something the catalog cannot keep.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    semantic_preferences: tuple[SemanticPreference, ...] = ()


class ActiveSearchState(BaseModel):
    """The product discovery task in progress.

    Deliberately `ResolvedSearch` minus `semantic_text`, plus `revision`.
    Composed rather than wrapped, so the per-turn field cannot arrive by
    inheritance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ProductSearchRequest
    semantics: ConstraintSemantics = ConstraintSemantics()
    semantic_preferences: tuple[SemanticPreference, ...] = ()

    semantic_intent: str | None = None
    """Durable fuzzy wording that no structured field can hold - cosy, elegant,
    sleek.

    `SemanticPreference` covers colour and style only, so a quality word has no
    attribute family to be filed under and would otherwise vanish between
    turns: "show me cheaper ones" after "cosy modern sofas" would quietly
    become a search for modern sofas.

    It is never executable truth. Nothing filters on it, and no service reads
    its contents to make a decision.
    """

    revision: int = Field(ge=0)
    """Identifier of the most recently committed search result set.

    Zero means no results have been committed yet - criteria exist, nothing has
    been presented. One is the first presented result set, two the second.

    Not a version counter for this object: changing the criteria does not
    execute anything. It advances only when results are committed, which is why
    no update contract can set it and only
    :func:`~app.services.agent_state.commit_search_results` moves it.
    """

    @field_validator("semantic_intent")
    @classmethod
    def _normalise_intent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        if len(trimmed) > MAX_SEMANTIC_INTENT_CHARS:
            raise ValueError(
                f"semantic_intent must be at most {MAX_SEMANTIC_INTENT_CHARS} characters"
            )
        return trimmed

    @model_validator(mode="after")
    def _check_dimensions(self) -> Self:
        validate_dimension_correspondence(self.request, self.semantics)
        return self


class ProductInteractionState(BaseModel):
    """Which products the conversation has referred to.

    References only. Order in `presented_product_ids` is what lets a later
    layer resolve "the second one", so it is a tuple rather than a set by
    design.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    presented_product_ids: tuple[int, ...] = ()
    presented_search_revision: int | None = None
    focused_product_id: int | None = None
    selected_product_ids: tuple[int, ...] = ()

    compared_product_ids: tuple[int, ...] = ()
    """The products of the comparison currently on screen, in column order.

    A comparison puts a **second numbering** in front of the customer. Sofas 3
    and 5 of a list of five become columns one and two, and "the second one"
    then has two truthful answers. Recorded separately because it has to be
    separately addressable: without it, a column can only be named by its
    position in the underlying list, which is not the number the customer is
    looking at (M15 1).

    It outlives the list it was drawn from on purpose. A comparison stays on
    screen while a later search replaces the results behind it, and "the one
    from the comparison" has to keep meaning the same product when it does.

    Cleared when a new comparison replaces it, never merged: two comparisons
    at once would recreate the ambiguity this exists to remove.
    """

    @field_validator(
        "presented_product_ids", "selected_product_ids", "compared_product_ids"
    )
    @classmethod
    def _unique(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        return _no_duplicates(value, "product id list")

    @model_validator(mode="after")
    def _focus_is_known(self) -> Self:
        """A focus on a product nobody has seen cannot be resolved.

        Selected and compared products count as known: a customer may reach
        one by a path other than the current result list, so neither is
        required to be a subset of what is presented.
        """
        if self.focused_product_id is None:
            return self
        known = (
            set(self.presented_product_ids)
            | set(self.selected_product_ids)
            | set(self.compared_product_ids)
        )
        if self.focused_product_id not in known:
            raise ValueError("focused_product_id must be a presented or selected product")
        return self


class BundleItemStatus(StrEnum):
    """What a bundle line's presence means for the next optimisation.

    Two states, because a third would behave like one of these. "Accepted"
    either survives re-optimisation - in which case it is `LOCKED` under
    another name - or it does not, in which case it is `SUGGESTED`. "Rejected"
    is not a state of an active line at all: a rejected product leaves the
    bundle. "Replaced" is event history, and V1 stores current state.
    """

    SUGGESTED = "suggested"
    """The application proposed it. A later optimisation may replace it."""

    LOCKED = "locked"
    """The customer asked to keep it. Preserved until they say otherwise, and
    never unlocked automatically (CLAUDE.md 10)."""


class BundleItemState(BaseModel):
    """One line of the room bundle: a reference, and what it means.

    **References, never facts.** No name, price, currency, url, image,
    dimension, colour, style, category, rank or relaxation depth. PostgreSQL
    is product truth and a remembered price is a wrong price; everything here
    is either an identifier or something the customer decided.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: int = Field(ge=1)
    """Stable application identity for this line.

    Needed because a product id is not unique here: the same product may fill
    two roles, and a lock covering no current need has no need to name it by.
    Application-assigned, never model-authored, and never catalog identity.
    """

    product_id: int = Field(ge=1)
    quantity: int = Field(ge=1)
    """Units of this product. Multiplies price and asserts nothing about stock."""

    acquisition: BundleAcquisition
    status: BundleItemStatus

    need_id: int | None = Field(default=None, ge=1)
    """The durable design need this line fills, or None.

    An identity, not a position. `BundleLine.need_index` and
    `UnmetNeed.need_index` remain execution-local plan-order indexes that mean
    nothing once their `InteriorDesignResult` is gone; this is what the
    application allocated so a later turn can still say which role a piece
    plays.

    `None` is ordinary and has two causes: a piece the customer asked to keep
    that the current plan has no place for, and a lock carried across a plan
    replacement, whose old role no longer exists.

    Not unique: a plan may want several of one kind, and two lines may fill one
    need.
    """


class RoomDesignNeedState(BaseModel):
    """One product type the current furnishing plan calls for.

    The durable half of a `DesignCategoryNeed`. It exists because a bundle line
    pointing at plan position 2 means nothing once that plan has been discarded,
    and every later refinement - replacing a piece, remembering a rejection,
    naming a missing role - has to know which need is being talked about.

    Plan-owned, never customer-owned. Nothing here is attributed to the
    customer: `design_preferences` on the room is what *they* said, and this is
    what the specialist concluded about one role.

    No product fact of any kind. `rejected_product_ids` is the single exception
    and is an application-only exclusion list, not a catalog value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    need_id: int = Field(ge=1)
    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)
    priority: DesignPriority
    quantity: int = Field(ge=1)
    seating_capacity: SeatingCapacityConstraint | None = None

    semantic_intent: str | None = Field(default=None, max_length=MAX_DESIGN_INTENT_CHARS)
    """This role's qualitative character, as the specialist described it.

    **A deliberate amendment to M12C.1**, which kept this transient. The rule
    it protected was that a ranking hint must not become durable *customer
    preference* state, indistinguishable from something they said. Kept here it
    is plan-owned, cleared when the plan is replaced, and absent from
    `CustomerPreferenceState` and `RoomProjectState.design_preferences` - so
    that rule still holds.

    It is persisted because replacing "the visually light reading chair" on a
    later turn would otherwise fall back to the bare product type plus the
    room's preferences, losing precisely the per-need ranking M12C.1 was
    created to add - and re-running the specialist to recover it is what
    ordinary substitution must not do.
    """

    rejected_product_ids: tuple[int, ...] = ()
    """Products the customer turned down **for this role**.

    Application-only and never model-visible. Scoped to the need rather than
    kept globally, so refusing one sofa says nothing about lamps and nothing
    about the next room. It dies with the need: a replaced plan starts clean
    rather than carrying a dislike forward into an unrelated design.

    Bounded by the same ceiling a search already applies to exclusions, so
    there is one answer to "how many" rather than two.
    """

    @field_validator("semantic_intent")
    @classmethod
    def _intent_is_ranking_prose_only(cls, value: str | None) -> str | None:
        """The same rule the specialist's own field applies."""
        return normalise_design_intent(value)

    @field_validator("rejected_product_ids")
    @classmethod
    def _exclusions_are_bounded_and_distinct(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(product_id < 1 for product_id in value):
            raise ValueError("a product id is 1 or greater")
        if len(value) != len(set(value)):
            raise ValueError("a product is rejected at most once for a need")
        if len(value) > MAX_EXCLUDED_PRODUCT_IDS:
            raise ValueError(f"a need may exclude at most {MAX_EXCLUDED_PRODUCT_IDS} products")
        return value


class RoomProjectState(BaseModel):
    """A whole-room task's customer-supplied requirements.

    Everything here is something the customer said, including the room
    measurements: `geometry` holds figures they stated, never one inferred from
    a photograph, a room type or a design model's guess.

    Still deliberately short of a floor plan. Slots, layouts, coordinates and
    scores are not here, and a room described only as "my living room" is a
    perfectly ordinary state - partial knowledge, not missing data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_type: str | None = None
    """Free-text customer context. **Never a catalog filter**: there is no
    approved room-type vocabulary, so letting this reach SQL would be an
    invented taxonomy value (CLAUDE.md 14.3)."""

    geometry: RoomGeometry | None = None
    """Room measurements the customer stated, in centimetres.

    Durable because the conversation is: someone gives their room size in one
    turn and asks whether a sofa fits three turns later. Only ever what they
    said - nothing inferred, nothing a design model proposed - so an absent
    measurement means they have not given it, not that it is unknown to us.
    """

    budget: PriceConstraint | None = None
    design_preferences: tuple[SemanticPreference, ...] = ()

    regular_seating_count: int | None = Field(default=None, ge=1, le=30)
    """How many people regularly use this room, when the customer has said.

    A **room requirement**, not a product quantity and not a shopping list. It
    records that five people use the living room; it decides nothing about what
    is bought. Whether that becomes a sectional, a sofa and two chairs, or two
    sofas is the design specialist's reasoning against this retailer's catalog
    (CLAUDE.md 10.1).

    Durable because it must survive the turns between being said and being
    used: a customer who says "family of five" while giving a budget must not
    be asked again two turns later when the room is finally planned. History
    alone cannot carry it - history is trimmed.

    Only from what they actually said. Never inferred from a room type, a
    product's seating capacity, how many pieces are in the bundle, or anything
    else about their behaviour.
    """

    design_needs: tuple[RoomDesignNeedState, ...] = ()
    """The furnishing plan currently being worked on, in the specialist's own
    order. Empty until a room has been planned."""

    next_design_need_id: int = Field(default=1, ge=1)
    """The id the next design need will receive.

    A counter, like `next_bundle_line_id` and for the same reason: a reused id
    would let a stale reference to a discarded role quietly bind to a new one.
    A plan replacement therefore issues entirely fresh ids and old references
    fail closed.
    """

    bundle_items: tuple[BundleItemState, ...] = ()
    """The room bundle, one line per selection.

    Distinct from `ProductInteractionState.selected_product_ids`, which is a
    browsing shortlist - two different ideas that must not share a name.

    Lines rather than ids because an id cannot say how many, whether it is
    being bought, or whether the customer asked to keep it. Order is the
    bundle's own and carries no ranking.
    """

    next_bundle_line_id: int = Field(default=1, ge=1)
    """The id the next line will receive.

    A counter rather than `max(line_id) + 1`, because the latter reuses an id
    after the highest line is removed - and a reused id makes a stale reference
    silently resolve to a different product.
    """

    bundle_revision: int = Field(default=0, ge=0)
    """How many times the bundle's content has changed.

    Zero means nothing has ever been committed. Application-owned, exactly like
    `ActiveSearchState.revision`: no update contract can set it, and it moves
    only when the lines actually differ.
    """

    @model_validator(mode="after")
    def _need_ids_are_unique_and_never_reused(self) -> Self:
        ids = [need.need_id for need in self.design_needs]
        if len(ids) != len(set(ids)):
            raise ValueError("a design need id identifies exactly one need")
        if ids and self.next_design_need_id <= max(ids):
            raise ValueError("next_design_need_id must exceed every need id already issued")
        return self

    @model_validator(mode="after")
    def _every_line_names_a_need_that_exists(self) -> Self:
        """A line may name no need; it may not name one that is not there.

        `None` is ordinary - an anchor the plan has no place for, or a lock
        carried across a replacement. A dangling id is not: it would resolve to
        nothing on the turn that tried to refine it.
        """
        known = {need.need_id for need in self.design_needs}
        for line in self.bundle_items:
            if line.need_id is not None and line.need_id not in known:
                raise ValueError("a bundle line cannot name a design need that is gone")
        return self

    @model_validator(mode="after")
    def _line_ids_are_unique_and_never_reused(self) -> Self:
        ids = [item.line_id for item in self.bundle_items]
        if len(ids) != len(set(ids)):
            raise ValueError("a bundle line id identifies exactly one line")
        if ids and self.next_bundle_line_id <= max(ids):
            raise ValueError("next_bundle_line_id must exceed every line id already issued")
        return self

    @property
    def locked_product_ids(self) -> tuple[int, ...]:
        """Products the customer asked to keep, in bundle order.

        Derived, never stored: a second tuple could disagree with the lines it
        summarises. Deliberately a property rather than a computed field, so it
        cannot serialise as a rival source of truth.

        A product locked on two lines appears once - callers want the set of
        products to preserve, not a count of units.
        """
        return tuple(
            dict.fromkeys(
                item.product_id
                for item in self.bundle_items
                if item.status is BundleItemStatus.LOCKED
            )
        )

    def count(self, status: BundleItemStatus) -> int:
        return sum(1 for item in self.bundle_items if item.status is status)

    def count_acquisition(self, acquisition: BundleAcquisition) -> int:
        return sum(1 for item in self.bundle_items if item.acquisition is acquisition)


class PurchaseStage(StrEnum):
    EXPLORING = "exploring"
    CONSIDERING = "considering"
    HIGH_PURCHASE_INTENT = "high_purchase_intent"


class DerivedCommerceState(BaseModel):
    """The system's own commercial read, kept apart from what the customer said.

    `None` is not `EXPLORING`: absence means nothing has assessed the stage yet,
    the same way an absent `ConstraintStrength` is never read as consent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    purchase_stage: PurchaseStage | None = None


class AgentStateV1(BaseModel):
    """Structured working memory. Five domains, and nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["agent_state_v5"] = AGENT_STATE_VERSION
    customer_preferences: CustomerPreferenceState = CustomerPreferenceState()
    active_search: ActiveSearchState | None = None
    product_interaction: ProductInteractionState = ProductInteractionState()
    room_project: RoomProjectState | None = None
    derived_commerce: DerivedCommerceState = DerivedCommerceState()

    @model_validator(mode="after")
    def _presented_matches_the_executed_search(self) -> Self:
        """A presented list must name the search lineage that produced it.

        Without this, "the second one" could resolve against results the
        customer can no longer see.
        """
        revision = self.product_interaction.presented_search_revision
        if revision is None:
            return self
        if self.active_search is None:
            raise ValueError("presented_search_revision requires an active_search")
        if revision != self.active_search.revision:
            raise ValueError("presented_search_revision must equal active_search.revision")
        if revision < 1:
            raise ValueError("a presented result set implies a committed search revision")
        return self
