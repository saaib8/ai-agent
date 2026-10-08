"""The customer's picks: products ticked on screen and kept in a tray.

Picks are `selected_product_ids` - the same shortlist a typed "I'll take the
second one" adds to - so a tick and a sentence record the same thing
(CLAUDE.md 17.1). Ticking is silent: it changes the session without a chat
turn, and returns the picks as they now stand.

**The server says what is picked.** Every reply carries the picks, and the
client draws its ticks and its tray from them. Otherwise a refresh, or a typed
choice, would leave the screen and the assistant disagreeing about what the
customer chose.

A pick carries no product id. It is named by its position in the picks, and a
card by its position on screen; both are resolved server-side against verified
state (CLAUDE.md 20.2).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.furniture_finder import SESSION_ID_PATTERN


class SelectPickAction(BaseModel):
    """Tick a card: pick the product at this position in a result list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["select"] = "select"
    ordinal: int = Field(ge=1)
    list_revision: int | None = Field(default=None, ge=1)
    """The result list the card is on, as the screen was told it
    (`ChatPresentation.list_revision`). None means the latest list. A few
    earlier lists stay tickable, so a sofa can still be picked after the
    screen has moved on to what goes with another."""


class DeselectPickAction(BaseModel):
    """Untick: remove the pick at this position in the picks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["deselect"] = "deselect"
    pick: int = Field(ge=1)


PickActionRequest = Annotated[
    SelectPickAction | DeselectPickAction,
    Field(discriminator="kind"),
]


class PicksRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, pattern=SESSION_ID_PATTERN)
    store_id: int = Field(ge=1)
    action: PickActionRequest
    expected_session_revision: int | None = Field(default=None, ge=0)
    """Same meaning as on a chat message: the revision the screen was drawn
    from. A tick on a list that has since been replaced would pick a product
    the customer never saw."""


class PickPosition(BaseModel):
    """Where a pick sits among the result lists still on screen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    list_revision: int = Field(ge=1)
    ordinal: int = Field(ge=1)


class PickView(BaseModel):
    """One pick, as the tray draws it. Read fresh from the catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pick: int = Field(ge=1)
    """Its position in the picks - the number the tray's actions use. A pick
    the catalog no longer returns is left out without renumbering the rest, so
    this number always means the same product."""

    name_english: str
    image_url: str
    price_amount: Decimal
    price_unit: str
    kind: str | None = None
    """What it is, in customer words ("nightstand")."""

    presented_ordinal: int | None = Field(default=None, ge=1)
    """Its card number in the results on screen, when it is one of them - how
    the client knows which cards to draw ticked."""

    positions: tuple[PickPosition, ...] = ()
    """Every card showing this product on the result lists still tickable -
    how the client draws a card ticked on an older list as well as the
    latest."""

    focused: bool = False
    """The product the conversation is about, when it is this pick."""


class PicksResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    session_revision: int = Field(ge=1)
    picks: tuple[PickView, ...] = ()
    goes_with: int | None = Field(default=None, ge=1)
    """The pick this tick added, for the client to open as a turn of the
    conversation ("I like the ..."): its card, and what goes with it when
    anything does. Set on every new pick; None on an untick, a tick that
    changed nothing, or a pick that could not be read back."""
