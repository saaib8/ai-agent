"""Room visualisation contracts: which view, and the render that comes back.

The request names a camera view and nothing else about the room. What is
rendered is the room package the session holds - its pieces, the room's size
and style - read by the application, so a client cannot put products in a
render that the conversation never chose.
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
    customer this picture no longer matches their room.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_url: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    view: RenderView
    view_label: str
    items: tuple[RoomRenderItem, ...]
