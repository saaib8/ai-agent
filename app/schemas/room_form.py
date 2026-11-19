"""The room form: the three questions every room shares, asked together.

When Nora starts a room she shows one small form - the budget (required),
the room's size and its look - instead of one question a reply. Each question
has choices and a box to type; the follow-ups a room needs are her ordinary
chat questions afterwards (docs/room-form-plan.md).

What the client draws (`RoomForm`), what the session remembers so a tapped
choice can be read back (`RoomFormRecord`), and the answers the client sends
(`RoomFormAction`). A client names a choice by its key and never invents one:
a key the form did not offer is ignored.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RoomFormQuestionKind = Literal["budget", "size", "colours", "style"]

MAX_ROOM_SIDE_M = Decimal(30)
"""The largest room side the size boxes take, in metres."""
MIN_ROOM_SIDE_M = Decimal(1)

MAX_FORM_TEXT = 200
"""A typed answer's length: a figure, a size or a few words of a look."""

MAX_COLOURS_CHOSEN = 3
"""Their colours lead the palette; Nora completes it (up to five)."""
MAX_STYLES_CHOSEN = 2

YOU_DECIDE = "you_decide"
"""The style chip that leaves the look to Nora."""


class RoomFormChoice(BaseModel):
    """One chip: its key, sent back when tapped, and its words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=80)


class RoomFormDimensions(BaseModel):
    """Two number boxes, length and width, in metres."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    length_label: str = Field(min_length=1, max_length=40)
    width_label: str = Field(min_length=1, max_length=40)
    unit: str = Field(min_length=1, max_length=10)
    """The unit's word beside the boxes ("m")."""
    step: Decimal = Decimal("0.1")
    minimum: Decimal = MIN_ROOM_SIDE_M
    maximum: Decimal = MAX_ROOM_SIDE_M


class RoomFormQuestion(BaseModel):
    """One question on the form, with its chips and the box beside them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RoomFormQuestionKind
    label: str = Field(min_length=1, max_length=120)
    choices: tuple[RoomFormChoice, ...] = Field(min_length=1)
    max_choices: int = Field(default=1, ge=1)
    """One for the budget and the size; several colours and styles."""
    placeholder: str | None = Field(default=None, min_length=1, max_length=80)
    """The box beside the chips, for an answer in their own words. The colours
    have none: the style's box describes the whole look."""
    dimensions: RoomFormDimensions | None = None
    """The size's two number boxes - length and width in metres, each with up
    and down - in place of a text box."""
    required: bool = False
    """Only the budget: the form is not sent without it."""


class RoomForm(BaseModel):
    """The form, as a client draws it, in the reply's language."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1)
    """Which form this is in the session. The answers name it, so an old form
    cannot answer a newer one."""
    questions: tuple[RoomFormQuestion, ...] = Field(min_length=1)
    submit_label: str = Field(min_length=1, max_length=40)
    budget_needed: str = Field(min_length=1, max_length=120)
    """Shown under the budget when Design it is tapped without one."""
    budget_missing: bool = False
    """The last answers came without a budget: the budget is marked."""


class RoomFormRecord(BaseModel):
    """The form the session last showed, so its answers can be read back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1)
    currency: str | None = None
    """The store's one currency, for a typed amount said without one."""
    budgets: dict[str, Decimal] = Field(default_factory=dict)
    """Each budget chip's key and its amount."""
    sizes: dict[str, Decimal] = Field(default_factory=dict)
    """Each size chip's key and its side in centimetres (a square room)."""
    colours: tuple[str, ...] = ()
    """The colour chips' keys: the approved colour values offered."""
    styles: tuple[str, ...] = ()
    """The style chips' keys: the approved style values offered, and
    `you_decide`."""
    looks_answered: bool = False
    """They answered the look - a chip, their own words or "you decide" - so a
    later form leaves it off."""


class RoomFormAction(BaseModel):
    """The answers to the room form, sent with one tap on Design it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    form: int = Field(ge=1)
    budget: str | None = Field(default=None, min_length=1, max_length=64)
    budget_text: str | None = Field(default=None, min_length=1, max_length=MAX_FORM_TEXT)
    size: str | None = Field(default=None, min_length=1, max_length=64)
    length_m: Decimal | None = Field(default=None, ge=MIN_ROOM_SIDE_M, le=MAX_ROOM_SIDE_M)
    width_m: Decimal | None = Field(default=None, ge=MIN_ROOM_SIDE_M, le=MAX_ROOM_SIDE_M)
    """The size boxes, in metres. Both, or the size chip."""
    colours: tuple[str, ...] = Field(default=(), max_length=MAX_COLOURS_CHOSEN)
    styles: tuple[str, ...] = Field(default=(), max_length=MAX_STYLES_CHOSEN)
    look_text: str | None = Field(default=None, min_length=1, max_length=MAX_FORM_TEXT)
