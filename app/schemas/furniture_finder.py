"""Furniture Finder contracts: a photo, the objects in it, and a pick.

Two exchanges. The first uploads a photo and returns what the main platform's
finder found in it. The second picks one of those objects and is a **chat
turn**: it answers with the same `ChatResponse` a message does, and it commits
the products it found as the list on screen, so "compare the first two" on the
next message resolves against them.

What the client may send on the pick is deliberately narrow: which photo and
which object, by the identifiers the upload returned. Never a crop, a box or a
polygon - the outline that is searched is the one the application stored when
the detector drew it, so a client cannot steer the search to pixels the
detector never proposed.
"""

from __future__ import annotations

import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

SESSION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
"""The rule the session store enforces when it builds a key (M13 27)."""

IMAGE_ID_PATTERN = r"^[0-9a-f]{32}$"
"""A photo id is application-issued (a uuid4 hex), never chosen by a client."""

FINDER_PHOTO_VERSION = "finder_photo_v2"

STORED_IMAGE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9/_.\-]{0,511}$"
"""A key in the main platform's image store, as its finder returned it. A
relative path only - never a scheme, a host or a step out of the folder - since
it is appended to the configured base to make the link the platform reads."""


def is_plain_key(value: object) -> bool:
    """A key the main platform stores an image under: no scheme, no host, no `..`."""
    return (
        isinstance(value, str)
        and re.fullmatch(STORED_IMAGE_PATTERN, value) is not None
        and ".." not in value
    )


WIDGET_SESSION_PATTERN = r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$"
WIDGET_KEY_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
WIDGET_STORE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$"

Point = tuple[int, int]


class ImageBox(BaseModel):
    """An axis-aligned box in the stored photo's pixel space."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x1: int = Field(ge=0)
    y1: int = Field(ge=0)
    x2: int = Field(gt=0)
    y2: int = Field(gt=0)

    @model_validator(mode="after")
    def _has_area(self) -> Self:
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError("a box must have positive width and height")
        return self


class DetectedObject(BaseModel):
    """One thing the detector outlined, as this application stores it.

    `label` is the detector's class, which is also the catalog category the
    product index is filtered on. It is a *visual* category, not a commerce
    taxonomy value, and it is never written into a structured search.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    object_id: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=100)
    confidence: float = Field(ge=0, le=1)
    box: ImageBox
    polygon: tuple[Point, ...] = Field(min_length=3)

    @property
    def display_label(self) -> str:
        """How a person would say the label: `side-table` -> `side table`."""
        return self.label.replace("-", " ").replace("_", " ")


class FinderPhoto(BaseModel):
    """A photo held for picking, between the upload and the pick.

    Stored under the store and the session it was uploaded in, with the
    session's lifetime, and never anywhere else. The photo itself stays with
    the main platform: what is kept here is which scan it was and where the
    platform keeps the copy its detector saw, so a pick is cut from the same
    pixels the outlines were drawn on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = FINDER_PHOTO_VERSION
    image_id: str = Field(pattern=IMAGE_ID_PATTERN)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    scan_id: str = Field(min_length=1, max_length=64)
    """The platform's id for the detection, for its logs and ours."""
    stored_image: str = Field(pattern=STORED_IMAGE_PATTERN)
    objects: tuple[DetectedObject, ...] = ()

    @model_validator(mode="after")
    def _stored_image_stays_in_its_folder(self) -> Self:
        if not is_plain_key(self.stored_image):
            raise ValueError("stored_image must be a plain key, never a step out of its folder")
        return self

    def object(self, object_id: int) -> DetectedObject | None:
        return next((o for o in self.objects if o.object_id == object_id), None)


# ── wire contracts ──────────────────────────────────────────────────────────


class FinderObjectView(BaseModel):
    """One pickable object, as a client draws it over the photo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    object_id: int
    label: str
    display_label: str
    confidence: float
    box: ImageBox
    polygon: tuple[Point, ...]


class FinderPhotoResponse(BaseModel):
    """The upload's answer: which photo, its size, and what can be picked.

    Only objects this retailer can actually match are listed. An outline the
    customer can click that can never return a product would be a promise the
    catalog cannot keep.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_id: str
    width: int
    height: int
    objects: tuple[FinderObjectView, ...]
    unmatched_count: int = Field(ge=0)
    """Objects the detector found that this catalog has nothing to match
    against. A count, not their labels: enough for a client to say "some
    items can't be matched here" without describing another store's range."""


class ShopperCredentials(BaseModel):
    """Who the finder runs as on the main platform: the shopper's widget session.

    Forwarded by the gateway from the widget - a signed-in member's token or an
    anonymous visitor's. Opaque here: the platform verifies it and decides the
    store from it; this service only passes it on, and never logs it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: SecretStr
    widget_key: str | None = Field(default=None, pattern=WIDGET_KEY_PATTERN)
    store_public_id: str | None = Field(default=None, pattern=WIDGET_STORE_PATTERN)


class FinderPickRequest(BaseModel):
    """Pick one object from an uploaded photo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, pattern=SESSION_ID_PATTERN)
    store_id: int = Field(ge=1)
    image_id: str = Field(pattern=IMAGE_ID_PATTERN)
    object_id: int = Field(ge=1)
    expected_session_revision: int | None = Field(default=None, ge=0)
    """Same meaning as on a chat message: the revision the client's screen was
    drawn from, checked before any provider is paid."""


def view_of(detected: DetectedObject) -> FinderObjectView:
    return FinderObjectView(
        object_id=detected.object_id,
        label=detected.label,
        display_label=detected.display_label,
        confidence=detected.confidence,
        box=detected.box,
        polygon=detected.polygon,
    )
