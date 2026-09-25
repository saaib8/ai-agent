"""The public shape of one chat exchange.

Small on purpose. Everything a client renders already exists as an
application-built, customer-safe type; this module adds the envelope around
them and nothing else. Recreating a product DTO here would be a second place
that decides what a customer may see (CLAUDE.md 20.4).

What is deliberately absent from the response is longer than what is present:
no agent state, no decision, no grounding envelope, no retailer context, no
product ids, no prices the application did not compute, no model or prompt
identifiers, and nothing about Redis.

`session_revision` is the one runtime token a client does see. It is not
`bundle_revision`, not a search revision and not agent-state identity - it
counts persisted turns, and its purpose is to let a client say *this is the
session my screen was drawn from* on the next request.
"""

from __future__ import annotations

import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.agent_turn import CustomerResponse
from app.schemas.bundle_action import BundleActionRequest
from app.schemas.bundle_presentation import GroundedBundlePresentation
from app.schemas.comparison import ProductComparisonResult
from app.schemas.grounding import GroundedProduct
from app.schemas.search_action import SearchActionRequest

MAX_MESSAGE_CHARS = 2000
"""A message, not a transcript. Long enough for a detailed request about a
room; short enough that a pasted document cannot become a prompt."""

_SESSION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
"""The same rule the session store enforces when it builds a key.

Checked here as well, so a malformed id is a transport validation failure the
caller can act on rather than an error discovered deeper in.
"""


class ChatRequest(BaseModel):
    """One customer message, and where it belongs.

    No authentication fields: V1 has no identity, and adding a slot for one
    before it exists would invite callers to send something nothing verifies
    (CLAUDE.md 25).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    store_id: int = Field(ge=1)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    bundle_action: BundleActionRequest | None = None
    """A deterministic room edit the customer drove from the screen.

    When present, the turn is a structured action — swap this piece for that
    option — and no decision model is consulted: the customer's clicks are the
    decision (CLAUDE.md 3.6). `message` still carries a short human description
    of the action for the conversation record, but its content does not route
    the turn.
    """

    search_action: SearchActionRequest | None = None
    """A deterministic follow-up on a product search the customer drove from
    the screen — "show me different options", or "not this one".

    Like `bundle_action`, no decision model is consulted: the customer tapped a
    control, so the turn re-runs the search in progress while excluding what was
    already shown (or the one product turned down), giving retrieval a memory it
    otherwise lacks (CLAUDE.md 3.6). At most one of `bundle_action` and
    `search_action` is present on a turn.
    """

    expected_session_revision: int | None = Field(default=None, ge=0)
    """The session revision the client's screen was rendered from.

    Optional. Omitted means *use whatever is stored now*, which is right for a
    plain chat box with nothing on screen to go stale.

    Supplied, it is checked before any provider work begins. A client showing
    products from revision 5 that says "keep the second one" after another turn
    committed revision 6 is pointing at a list that has changed, and resolving
    the ordinal against the newer session would act on a product they never
    saw. Rejecting costs nothing; guessing costs the customer the wrong sofa.

    It never reaches either reasoning model.
    """

    @field_validator("session_id")
    @classmethod
    def _session_id_is_addressable(cls, value: str) -> str:
        """Restricted rather than escaped.

        `:` separates the parts of a session key, so an unrestricted id could
        name a key inside another retailer's namespace.
        """
        if not _SESSION_ID.fullmatch(value):
            raise ValueError("session_id may contain letters, digits, dot, dash and underscore")
        return value

    @field_validator("message")
    @classmethod
    def _a_message_has_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a message cannot be blank")
        return value

    @model_validator(mode="after")
    def _one_structured_action_at_most(self) -> Self:
        """A turn is either a room edit or a search follow-up, never both.

        Each takes its own deterministic path, so a turn carrying both would
        have one silently ignored - a defect the client should hear about at
        the boundary rather than discover from surprising results.
        """
        if self.bundle_action is not None and self.search_action is not None:
            raise ValueError("a turn carries at most one structured action")
        return self


class ChatPresentation(BaseModel):
    """What a client draws beside the assistant's words.

    Every field is an existing application-rendered type. The response model
    writes prose and cites `grounding_ref` handles; it does not produce any of
    this, which is what keeps a stated price and a rendered price the same
    number (CLAUDE.md 20.4, 25).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    products: tuple[GroundedProduct, ...] = ()
    """Search results, or the single product a detail turn was about.

    One field for both because a client renders them the same way - a list of
    cards - and the assistant's own words say which it is. Order is the order
    the turn established; a client must not re-sort it, because "the second
    one" is resolved against this sequence next turn.
    """

    comparison: ProductComparisonResult | None = None
    room: GroundedBundlePresentation | None = None

    seating_bundles: tuple[GroundedBundlePresentation, ...] = ()
    """Composed seating combinations, when a seat count no single piece met was
    recovered by pairing pieces (CLAUDE.md 27).

    A tuple rather than one `room`, because the planner offers a few
    alternatives and a client draws each as its own package. Every one is an
    existing `GroundedBundlePresentation`, so the same cards, prices and totals
    render with nothing new to build. Empty when the closest combination was
    over budget - then the reply owns the shortfall and there is nothing to draw.
    """

    def is_empty(self) -> bool:
        """Whether there is anything to draw.

        An answer, a clarification or a design question produces text and no
        cards, and sending an empty object rather than nothing would have every
        client check the same fields to discover that.
        """
        return (
            not self.products
            and self.comparison is None
            and self.room is None
            and not self.seating_bundles
        )


class ChatResponse(BaseModel):
    """One turn as the caller receives it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    session_revision: int = Field(ge=1)
    """The revision this turn committed. Always at least one: a response is
    returned only after the turn was persisted, so there is no successful
    exchange that leaves the session at zero."""

    response: CustomerResponse
    presentation: ChatPresentation | None = None
