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


class SwapConfirmAction(BaseModel):
    """Keep the dearer swap the customer was just offered, over budget and all.

    The held `swap_budget_offer` says which piece and which product; this action
    only says "yes". The room is re-derived from that verified offer and the
    budget raised to fit it (CLAUDE.md 27).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["swap_confirm"] = "swap_confirm"


class SwapDeclineAction(BaseModel):
    """Do not stretch the budget for the swap just offered.

    Applies nothing: the room stays exactly as it was before the swap. It moves
    the held offer to its second question - whether to show cheaper options for
    that one piece.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["swap_decline"] = "swap_decline"


class SwapKeepOriginalAction(BaseModel):
    """Keep the room exactly as it was before the dearer swap was tried.

    The over-budget swap never committed, so the original in-budget room is
    still in state: this clears the held offer and re-presents that room, so the
    customer sees they are back to what they had (CLAUDE.md 27).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["swap_keep_original"] = "swap_keep_original"


class SwapAlternativesAction(BaseModel):
    """Show cheaper options for the piece the customer was swapping.

    Lists alternatives for the held piece alone, bounded so the room stays
    within budget, every other piece kept exactly as it is. Selecting one
    performs the swap into that role - it is never a fresh product pick, so a
    room being edited never cross-sells a piece it already holds (CLAUDE.md 27).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["swap_alternatives"] = "swap_alternatives"


class SwapDismissAction(BaseModel):
    """No to cheaper options: leave the room exactly as it was, offer cleared."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["swap_dismiss"] = "swap_dismiss"


class UpgradeAcceptAction(BaseModel):
    """Yes to the step-up offered for one piece of the room: show the room with
    it. The held `room_upgrade_offer` says which piece and which product; this
    only says "yes". The swap is re-derived from that verified offer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["upgrade_accept"] = "upgrade_accept"


class UpgradeDeclineAction(BaseModel):
    """No to the step-up: the package stays exactly as built, offer cleared."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["upgrade_decline"] = "upgrade_decline"


BundleActionRequest = Annotated[
    BundleAlternativesAction
    | BundleSwapAction
    | SwapConfirmAction
    | SwapDeclineAction
    | SwapKeepOriginalAction
    | SwapAlternativesAction
    | SwapDismissAction
    | UpgradeAcceptAction
    | UpgradeDeclineAction,
    Field(discriminator="kind"),
]
"""Any room-edit action, told apart by `kind`."""
