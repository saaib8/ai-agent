"""What every agent is told about the session, each turn (CLAUDE.md 17.2).

Everything the customer has touched or said - the screen, the cart, every
budget on record, the running search, the room, what they liked, their taste,
what is waiting on their answer - and nothing of the catalogue beyond it. It is
a projection of `AgentStateV1` read fresh from the catalog, never written back.

No product id, URL or store appears: products are named by their place on the
screen, in the cart, in the liked list or in an offer, exactly as the customer
sees them (CLAUDE.md 17.1). Money is summed by code, so a total an agent says is
a total code made.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.discovery import DimensionConstraintKind, ProductSort
from app.schemas.geometry import RoomMeasurementRole
from app.schemas.seating_solution import SeatingShape
from app.taxonomy.dimensions import DimensionRole, FloorSide


class _Brief(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BriefProduct(_Brief):
    """One product as its card shows it."""

    card: int | None = Field(default=None, ge=1)
    name: str
    name_ar: str | None = None
    """The store's Arabic name, when the reply is in Arabic - the name the
    customer's card shows (CLAUDE.md 25)."""
    kind: str | None
    price: str
    currency: str
    colour: str | None = None
    styles: tuple[str, ...] | None = None
    seats: int | None = None
    size_cm: dict[str, str] | None = None


class CartLine(BriefProduct):
    pick: int = Field(ge=1)
    """Its place in the cart - what a `picked` reference names."""
    quantity: int = Field(ge=1)
    line_total: str
    room: str | None = None
    """The room it was bought for, when it is one of a room's pieces."""


class MoneyTotal(_Brief):
    currency: str
    amount: str


class CartBrief(_Brief):
    lines: tuple[CartLine, ...] = ()
    items: int = Field(default=0, ge=0)
    totals: tuple[MoneyTotal, ...] = ()
    """One per currency, never converted."""
    not_priced: int | None = Field(default=None, ge=1)
    """Products in the cart the catalog did not return just now - still in
    the cart, but left out of the lines and the totals. Never "an empty cart"."""


class Budget(_Brief):
    """A money bound on record, and what it bounds."""

    applies_to: Literal["search", "room", "seating"]
    currency: str
    at_least: str | None = None
    at_most: str | None = None


class SizeBrief(_Brief):
    measures: DimensionRole
    side: FloorSide | None = None
    kind: DimensionConstraintKind
    min_cm: str | None = None
    max_cm: str | None = None
    target_cm: str | None = None


class SizePair(_Brief):
    first_cm: str
    second_cm: str


class SearchBrief(_Brief):
    """The search the cards on screen came from, exactly as it ran."""

    kind: str
    also_showing: tuple[str, ...] | None = None
    """Types code searched beside the one asked for (sofa sets beside sofas)."""
    only_this_type: bool | None = None
    colours_required: tuple[str, ...] | None = None
    styles_required: tuple[str, ...] | None = None
    colours_preferred: tuple[str, ...] | None = None
    styles_preferred: tuple[str, ...] | None = None
    wording: str | None = None
    """Their descriptive words, which rank by meaning and filter nothing."""
    seats_at_least: int | None = None
    seats_at_most: int | None = None
    head_count: int | None = None
    """How many usually sit there: orders, never filters."""
    sizes: tuple[SizeBrief, ...] | None = None
    size_pair: SizePair | None = None
    sort: ProductSort | None = None


class RoomSizeBrief(_Brief):
    measures: RoomMeasurementRole
    cm: str
    label: str | None = None


class WallBrief(_Brief):
    """A wall as they described it: its length and what is on it."""

    wall: str
    cm: str | None = None
    has: tuple[str, ...] | None = None
    note: str | None = None


class RoomBrief(_Brief):
    """The room being designed - what they told us, and the plan once built."""

    room: str
    kind: str | None = None
    size: tuple[RoomSizeBrief, ...] | None = None
    size_known: bool | None = None
    """Its length and width are on record."""
    size_declined: bool | None = None
    """They would rather not give its size: do not ask again."""
    palette: tuple[str, ...] | None = None
    """The room's colours as designed."""
    purpose: str | None = None
    """What the room is mostly for, in their words."""
    layout_idea: str | None = None
    """How Nora arranged it, in her words."""
    walls: tuple[WallBrief, ...] | None = None
    head_count: int | None = None
    pieces_chosen: tuple[str, ...] | None = None
    colours: tuple[str, ...] | None = None
    styles: tuple[str, ...] | None = None
    built_around_picks: tuple[int, ...] | None = None
    """Cart places of the picks the room was built around."""
    in_cart: bool | None = None
    plan: dict[str, Any] | None = None
    """The built room as the room engine reports it: pieces by card, totals."""
    placements: list[dict[str, Any]] | None = None
    """Where Nora would put each piece on their walls, and why - words only."""
    next_sizes: list[dict[str, Any]] | None = None
    """The designer's sizes for the pieces the room wants next: search at one
    with `designer_size`."""


class FinishedRoomBrief(_Brief):
    room: str
    cart_picks: tuple[int, ...] = ()


class TasteBrief(_Brief):
    """What they said they like and would rather avoid. It orders, never filters."""

    colours: tuple[str, ...] | None = None
    styles: tuple[str, ...] | None = None
    avoid_colours: tuple[str, ...] | None = None
    avoid_styles: tuple[str, ...] | None = None
    shopping_for_room: str | None = None


class SavedSizes(_Brief):
    """Sizes they gave for a type, brought back when they search it again."""

    kind: str
    sizes: tuple[SizeBrief, ...] = ()
    size_pair: SizePair | None = None


class SeatingPiece(_Brief):
    name: str
    kind: str | None
    seats: int | None = None
    quantity: int = Field(ge=1)
    price: str
    currency: str


class SeatingOption(_Brief):
    option: int = Field(ge=1)
    shape: SeatingShape
    pieces: tuple[SeatingPiece, ...]
    total: str | None = None
    turned_down: bool | None = None


class SeatingBrief(_Brief):
    seats: int
    budget: str | None = None
    currency: str | None = None
    options: tuple[SeatingOption, ...] = ()
    chosen: SeatingOption | None = None


class OverBudgetOffer(_Brief):
    """A dearer piece held until they say yes - nothing in the room has changed."""

    room_piece: int | None = None
    adding_piece: str | None = None
    product: BriefProduct | None = None
    room_total_if_accepted: str | None = None
    currency: str | None = None
    stage: str


class GoalMember(_Brief):
    pick: int = Field(ge=1)
    """Its place in the cart."""
    name: str
    kind: str | None
    seats: int | None = None
    quantity: int = Field(ge=1)


class GoalBrief(_Brief):
    """A goal and how far it has come - counted by code, judged by the agent."""

    goal: int = Field(ge=1)
    kind: str
    status: str
    summary: str
    room: str | None = None
    for_kind: str | None = None
    seats: int | None = None
    seats_covered: int | None = None
    """Seats its members in the cart give, from their recorded seat counts."""
    seats_left: int | None = None
    members: tuple[GoalMember, ...] = ()


class PendingBrief(_Brief):
    seating: SeatingBrief | None = None
    over_budget_offer: OverBudgetOffer | None = None
    options_on_screen_for_room_piece: int | None = None


class SessionBrief(_Brief):
    language: str
    store_currency: str | None = None
    catalog_unavailable: bool | None = None
    cards_on_screen: tuple[BriefProduct, ...] = ()
    comparison_columns: tuple[BriefProduct, ...] | None = None
    in_focus: BriefProduct | None = None
    cart: CartBrief = CartBrief()
    budgets: tuple[Budget, ...] | None = None
    search: SearchBrief | None = None
    goals: tuple[GoalBrief, ...] | None = None
    """What they are working toward, and how far each has come."""
    room: RoomBrief | None = None
    finished_rooms: tuple[FinishedRoomBrief, ...] | None = None
    liked: tuple[BriefProduct, ...] | None = None
    """Numbered by their place in the liked list - what a `liked` reference names."""
    taste: TasteBrief | None = None
    saved_sizes: tuple[SavedSizes, ...] | None = None
    pending: PendingBrief | None = None

    def context(self) -> dict[str, Any]:
        """The brief as the model reads it: JSON, with nothing empty."""
        return self.model_dump(mode="json", exclude_none=True)
