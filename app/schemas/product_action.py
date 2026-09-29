"""Screen-driven, deterministic actions on the customer's picks.

**Transport, and application-only.** Like `search_action` and `bundle_action`,
these are things the customer did on the screen - tapped "Ask about this" on a
pick, "Compare" on two, or a "Matching rugs" chip - not language a model
interpreted, so no decision model is consulted and none can mis-route them
(CLAUDE.md 3.6).

They carry no product id. A pick is named by its position in the customer's
picks, and the server resolves it against verified state (CLAUDE.md 20.2). A
companion is named by product type, and only a type the reviewed pairings
offer beside the product in focus is accepted - a chip is never a way to run an
arbitrary search.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GoesWithPickAction(BaseModel):
    """What goes with one pick: put it in focus, and show its companions.

    Sent when a customer picks the first product of its kind, and from a
    pick's "Goes with" button in the tray.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["goes_with"] = "goes_with"
    pick: int = Field(ge=1)


class ComparePicksAction(BaseModel):
    """Two picks side by side, in the order given.

    Exactly two: a comparison of two is one the customer can read at a glance,
    and the product decision was to keep it there. The service's own limit is
    configuration and is checked again where the comparison is built.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["compare"] = "compare"
    picks: tuple[int, int]

    @model_validator(mode="after")
    def _two_different_picks(self) -> Self:
        if any(pick < 1 for pick in self.picks):
            raise ValueError("a pick is 1 or greater")
        if self.picks[0] == self.picks[1]:
            raise ValueError("a comparison needs two different picks")
        return self


class CompanionAction(BaseModel):
    """Products of one type that go with the product in focus.

    The type is one the reply offered as a chip. It is checked against the
    reviewed pairings for the focused product before anything is searched.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["companion"] = "companion"
    category: str = Field(min_length=1, max_length=64)
    subcategory: str = Field(min_length=1, max_length=64)


class CompanionOffer(BaseModel):
    """A companion type the reply offers as a chip, from the reviewed pairings.

    Application-only until the runtime turns it into a chip. `label` is the
    type in customer words, plural - "rugs".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str = Field(min_length=1)
    subcategory: str = Field(min_length=1)
    label: str = Field(min_length=1)


ProductActionRequest = Annotated[
    GoesWithPickAction | ComparePicksAction | CompanionAction,
    Field(discriminator="kind"),
]
"""Any action on the picks, told apart by `kind`."""
