"""What a tool leaves for the screen: typed, verified, never shown to a model.

The agent reads a tool's facts; the customer's screen is built from these. A
turn holds at most one thing that owns the screen - the last list, product,
comparison, room or seating offer an agent put there.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.schemas.bundle_presentation import GroundedBundlePresentation
from app.schemas.comparison import ProductComparisonResult
from app.schemas.grounding import GroundedProduct, SearchExecutionGrounding
from app.schemas.seating_solution import SeatingSolution
from app.schemas.store_knowledge import StoreKnowledgeGrounding
from app.schemas.support import SupportGrounding


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class SearchShown(_Payload):
    grounding: SearchExecutionGrounding


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
)
"""Payloads that replace what the customer is looking at."""
