"""What the model is allowed to see of the conversation's state.

`AgentStateV1` is the authoritative structured memory. This is a projection of
it for one purpose: giving a language model enough context to reason, and
nothing it could act on unsafely.

Two things are deliberately absent.

**Every product id.** The whole authority design rests on the model never
emitting one. Serialising ids into the prompt is what makes emitting one easy,
and a plausible-looking id is exactly the hallucination a membership check
waves through. So products appear here by *position*: enough to say "the second
one", never enough to name it.

What a position carries is the card the customer is looking at - its name,
price, capacity, colour and size (CLAUDE.md 2). That is merchandise, not
identity: it is already on their screen, it cannot be used to address a
repository, and without it the agent could not tell a four-seater from a
five-seater it had shown a moment earlier. The rule was never "the model knows
nothing about the products"; it was "the model cannot name one to the backend".

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
from app.schemas.geometry import RoomMeasurementRole
from app.schemas.query import ConstraintStrength
from app.schemas.screen import PresentedCardView
from app.schemas.seating_solution import SeatingShape
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

    cards: tuple[PresentedCardView, ...] = ()
    """What is actually on those cards, freshly read from the catalog.

    The count alone was not enough. A salesperson beside five sofas knows which
    five; knowing only that there are five is what produced "here's what I
    found" and an agent that could not tell a 4-seater from a 5-seater it had
    just shown (CLAUDE.md 2, 9).

    Shorter than `count` when the catalog no longer returns a product that was
    on screen. Positions are never closed up to hide the gap, so a card's
    `presented_ordinal` always means the same thing as the ordinal the customer
    would say (CLAUDE.md 7).

    Empty is ordinary: nothing has been shown yet, or this turn could not read
    the catalog. A missing card is a card the agent must not talk about, which
    is the safe direction to fail in.
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

        positions = [card.presented_ordinal for card in self.cards]
        for ordinal in positions:
            if not 1 <= ordinal <= self.count:
                raise ValueError("a card must occupy a presented position")
        if len(set(positions)) != len(positions):
            raise ValueError("two cards cannot occupy the same position")
        if positions != sorted(positions):
            raise ValueError("cards must be in the order they are presented")
        return self


class RoomMeasurementView(BaseModel):
    """One room measurement the customer gave, as they gave it.

    Their own number, so the decision model may see it for the same reason it
    may see their budget: it is what they said, and asking again for something
    already stated is the failure this prevents.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: RoomMeasurementRole
    centimetres: Decimal
    label: str | None = None


class DesignNeedReferenceView(BaseModel):
    """One furnishing role the current plan holds, by what kind of thing it is.

    The model sees these for exactly one purpose: resolving a customer's
    explicit reference to composition that already exists - "the dining area",
    "the lamp" - into approved taxonomy it could not otherwise name. Without
    it, a removal constraint would be a guess about a plan the model cannot
    see.

    **Two fields, and deliberately not a third.** Priority, quantity, capacity
    and design wording are what the room *should be*, and supplying them would
    invite the model to reason about composition - which is the design
    specialist's job (CLAUDE.md 17.2). Identity is absent for the usual reason:
    a model that can see a `need_id` can emit one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)


class RoomProjectView(BaseModel):
    """A whole-room task's customer-supplied requirements.

    Bundle membership appears as counts. Which products are in the room is a
    fact the application resolves, not a list for the model to edit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_type: str | None = None
    room_measurements: tuple[RoomMeasurementView, ...] = ()
    budget: PriceView | None = None
    design_preferences: tuple[PreferenceView, ...] = ()
    bundle_line_count: int = Field(default=0, ge=0)
    """Lines, not units. Four of one product is one line, and the name says so:
    calling it an item count would invite the model to read it as four."""

    locked_line_count: int = Field(default=0, ge=0)
    bundle_card_count: int = Field(default=0, ge=0)
    """How many pieces the customer can actually see.

    Lines merge into cards, so four lines may show as three - and "the second
    one" counts cards. Without this the model cannot tell whether an ordinal it
    is about to accept refers to anything. It names no product and no type.
    """

    room_kind: str | None = None
    chosen_pieces: tuple[str, ...] | None = None
    """The piece keys they chose for the room. None until they answer."""

    last_room_question: str | None = None
    """The room question asked last turn, while the room is not yet built -
    what their reply is most likely answering."""

    regular_seating_count: int | None = None
    """How many people regularly use the room, when they have said.

    Shown so the agent does not ask twice. History is trimmed; this is not, so
    a customer who said "family of five" three turns ago is not asked again
    when the room is finally planned (CLAUDE.md 10.1).

    A requirement, not a quantity: it never tells the agent how many sofas to
    put in the room.
    """

    design_needs: tuple[DesignNeedReferenceView, ...] = ()
    """The roles the current plan calls for, in durable plan order.

    Empty when nothing has been planned, which reads correctly either way:
    there is no composition to refer back to.
    """

    already_owned_line_count: int = Field(default=0, ge=0)
    """Lines the customer already has.

    Worth knowing and safe to know: without it the agent could offer to sell
    someone a piece they told us they own. It is a count, so it names no
    product.
    """

    @model_validator(mode="after")
    def _counts_fit_inside_the_bundle(self) -> Self:
        for name, count in (
            ("locked_line_count", self.locked_line_count),
            ("already_owned_line_count", self.already_owned_line_count),
            ("bundle_card_count", self.bundle_card_count),
        ):
            if count > self.bundle_line_count:
                raise ValueError(f"{name} cannot exceed bundle_line_count")
        return self


class SeatingOfferView(BaseModel):
    """A seat count no single piece meets, and what the customer was offered.

    `pending_question` means the shape question is on screen now: their reply
    is most likely the answer, read into `seating_answer`. Shapes only - no
    product and no price reaches the decision model this way.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_seats: int
    offered_shapes: tuple[SeatingShape, ...] = ()
    pending_question: bool = False
    chosen_shape: SeatingShape | None = None
    combinations_on_screen: int = 0


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
    seating_offer: SeatingOfferView | None = None
