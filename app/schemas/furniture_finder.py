"""Furniture Finder contracts: a photo, the objects in it, and a pick.

Two exchanges. The first uploads a photo and returns what the detector found
in it. The second picks one of those objects and is a **chat turn**: it
answers with the same `ChatResponse` a message does, and it commits the
products it found as the list on screen, so "compare the first two" on the
next message resolves against them.

What the client may send on the pick is deliberately narrow: which photo and
which object, by the identifiers the upload returned. Never a crop, a box or a
polygon - the outline that is searched is the one the application stored when
the detector drew it, so a client cannot steer the search to pixels the
detector never proposed.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SESSION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
"""The rule the session store enforces when it builds a key (M13 27)."""

IMAGE_ID_PATTERN = r"^[0-9a-f]{32}$"
"""A photo id is application-issued (a uuid4 hex), never chosen by a client."""

FINDER_PHOTO_VERSION = "finder_photo_v1"

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
    session's lifetime, and never anywhere else. The image kept is the
    downscaled one the detector saw, so the outlines and the pixels share one
    coordinate space by construction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = FINDER_PHOTO_VERSION
    image_id: str = Field(pattern=IMAGE_ID_PATTERN)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    jpeg_b64: str = Field(min_length=1)
    objects: tuple[DetectedObject, ...] = ()

    def object(self, object_id: int) -> DetectedObject | None:
        return next((o for o in self.objects if o.object_id == object_id), None)


class ObjectDescription(BaseModel):
    """What the vision model saw in a picked object, as words to search with.

    A query, never a fact: nothing here is shown to the customer as a product
    attribute, and no filter is built from it. The index was built from text
    documents about each product, so this is turned into the same shape of
    document and embedded the same way.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(min_length=1, max_length=300)
    styles: tuple[str, ...] = Field(default=(), max_length=3)
    color: str = Field(min_length=1, max_length=60)
    materials: tuple[str, ...] = Field(default=(), max_length=3)

    def query_text(self, category_words: str) -> str:
        """A document shaped like the index's own product documents.

        Those read "<name>. Category: <category>. Style: <a, b>. Color: <c>.
        ...", so the query leads with the summary where a name would be and
        follows with the same labelled fields. The documents have no material
        field; it follows the summary as a short clause of its own, where a
        product name would usually mention it.
        """
        parts = [self.summary.rstrip(". ")]
        if self.materials:
            parts.append("Material: " + ", ".join(self.materials))
        parts.append(f"Category: {category_words}")
        if self.styles:
            parts.append("Style: " + ", ".join(self.styles))
        parts.append(f"Color: {self.color}")
        return ". ".join(parts) + "."


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
