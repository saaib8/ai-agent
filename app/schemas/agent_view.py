"""What the model is allowed to see of the conversation's state.

`AgentStateV1` is the authoritative structured memory. This is a projection of
it for one purpose: giving a language model enough context to reason, and
nothing it could act on unsafely.

Two things are deliberately absent.

**Every product id.** The whole authority design rests on the model never
emitting one. Serialising ids into the prompt is what makes emitting one easy,
and a plausible-looking id is exactly the hallucination a membership check
waves through. So products appear here as positions and counts: enough to say
"the second one", never enough to name it.

**Retailer scope.** `store_id` lives on `RetailerContext`, which application
code passes straight to the repositories. There is no decision a model makes
better for knowing it (CLAUDE.md 8, 20.2).

This is a projection, not a second state model: nothing writes it back, and no
service reads it to make a decision. The pure conversion from `AgentStateV1`
belongs to the layer that builds model input, not here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.agent_state import PurchaseStage
from app.schemas.discovery import DimensionConstraintKind, ProductSort
from app.schemas.query import ConstraintStrength
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.dimensions import DimensionRole


class PreferenceView(BaseModel):
    """A colour or style leaning, in the customer's own words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family: AttributeFamily
    raw_value: str
    canonical_value: str | None = None
    strength: ConstraintStrength


class PriceView(BaseModel):
    """A money bound and how firmly it was expressed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: str
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    min_exclusive: bool = False
    max_exclusive: bool = False
    min_strength: ConstraintStrength | None = None
    max_strength: ConstraintStrength | None = None


class CapacityView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_capacity: int | None = None
    max_capacity: int | None = None
    min_strength: ConstraintStrength | None = None
    max_strength: ConstraintStrength | None = None


class DimensionView(BaseModel):
    """One measurement, in the customer's physical terms - never a column."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: DimensionRole
    kind: DimensionConstraintKind
    min_cm: Decimal | None = None
    max_cm: Decimal | None = None
    target_cm: Decimal | None = None
    source_value: str | None = None
    source_unit: str | None = None
    strength: ConstraintStrength


class PlanarView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    first_cm: Decimal
    second_cm: Decimal
    source_unit: str | None = None
    strength: ConstraintStrength


class ActiveSearchView(BaseModel):
    """The search in progress, as criteria rather than as a query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str
    commerce_subcategory: str | None = None
    price: PriceView | None = None
    seating_capacity: CapacityView | None = None
    dimensions: tuple[DimensionView, ...] = ()
    planar_dimensions: PlanarView | None = None
    required_colors: tuple[str, ...] = ()
    """Exact colour requirements - the customer ruled the alternatives out."""

    required_styles: tuple[str, ...] = ()
    attribute_preferences: tuple[PreferenceView, ...] = ()
    """Colour and style leanings. Not filters (CLAUDE.md 12.4)."""

    semantic_intent: str | None = None
    sort: ProductSort = ProductSort.DEFAULT

    # `revision` is absent: it is execution bookkeeping, and a model that
    # could see it could start reasoning about search lineage.


class PresentedProductsView(BaseModel):
    """What the customer is looking at, without naming any of it.

    `count` is what makes an ordinal meaningful: it tells the model that
    saying "the second one" is possible, and how far the list goes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int = Field(default=0, ge=0)
    has_focused_product: bool = False
    selected_count: int = Field(default=0, ge=0)
    selected_ordinals: tuple[int, ...] = ()
    """Positions within the presented list, for selections still visible there.

    A selection reached by another path has no position, so this can be
    shorter than `selected_count`. It is never padded to match.
    """

    @model_validator(mode="after")
    def _ordinals_are_within_the_presented_list(self) -> Self:
        for ordinal in self.selected_ordinals:
            if not 1 <= ordinal <= self.count:
                raise ValueError("a selected ordinal must name a presented position")
        if len(set(self.selected_ordinals)) != len(self.selected_ordinals):
            raise ValueError("selected_ordinals must not repeat a position")
        if len(self.selected_ordinals) > self.selected_count:
            raise ValueError("more selected ordinals than selected products")
        return self


class RoomProjectView(BaseModel):
    """A whole-room task's customer-supplied requirements.

    Bundle membership appears as counts. Which products are in the room is a
    fact the application resolves, not a list for the model to edit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_type: str | None = None
    budget: PriceView | None = None
    design_preferences: tuple[PreferenceView, ...] = ()
    bundle_item_count: int = Field(default=0, ge=0)
    locked_item_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _locked_within_bundle(self) -> Self:
        if self.locked_item_count > self.bundle_item_count:
            raise ValueError("locked_item_count cannot exceed bundle_item_count")
        return self


class AgentStateView(BaseModel):
    """Model input only. Never persisted, never written back.

    `customer_preferences` is projected because "what style did I say I liked?"
    is answerable only from it. It is context for answering, not criteria to
    reapply: composing search criteria from preferences is the deterministic
    composer's job.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    active_search: ActiveSearchView | None = None
    customer_preferences: tuple[PreferenceView, ...] = ()
    presented: PresentedProductsView = PresentedProductsView()
    room_project: RoomProjectView | None = None
    purchase_stage: PurchaseStage | None = None
