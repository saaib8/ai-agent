"""What every tool runs against, and no model ever sees."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.schemas.card_comparison import CardRef
from app.schemas.furniture_finder import ShopperCredentials
from app.schemas.language import ReplyLanguage
from app.schemas.retailer import RetailerContext
from app.schemas.visualization import VISUALIZATION_ALLOWED

if TYPE_CHECKING:
    from app.agents.services import AgentServices


@dataclass(frozen=True, slots=True)
class ToolContext:
    """The request's own scope and services, handed to each tool by the loop.

    The store comes from here and only from here: no tool's arguments can
    name one, so nothing a model writes can widen a search past the store the
    request resolved (CLAUDE.md 8, 20.2).
    """

    retailer: RetailerContext
    services: AgentServices
    language: ReplyLanguage | None = None
    tap_cards: tuple[CardRef, ...] = ()
    """The cards a tapped button named, each with the list it was on - so
    "card 3" of a tap means card 3 of that list, not of the newest one."""
    tapped: str | None = None
    """The button this turn's tap was, when it was one."""
    customer_said: str | None = None
    """The message the customer typed this turn - never a tap's stand-in words.
    A search they ask for ranks on it as well as on the agent's wording."""
    session_id: str | None = None
    """The chat this turn belongs to - only to find what the chat keeps beside
    its session (its room photo); never shown to a model."""
    with_their_photo: bool = False
    """The customer uploaded a photo of their room for this picture: the
    upload button's tap says so. Their photo is used only then - never on a
    typed "my room", which means the room being designed."""
    photo_sent_now: bool = False
    """A photo came with this message (`AgentStateV1.chat_photo` is its
    record) - the photo the customer means by "this"."""
    shopper: ShopperCredentials | None = None
    """The shopper's widget session the gateway forwarded - what the finder
    runs as on the platform; never shown to a model."""
    finder_access: str = VISUALIZATION_ALLOWED
    """Whether this shopper may use the finder this turn: `allowed`, or the
    gateway's refusal (the same five values as `visualization`)."""
    visualization: str = VISUALIZATION_ALLOWED
    """Whether this shopper may have a room drawn this turn: `allowed`, or the
    backend's refusal code (`visualization_access`). Every picture tool checks
    it before an image is paid for; the chat runtime always sets it from the
    request."""
