"""What the customer told us about their room.

Measurements only, and only ones they stated. This is not a floor plan: there
are no coordinates, no polygons, no door or window positions and no furniture
placement. A room here is a handful of numbers a person can say out loud -
"it's five by four metres", "the wall by the window is three-twenty" - and
nothing that would require drawing anything.

Two properties carry the design.

**Every value is in centimetres.** Conversion happens once, at capture, through
the same unit vocabulary the catalog uses, so a room measurement and a product
measurement are directly comparable without anyone converting again. An
unrecognised unit is not converted and not guessed; it is refused upstream.

**Every value is the customer's own.** Nothing here is ever inferred. A room
whose length nobody stated has no length, and design reasoning about it must
say so rather than assume one (CLAUDE.md 3.3).
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MeasurementAuthority(StrEnum):
    """Where a number came from, and therefore what may be claimed with it.

    The distinction the response layer depends on. A room that is 320 cm wide
    and a sofa that is 280 cm wide are facts about *this* customer and *this*
    catalog; "leave 75 to 90 cm for a walkway" is a convention that is true of
    rooms in general and of none in particular. Stating the third as though it
    were the first would be inventing a fact about their home.

    Never defaulted anywhere it appears: each carrier fixes its own value as a
    `Literal`, so a measurement cannot be built with the wrong provenance.
    """

    USER_PROVIDED = "user_provided"
    """The customer stated it. Authoritative about their room, and nothing else."""

    CATALOG_VERIFIED = "catalog_verified"
    """Read from the catalog this turn. Authoritative about the product."""

    GENERAL_GUIDANCE = "general_guidance"
    """A design convention. True in general, asserted about no actual room."""


class RoomMeasurementRole(StrEnum):
    """What a stated number measures.

    Deliberately short. Each member is something a customer can say without
    drawing anything, and each is directly usable for a fit question.
    """

    ROOM_LENGTH = "room_length"
    ROOM_WIDTH = "room_width"
    CEILING_HEIGHT = "ceiling_height"

    USABLE_WALL = "usable_wall"
    """A run of wall with nothing in the way. Repeatable: a room has several,
    and which one a piece is destined for is the customer's to say."""

    DOORWAY_WIDTH = "doorway_width"
    """Whether it gets into the room at all. A width, never a position."""


_REPEATABLE = frozenset({RoomMeasurementRole.USABLE_WALL, RoomMeasurementRole.DOORWAY_WIDTH})
"""Roles a room can legitimately have several of. The rest are singular: a room
has one length, and two different answers would mean one of them is wrong."""


class RoomMeasurement(BaseModel):
    """One number the customer gave, in centimetres."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: RoomMeasurementRole
    centimetres: Decimal = Field(gt=0)
    label: str | None = Field(default=None, max_length=80)
    """Which one they meant, in their words - "the wall by the window".

    Only useful for a repeatable role. It is a note for the conversation, never
    a coordinate and never a filter.
    """

    authority: Literal[MeasurementAuthority.USER_PROVIDED] = (
        MeasurementAuthority.USER_PROVIDED
    )
    """Fixed by the type. A room measurement is always the customer's own."""


class RoomGeometry(BaseModel):
    """The room, as far as the customer has described it.

    Partial by nature and useful anyway: knowing the usable wall is enough to
    answer whether a sofa fits along it, with no need for the room's length.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    measurements: tuple[RoomMeasurement, ...] = ()

    @model_validator(mode="after")
    def _singular_roles_appear_once(self) -> Self:
        counts = Counter(m.role for m in self.measurements)
        repeated = sorted(
            role for role, n in counts.items() if n > 1 and role not in _REPEATABLE
        )
        if repeated:
            raise ValueError(f"a room has one of each: {repeated}")
        return self

    def of(self, role: RoomMeasurementRole) -> tuple[RoomMeasurement, ...]:
        """Every measurement for a role, in the order they were stated."""
        return tuple(m for m in self.measurements if m.role is role)

    def one(self, role: RoomMeasurementRole) -> RoomMeasurement | None:
        """The single measurement for a singular role, or None.

        Returns None for a repeatable role with several values rather than
        picking one: which wall they meant is not this object's to decide.
        """
        found = self.of(role)
        return found[0] if len(found) == 1 else None

    @property
    def is_empty(self) -> bool:
        return not self.measurements
