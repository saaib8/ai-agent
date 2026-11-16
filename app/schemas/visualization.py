"""Room visualisation contracts: which view, and the render that comes back.

A package render names a camera view and nothing else about the room: what is
rendered is the room package the session holds - its pieces, the room's size
and style - read by the application, so a client cannot put products in a
render that the conversation never chose.

A catalogue render is the other kind: the customer picked the pieces
themselves and described the room (see :mod:`app.schemas.catalog`). Its
products are still read from the store-scoped catalog, and its room is
described in closed vocabularies, never free text.

Either can instead be drawn in the customer's own room: a `room_photo_id`
names the emptied photo the session keeps (see :mod:`app.schemas.room_photo`).
The photo is the room and the camera, so no view is chosen for it.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PrivateAttr

from app.schemas.furniture_finder import SESSION_ID_PATTERN
from app.schemas.room_photo import ROOM_PHOTO_ID_PATTERN


class RenderView(StrEnum):
    """The camera views a customer may pick. The prompt defines each."""

    CORNER = "corner"
    EYE_LEVEL = "eye_level"
    ISOMETRIC = "isometric"
    TOP_DOWN = "top_down"


class RenderSource(StrEnum):
    """What a render pictured: the conversation's room package, pieces the
    customer picked from the catalogue, or one product staged in a room of its
    own. Only a package render can go out of date, because only the package
    changes under it; a catalogue or product render pictures a fixed choice."""

    PACKAGE = "package"
    CATALOG = "catalog"
    PRODUCT = "product"
    """One product the customer tapped, shown in an inviting room: the product
    is faithful to the catalog, the room and its decor are invented, and
    nothing else in the picture is a product the customer can buy."""


class RoomType(StrEnum):
    """The rooms a catalogue render can be of. Closed, because the choice is
    written into the image prompt; the prompt module words each one."""

    LIVING_ROOM = "living_room"
    BEDROOM = "bedroom"
    DINING_ROOM = "dining_room"
    HOME_OFFICE = "home_office"
    KIDS_ROOM = "kids_room"
    MAJLIS = "majlis"
    ENTRYWAY = "entryway"


class RenderRoomSpec(BaseModel):
    """The room a catalogue render is of, as the customer set it up.

    `style` is an approved catalog style value; the service checks it against
    the registry, which schemas do not load. Side lengths are bounded by
    configuration in the service; the bounds here only refuse absurd input.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_type: RoomType
    style: str = Field(min_length=1, max_length=40)
    length_m: float = Field(gt=0, le=100)
    width_m: float = Field(gt=0, le=100)


class VisualizeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, pattern=SESSION_ID_PATTERN)
    store_id: int = Field(ge=1)
    view: RenderView = RenderView.CORNER
    room_photo_id: str | None = Field(default=None, pattern=ROOM_PHOTO_ID_PATTERN)
    """Draw the package in the customer's own room instead: the emptied photo
    this session keeps. The photo is the camera, so `view` is not used."""
    visualization: VisualizationAccess = None
    """Whether this shopper may have a room drawn - the same answer, set the
    same way by the gateway, as `ChatRequest.visualization`."""
    expected_session_revision: int | None = Field(default=None, ge=0)
    """Same meaning as on a chat message: the revision the client's screen was
    drawn from, checked before any image model is paid."""


class RoomRenderItem(BaseModel):
    """One piece that was sent to be rendered, as the customer knows it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name_english: str
    image_url: str
    product_url: str
    quantity: int = Field(ge=1)


class RoomRenderPresentation(BaseModel):
    """A finished render, and exactly which pieces it was asked to show.

    `items` is what a client compares with a later room package to tell the
    customer this picture no longer matches their room. A catalogue render
    carries the room it was set up with. Like every chat presentation it names
    no catalog ids: the client that sent a selection already holds it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_url: str
    """A `data:image/jpeg;base64,` URL. Renders are not stored anywhere, so the
    picture itself is the only copy, and it lives on the customer's screen."""

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    view: RenderView | None
    """None when drawn in the customer's own room: their photo is the camera."""
    view_label: str
    items: tuple[RoomRenderItem, ...]
    source: RenderSource = RenderSource.PACKAGE
    room: RenderRoomSpec | None = None
    room_photo_id: str | None = Field(default=None, pattern=ROOM_PHOTO_ID_PATTERN)
    """The customer's room it was drawn in, so it can be drawn there again."""

    _drawn: tuple[tuple[int, int], ...] = PrivateAttr(default=())

    @property
    def drawn(self) -> tuple[tuple[int, int], ...]:
        """Each piece drawn as (product id, quantity), in `items` order - for
        the session's memory of the picture (`AgentStateV1.last_render`).
        Private: never serialized, so no catalog id reaches a client."""
        return self._drawn

    def with_drawn(self, pieces: Sequence[tuple[int, int]]) -> RoomRenderPresentation:
        copy = self.model_copy()
        copy._drawn = tuple(pieces)
        return copy


class VisualizationAnswer(StrEnum):
    """Whether this shopper may have a room drawn - the gateway's answer, one
    of five fixed values (docs/visualization-in-chat-plan.md D1). The gateway
    maps its own refusal codes onto these, so its codes can change without this
    service."""

    ALLOWED = "allowed"
    SIGN_UP_REQUIRED = "sign_up_required"
    """An anonymous shopper: signing in or up would open it."""
    LIMIT_REACHED = "limit_reached"
    """Their own allowance is used up for now (today's, or this month's)."""
    NOT_INCLUDED = "not_included"
    """Their role or access does not include it."""
    STORE_UNAVAILABLE = "store_unavailable"
    """The store does not offer it right now - its plan, budget or allowance."""


VISUALIZATION_ALLOWED = VisualizationAnswer.ALLOWED.value
VISUALIZATION_UNCONFIRMED = "not_confirmed"
"""No answer came with the request, and this deployment does not draw without
one (`visualization.allowed_without_gateway`)."""
VISUALIZATION_UNRECOGNISED = "unrecognised"
"""An answer came that is not one of the five: refused, worded like
`store_unavailable`, and logged so the gateway's mistake is seen."""

_ANSWERS = frozenset(answer.value for answer in VisualizationAnswer)


def _read_answer(value: Any) -> str | None:
    """Never a reason to refuse the request: an answer this service does not
    know - a new code, a typo, the wrong type - is read as no picture."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in _ANSWERS:
        return value.strip().lower()
    return VISUALIZATION_UNRECOGNISED


VisualizationAccess = Annotated[str | None, BeforeValidator(_read_answer)]
"""The gateway's answer on drawing, read leniently (`_read_answer`): one of
`VisualizationAnswer`, `unrecognised`, or None when it sent none. Set by the
gateway, never by the widget: the gateway overwrites whatever the widget sent."""


def visualization_access(sent: str | None, *, without_gateway: bool) -> str:
    """What this request may draw: the gateway's answer when it sent one; else
    allowed only where this deployment draws without one (local and stage, for
    the console) - in production, no answer is no picture."""
    if sent is not None:
        return sent
    return VISUALIZATION_ALLOWED if without_gateway else VISUALIZATION_UNCONFIRMED
