"""Room visualisation contracts: which view, and the render that comes back.

A package render names a camera view and nothing else about the room: what is
rendered is the room package the session holds - its pieces, the room's size
and style - read by the application, so a client cannot put products in a
render that the conversation never chose.

A catalogue render is the other kind: the customer picked the pieces
themselves and described the room (see :mod:`app.schemas.catalog`). Its
products are still read from the store-scoped catalog, and its room is
described in closed vocabularies, never free text.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.furniture_finder import SESSION_ID_PATTERN


class RenderView(StrEnum):
    """The camera views a customer may pick. The prompt defines each."""

    CORNER = "corner"
    EYE_LEVEL = "eye_level"
    ISOMETRIC = "isometric"
    TOP_DOWN = "top_down"


class RenderSource(StrEnum):
    """What a render pictured: the conversation's room package, or pieces the
    customer picked from the catalogue. Only a package render can go out of
    date, because only the package changes under it."""

    PACKAGE = "package"
    CATALOG = "catalog"


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
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    view: RenderView
    view_label: str
    items: tuple[RoomRenderItem, ...]
    source: RenderSource = RenderSource.PACKAGE
    room: RenderRoomSpec | None = None
