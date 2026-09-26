"""Typed, bounded state updates.

A future agent proposes changes; it does not write state. It also does not
resend state it is not changing, which is the whole point: an update that omits
a domain provably cannot alter it, so "show me cheaper ones" cannot silently
drop a locked product or a preference recorded three turns ago.

Two shapes carry the design.

* **Tuple fields take an explicit verb** - add, remove or replace - so a list is
  never cleared by being left out, and clearing is something someone asked for.
* **Optional scalars take a tagged operation** - set or clear - because a bare
  ``None`` cannot say whether it means "leave it alone" or "empty it", and
  guessing between those two is how durable context disappears.

Absence always means untouched.

Nothing here can commit a search result. Which products were presented is a
fact about what discovery executed, not a change an agent may propose, so it
lives behind `commit_search_results` in the service and has no field on this
schema at all. An agent cannot fabricate a result set it did not run.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_state import BundleItemStatus, PurchaseStage
from app.schemas.design import MAX_REGULAR_SEATING_COUNT, DesignPriority
from app.schemas.discovery import PriceConstraint, ProductSearchRequest, SeatingCapacityConstraint
from app.schemas.geometry import RoomGeometry
from app.schemas.query import ConstraintSemantics, SemanticPreference
from app.schemas.room_opener import RoomQuestionKind

# ── tuple operations ────────────────────────────────────────────────────────


class AddItems[ItemT](BaseModel):
    """Append items not already present, preserving existing order."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["add"] = "add"
    items: tuple[ItemT, ...]


class RemoveItems[ItemT](BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["remove"] = "remove"
    items: tuple[ItemT, ...]


class ReplaceItems[ItemT](BaseModel):
    """The whole list, deliberately. Available, but never the only option."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["replace"] = "replace"
    items: tuple[ItemT, ...]


PreferenceListUpdate = Annotated[
    AddItems[SemanticPreference]
    | RemoveItems[SemanticPreference]
    | ReplaceItems[SemanticPreference],
    Field(discriminator="op"),
]
ProductIdListUpdate = Annotated[
    AddItems[int] | RemoveItems[int] | ReplaceItems[int],
    Field(discriminator="op"),
]


def apply_items[ItemT](
    current: tuple[ItemT, ...],
    update: AddItems[ItemT] | RemoveItems[ItemT] | ReplaceItems[ItemT] | None,
) -> tuple[ItemT, ...]:
    """The one place a tuple operation is interpreted.

    `None` returns the list untouched, which is what makes omission safe.
    """
    if update is None:
        return current
    if isinstance(update, ReplaceItems):
        return tuple(update.items)
    if isinstance(update, AddItems):
        return current + tuple(i for i in update.items if i not in current)
    removed = set(update.items)
    return tuple(i for i in current if i not in removed)


# ── optional scalar operations ──────────────────────────────────────────────


class SetSemanticIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["set"] = "set"
    value: str


class ClearSemanticIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["clear"] = "clear"


SemanticIntentUpdate = Annotated[SetSemanticIntent | ClearSemanticIntent, Field(discriminator="op")]


# ── domain updates ──────────────────────────────────────────────────────────


class CustomerPreferenceUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    semantic_preferences: PreferenceListUpdate | None = None


class ActiveSearchUpdate(BaseModel):
    """Search criteria only.

    There is deliberately no `revision` field: an agent must not be able to
    claim a search executed, or to move the lineage backwards.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ProductSearchRequest | None = None
    semantics: ConstraintSemantics | None = None
    semantic_preferences: PreferenceListUpdate | None = None
    semantic_intent: SemanticIntentUpdate | None = None


class ProductInteractionUpdate(BaseModel):
    """Focus and selection only.

    `presented_product_ids` and `presented_search_revision` are absent on
    purpose: a presented list is produced by an executed search and is
    committed through `RecordSearchResults`, atomically with the revision it
    belongs to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    focused_product_id: int | None = None
    clear_focus: bool = False
    selected_product_ids: ProductIdListUpdate | None = None

    compared_product_ids: tuple[int, ...] | None = None
    """The comparison now on screen, in column order, replacing any before it.

    Set by the comparison branch from products it has already verified, never
    proposed by a model. `None` leaves the previous comparison in place, which
    is what an unrelated turn should do: a comparison stays on screen until
    something replaces it.
    """


# ── the room bundle ─────────────────────────────────────────────────────────
#
# Typed operations rather than independent tuple fields, because quantity,
# acquisition and status belong to a line and must not be able to drift apart
# from the product they describe.


class BundleLineSpec(BaseModel):
    """A line to create. Carries no `line_id`: the reducer allocates it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: int = Field(ge=1)
    quantity: int = Field(default=1, ge=1)
    acquisition: BundleAcquisition
    """Required, and never defaulted here. A lock proves preservation and says
    nothing about purchase, so the caller that heard the customer states it."""

    status: BundleItemStatus = BundleItemStatus.SUGGESTED

    need_id: int | None = Field(default=None, ge=1)
    """The durable need this line fills, when the plan it belongs to already
    exists. A commit that is *creating* the plan cannot name one yet and uses
    :class:`PlannedBundleLineSpec` instead."""


class PlannedBundleLineSpec(BaseModel):
    """A line created alongside the plan it belongs to.

    Identical to :class:`BundleLineSpec` but for the last field, and separate
    because that field means something different: `need_index` is a position in
    the plan being committed, which the reducer maps to the id it has just
    allocated. One type carrying both meanings would need a flag to say which,
    and a flag is exactly how the wrong one gets read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: int = Field(ge=1)
    quantity: int = Field(default=1, ge=1)
    acquisition: BundleAcquisition
    status: BundleItemStatus = BundleItemStatus.SUGGESTED
    need_index: int | None = Field(default=None, ge=0)
    """Position in `ReplaceDesignPlan.needs`, or None for a line no need
    claims. Validated against the plan's length by the reducer."""


class DesignNeedSpec(BaseModel):
    """One need of a plan being committed. Carries no id: the reducer allocates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)
    priority: DesignPriority
    quantity: int = Field(default=1, ge=1)
    seating_capacity: SeatingCapacityConstraint | None = None
    semantic_intent: str | None = None


class DesignNeedRefinement(BaseModel):
    """A change to one role of the plan the customer already has.

    Only the two things a refinement can change about a role: what it has been
    turned down for, and how it should read. Its kind, quantity, priority and
    seat count are design decisions, and a product substitution does not touch
    them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    need_id: int = Field(ge=1)
    rejected_product_ids: tuple[int, ...] | None = None
    """The role's full rejection list, or None to leave it. Replaced whole, so
    the caller that staged it decides what survives."""

    semantic_intent: str | None = None
    clear_semantic_intent: bool = False

    @model_validator(mode="after")
    def _setting_and_clearing_are_exclusive(self) -> Self:
        if self.clear_semantic_intent and self.semantic_intent is not None:
            raise ValueError("wording cannot be set and cleared at once")
        return self


class RefineBundle(BaseModel):
    """A re-optimised room, with the role changes that produced it.

    One operation, because they are one proposal. A staged rejection or a new
    phrase is only true of the room that was chosen under it: persisting either
    beside a bundle selected without it would make state claim the customer's
    current sofa was picked for a reason it never satisfied.

    Removals and refinements travel together so a plan edit and the room it
    forces cannot land separately either. The plan's identity is untouched -
    same ids, same order, same allocator - because refining a room is not
    replanning one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["refine_bundle"] = "refine_bundle"

    refinements: tuple[DesignNeedRefinement, ...] = ()
    removed_need_ids: tuple[int, ...] = ()
    preserved: tuple[PreservedBundleLine, ...] = ()
    added: tuple[BundleLineSpec, ...] = ()
    """Lines naming needs that already exist, so they carry `need_id`."""

    @model_validator(mode="after")
    def _each_line_and_need_appears_once(self) -> Self:
        for label, ids in (
            ("line", [entry.line_id for entry in self.preserved]),
            ("need", [entry.need_id for entry in self.refinements]),
            ("removal", list(self.removed_need_ids)),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"a {label} appears at most once in a refinement")
        return self


class ReplaceDesignPlan(BaseModel):
    """A new furnishing plan and the room chosen from it, in one step.

    Application-only, and deliberately not reachable from any model output.

    Separate from `ReplaceBundle` because it does something that one cannot: a
    plan and the bundle built from it are a single coherent proposal, and there
    must be no state in which new needs sit beside a bundle that was chosen
    from different ones. Adding a flag to `ReplaceBundle` would have made that
    difference invisible at the call site.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["replace_design_plan"] = "replace_design_plan"

    needs: tuple[DesignNeedSpec, ...] = ()
    preserved: tuple[PreservedBundleLine, ...] = ()
    """Locked lines carried across. They keep their identity and lose their
    need: the roles they belonged to no longer exist, and matching them onto
    new ones would be a guess."""

    added: tuple[PlannedBundleLineSpec, ...] = ()

    @model_validator(mode="after")
    def _a_line_is_preserved_at_most_once(self) -> Self:
        ids = [entry.line_id for entry in self.preserved]
        if len(ids) != len(set(ids)):
            raise ValueError("a line can be preserved only once")
        return self


class AddBundleLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["add_line"] = "add_line"
    line: BundleLineSpec


class RemoveBundleLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["remove_line"] = "remove_line"
    line_id: int = Field(ge=1)


class SetBundleLineQuantity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["set_quantity"] = "set_quantity"
    line_id: int = Field(ge=1)
    quantity: int = Field(ge=1)


class SetBundleLineAcquisition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["set_acquisition"] = "set_acquisition"
    line_id: int = Field(ge=1)
    acquisition: BundleAcquisition


class SetBundleLineStatus(BaseModel):
    """Lock and unlock, which are one operation with two values.

    Two named operations would be two ways to write the same field, and a
    reducer would then have to decide which wins when both appear.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["set_status"] = "set_status"
    line_id: int = Field(ge=1)
    status: BundleItemStatus


class PreservedBundleLine(BaseModel):
    """A line already in the bundle, carried across a re-plan untouched.

    An id and nothing else, deliberately. Anything else here would be a field a
    caller could change while calling it preservation - and the caller most
    likely to try is a whole-room commit, which has no way to know which state
    line an optimiser's locked output came from. The reducer copies the
    existing line byte for byte, so there is nothing to get wrong.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: int = Field(ge=1)


class ReplaceBundle(BaseModel):
    """The whole bundle, atomically.

    Two lists, because the two halves are different things. `preserved` names
    lines that already exist and must survive with their identity intact - the
    pieces a customer said to keep. `added` describes lines that do not exist
    yet and take fresh ids.

    Anything in neither list is dropped, which is what a re-plan does to last
    round's suggestions. There is no intermediate empty bundle.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    op: Literal["replace_bundle"] = "replace_bundle"
    preserved: tuple[PreservedBundleLine, ...] = ()
    added: tuple[BundleLineSpec, ...] = ()

    @model_validator(mode="after")
    def _a_line_is_preserved_at_most_once(self) -> Self:
        ids = [entry.line_id for entry in self.preserved]
        if len(ids) != len(set(ids)):
            raise ValueError("a line can be preserved only once")
        return self


BundleOperation = Annotated[
    RefineBundle
    | ReplaceDesignPlan
    | AddBundleLine
    | RemoveBundleLine
    | SetBundleLineQuantity
    | SetBundleLineAcquisition
    | SetBundleLineStatus
    | ReplaceBundle,
    Field(discriminator="op"),
]


class RoomProjectUpdate(BaseModel):
    """Every field is customer-supplied, so the Customer/Commerce Agent may
    propose all of them. The Interior Design Agent owns none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_type: str | None = None
    clear_room_type: bool = False
    geometry: RoomGeometry | None = None
    clear_geometry: bool = False
    budget: PriceConstraint | None = None
    clear_budget: bool = False
    design_preferences: PreferenceListUpdate | None = None
    regular_seating_count: int | None = Field(
        default=None, ge=1, le=MAX_REGULAR_SEATING_COUNT
    )
    clear_regular_seating_count: bool = False
    room_kind: str | None = None
    """A different kind resets the chosen pieces and the opening question: the
    bedroom's pieces say nothing about a living room."""

    pieces: tuple[str, ...] | None = None
    """Replaces the chosen pieces. Registry keys already checked by the
    application; a model's raw keys never land here unchecked."""

    question_asked: RoomQuestionKind | None = None
    """Application-only: the room question asked this turn."""

    questions_done: bool = False
    """They asked to skip the remaining room questions."""

    bundle_operations: tuple[BundleOperation, ...] = ()
    """Changes to the room bundle, applied in order.

    **Application-only.** `map_proposals` never populates this, so no model
    output can reach it: bundle membership is resolved by application code from
    a verified product reference, exactly as `commit_search_results` keeps
    presented ids out of an agent's hands.

    A whole-room commit is one `ReplaceBundle`, not a clear followed by adds -
    an intermediate empty bundle is a state the customer never had.
    """


class DerivedCommerceUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    purchase_stage: PurchaseStage | None = None
    clear_purchase_stage: bool = False


class AgentStateUpdate(BaseModel):
    """One turn's proposed changes. A `None` domain is untouched."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_preferences: CustomerPreferenceUpdate | None = None
    active_search: ActiveSearchUpdate | None = None
    product_interaction: ProductInteractionUpdate | None = None
    room_project: RoomProjectUpdate | None = None
    derived_commerce: DerivedCommerceUpdate | None = None
