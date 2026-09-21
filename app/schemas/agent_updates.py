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

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.agent_state import PurchaseStage
from app.schemas.discovery import PriceConstraint, ProductSearchRequest
from app.schemas.query import ConstraintSemantics, SemanticPreference

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
    AddItems[SemanticPreference] | RemoveItems[SemanticPreference]
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


SemanticIntentUpdate = Annotated[
    SetSemanticIntent | ClearSemanticIntent, Field(discriminator="op")
]


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


class RoomProjectUpdate(BaseModel):
    """Every field is customer-supplied, so the Customer/Commerce Agent may
    propose all of them. The Interior Design Agent owns none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_type: str | None = None
    clear_room_type: bool = False
    budget: PriceConstraint | None = None
    clear_budget: bool = False
    design_preferences: PreferenceListUpdate | None = None
    bundle_product_ids: ProductIdListUpdate | None = None
    locked_product_ids: ProductIdListUpdate | None = None


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
