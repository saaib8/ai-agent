"""What a tool leaves for the screen: typed, verified, never shown to a model.

The agent reads a tool's facts; the customer's screen is built from these. A
turn holds at most one thing that owns the screen - the last list, product,
comparison, room or seating offer an agent put there.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.bundle_presentation import GroundedBundlePresentation
from app.schemas.comparison import ProductComparisonResult
from app.schemas.grounding import GroundedProduct, SearchExecutionGrounding
from app.schemas.seating_solution import SeatingSolution
from app.schemas.store_knowledge import StoreKnowledgeGrounding
from app.schemas.support import SupportGrounding
from app.schemas.visualization import RoomRenderPresentation


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class GridChip(_Payload):
    """A chip code draws beside a grid of kinds (CLAUDE.md 10.3): another of the
    piece's own kind, or the rest of its room - worded from the labels."""

    offers: Literal["another_of_its_kind", "whole_room"]
    name: str
    """The piece's kind in customer words."""
    name_ar: str | None = None
    room: str | None = None
    """The piece's room, when a fact says it (CLAUDE.md 36)."""


class SearchShown(_Payload):
    grounding: SearchExecutionGrounding
    more_options: bool = True
    """False for a list with nothing more to page through - Salman's grid of one
    product per kind, whose kind chips are how to see more of any one."""
    kind_cards: bool = False
    """True for that grid: each card stands for its kind, drawn small - an image
    and the kind's name - and a tap opens that kind's page."""
    kind_names: dict[str, tuple[str, str | None]] = {}
    grid_chips: tuple[GridChip, ...] = ()
    """With kind_cards: the only chips beside the grid, built by code."""
    """Each kind on the grid in customer words, English and Arabic ("rugs",
    not the catalog's "carpet"), by subcategory."""


class ProductShown(_Payload):
    product: GroundedProduct


class SelectionShown(_Payload):
    products: tuple[GroundedProduct, ...]
    liked: bool = False


class ComparisonShown(_Payload):
    comparison: ProductComparisonResult


class RoomShown(_Payload):
    room: GroundedBundlePresentation


class OptionsShown(_Payload):
    """A room piece's alternatives, as a list the customer can pick from."""

    products: tuple[GroundedProduct, ...]


class RenderShown(_Payload):
    """One product staged in a room of its own - the picture this turn made."""

    render: RoomRenderPresentation


class SeatingShown(_Payload):
    solution: SeatingSolution


class SupportShown(_Payload):
    support: SupportGrounding


class KnowledgeShown(_Payload):
    knowledge: StoreKnowledgeGrounding


ScreenPayload = (
    SearchShown
    | ProductShown
    | SelectionShown
    | ComparisonShown
    | RoomShown
    | OptionsShown
    | SeatingShown
    | RenderShown
)
"""Payloads that replace what the customer is looking at."""
