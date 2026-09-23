"""Screen-driven, deterministic edits to a room package.

**Transport, and application-only.** A `bundle_action` is what the customer did
on the screen — tapped a piece, then tapped an option — not something a model
interpreted. It carries no product id: everything is named by ordinal, the piece
by its position among the room cards and the option by its position among the
alternatives on screen, and the server resolves both against verified state the
same way a typed reference does (CLAUDE.md 6, 20.2).

Because the action already says exactly what to do, the turn that carries one
consults no decision model — which also means it never depends on a language
model routing "show me other beds" to a search rather than to a room refinement.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class BundleAlternativesAction(BaseModel):
    """Show the alternatives for one room piece, so the customer can pick.

    Deterministic: it runs that role's own search and presents the results, so
    the options are always for the right role and the ordinals the swap will
    reference are the ones the customer is looking at.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["list_alternatives"] = "list_alternatives"
    bundle_ordinal: int = Field(ge=1)


class BundleSwapAction(BaseModel):
    """Replace one room piece with a specific alternative the customer chose.

    `bundle_ordinal` is the piece's position among the room cards; the chosen
    option is `alternative_ordinal`, its position among the products on screen
    (the alternatives just shown for that role). The room is then chosen again
    around the fixed choice, keeping everything else.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["swap"] = "swap"
    bundle_ordinal: int = Field(ge=1)
    alternative_ordinal: int = Field(ge=1)


BundleActionRequest = Annotated[
    BundleAlternativesAction | BundleSwapAction,
    Field(discriminator="kind"),
]
"""Either action, told apart by `kind`."""
