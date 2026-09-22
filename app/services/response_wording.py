"""The sentences the application writes for itself.

Several branches never reach a model, for the same reason each time: there is
nothing a model could add that is worth the risk of it adding something else. A
question the decision model already wrote does not need re-wording. "I could
not do that just now" has no nuance to gain. A design handoff describes a
capability that does not exist yet, and a model given an empty grounding there
would be invited to improvise about it.

Every string here is fixed and holds no product fact, no figure, no internal
detail and no question - except the clarification pass-through, which is the
decision model's own question carried through unchanged.

Plain constants rather than a template engine: there is no substitution to do,
and a format string with a slot is a slot something can be interpolated into.
"""

from __future__ import annotations

from app.schemas.acquisition import BundleAcquisition
from app.schemas.bundle import BundleStatus, BundleUnavailableReason
from app.schemas.grounding import TurnFailureCode
from app.schemas.response import (
    DeterministicResponseKind,
    ResponseOutcomeKind,
    SideEffectNotice,
)

FAILURE_WORDING: dict[TurnFailureCode, str] = {
    TurnFailureCode.SEARCH_UNAVAILABLE: (
        "I wasn't able to run that search just now. Please try again in a moment."
    ),
    TurnFailureCode.PRODUCT_UNAVAILABLE: ("I wasn't able to pull up that product just now."),
    TurnFailureCode.COMPARISON_TARGET_UNAVAILABLE: (
        "I wasn't able to put those side by side just now."
    ),
    TurnFailureCode.REFERENCE_UNRESOLVED: ("I wasn't able to work out which product you meant."),
    TurnFailureCode.RESPONSE_UNAVAILABLE: ("I wasn't able to put a reply together just now."),
    TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE: (
        "One of the pieces you asked me to keep isn't available any more, so I "
        "wasn't able to plan the room around it."
    ),
    TurnFailureCode.BUNDLE_NOT_VERIFIABLE: (
        "I couldn't check everything currently in your room, so I wasn't sure "
        "which piece you meant."
    ),
    TurnFailureCode.NO_REPLACEMENT_CANDIDATE: (
        "I couldn't find another one of those to offer you, so I've left your "
        "current choice as it is."
    ),
    TurnFailureCode.REPLACEMENT_NOT_FEASIBLE: (
        "I found alternatives, but none of them works alongside the rest of the "
        "room, so I've left your current choice as it is."
    ),
    TurnFailureCode.NOTHING_SELECTED: (
        "You haven't picked anything out yet, so there's nothing to show you "
        "here."
    ),
    TurnFailureCode.DESIGN_ADVICE_UNAVAILABLE: (
        "I wasn't able to answer that one just now. Please try again in a "
        "moment."
    ),
    TurnFailureCode.DESIGN_UNAVAILABLE: (
        "I wasn't able to put a room plan together just now. Please try again in a moment."
    ),
}
"""Total over the failure codes, so a new one cannot fall through to silence.

`LOCKED_PRODUCT_UNAVAILABLE` says what happened and stops: it names no
piece, offers no substitute and does not unlock anything. `DESIGN_UNAVAILABLE`
says the planning failed and never that the retailer has nothing suitable -
that would be a claim about the catalog we did not establish.

`RESPONSE_UNAVAILABLE` is present for completeness only. The response layer
never emits it - a failure to generate becomes the fallback below, which needs
no model and therefore cannot itself fail.
"""

BUNDLE_UNAVAILABLE_WORDING: dict[BundleUnavailableReason, str] = {
    BundleUnavailableReason.UNSUPPORTED_BUDGET_FORM: (
        "I can work with a maximum you'd like to stay under, but not with the "
        "budget as you've described it, so I wasn't able to put the room "
        "together yet."
    ),
    BundleUnavailableReason.BUDGET_NOT_COMPARABLE: (
        "One of the pieces you asked me to keep is priced differently from the "
        "budget you gave me, so I can't compare the two reliably enough to "
        "build the package."
    ),
    BundleUnavailableReason.LOCKED_PRICE_UNUSABLE: (
        "I couldn't confirm the current price of one of the pieces you asked "
        "me to keep, so I wasn't able to work out what the room would come to."
    ),
}
"""Total over the optimiser's refusals, so a new one cannot fall through.

Each says what stopped the calculation and stops. None names a piece, quotes a
figure, mentions a store, or proposes the remedy - unlocking something or
changing a budget is the customer's decision, and a fixed sentence that
suggested it would be making it for them.
"""

BUNDLE_KEPT_WORDING = "That piece will stay in the room."
"""No product name, no figure, no question. The cards are rendered beside it."""

BUNDLE_UNLOCKED_WORDING = "That piece can change in later refinements."
"""Says what changed - permission - and does not promise a replacement."""

BUNDLE_ACQUISITION_WORDING: dict[BundleAcquisition, str] = {
    BundleAcquisition.ALREADY_OWNED: (
        "I'll treat that as something you already have, so it won't count towards what you spend."
    ),
    BundleAcquisition.TO_BUY: ("I'll count that as something you still need to buy."),
}
"""What the customer told us about owning a piece, said back plainly.

Neither sentence says the piece may change: an already-owned line is locked,
and nothing replaces it until the customer asks. Saying otherwise is exactly
the bug this wording exists to fix.

No product name, no figure, no enum, and no question - the card beside it
carries the rest.
"""

BUNDLE_CHANGED_NOT_REFRESHED_WORDING = (
    "I've made that change. I wasn't able to work the rest of the room out "
    "again just now, so what you can see may be out of date."
)
"""Both halves, in order. The change is stated as done because it is done, and
the room is described as stale rather than as wrong."""

DESIGN_HANDOFF_WORDING = "I wasn't able to put that together just now."
"""A design request that produced nothing to show.

It used to say the service could not plan a whole room yet. That stopped being
true when whole-room planning shipped, and it was doubly wrong once the same
route began answering "what goes with this?" - a customer who asked for one
complementary piece was told the room feature did not exist.

So it claims nothing at all: not about the design, not about a selection, not
about what the capability can or cannot do. Something did not come back, and
that is the whole message."""

SIDE_NOTICE_WORDING: dict[SideEffectNotice, str] = {
    SideEffectNotice.SELECTION_NOT_UPDATED: "I wasn't able to save that selection.",
    SideEffectNotice.SELECTION_NOT_REMOVED: "I wasn't able to remove that selection.",
    SideEffectNotice.FOCUS_NOT_CHANGED: "I wasn't able to switch to that product.",
}

FALLBACK_WORDING: dict[ResponseOutcomeKind, str] = {
    ResponseOutcomeKind.ANSWER: "I'm not able to answer that just now.",
    ResponseOutcomeKind.SEARCH_RESULTS: "Here's what I found.",
    ResponseOutcomeKind.ZERO_RESULTS: (
        "I couldn't find anything matching that. It's worth trying a different description."
    ),
    ResponseOutcomeKind.SELECTION: "Here's what you've picked out so far.",
    ResponseOutcomeKind.PRODUCT_DETAIL: "Here are the details for that one.",
    ResponseOutcomeKind.COMPARISON: "Here's how those compare.",
    ResponseOutcomeKind.DESIGN_ADVICE: (
        "I wasn't able to put that answer into words just now."
    ),
    ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION: (
        "Could you tell me a little more about what you're after?"
    ),
}
"""What the customer gets when generation could not be used.

Each says only what the application already knows to be true. The cards are
rendered either way, so a bare "here's what I found" beside real products is a
usable answer rather than an apology.
"""

BUNDLE_FALLBACK_WORDING: dict[BundleStatus, str] = {
    BundleStatus.COMPLETE: ("Here's a room package covering everything it needs."),
    BundleStatus.PARTIAL: (
        "Here's a partial room package - some of the pieces it needs are still missing."
    ),
    BundleStatus.INFEASIBLE: (
        "The pieces you asked me to keep don't fit within the budget you gave "
        "me, so I couldn't put a package together around them."
    ),
}
"""What a whole-room turn says when generation could not be used.

Keyed by status rather than by outcome kind, because `FALLBACK_WORDING` cannot
tell the three apart and calling a partial room complete is exactly the mistake
worth spending a second table to avoid. Digit-free: the figures are rendered
beside the prose either way.
"""

DETERMINISTIC_FALLBACK: dict[DeterministicResponseKind, str] = {
    DeterministicResponseKind.MODEL_CLARIFICATION: FALLBACK_WORDING[
        ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION
    ],
    DeterministicResponseKind.HANDLED_FAILURE: FAILURE_WORDING[
        TurnFailureCode.RESPONSE_UNAVAILABLE
    ],
    DeterministicResponseKind.DESIGN_HANDOFF: DESIGN_HANDOFF_WORDING,
    DeterministicResponseKind.BUNDLE_CHANGED_NOT_REFRESHED: (BUNDLE_CHANGED_NOT_REFRESHED_WORDING),
    DeterministicResponseKind.BUNDLE_KEPT: BUNDLE_KEPT_WORDING,
    DeterministicResponseKind.BUNDLE_UNLOCKED: BUNDLE_UNLOCKED_WORDING,
    # Deliberately the owned sentence rather than a neutral one: this map is
    # reached only when the branch's own wording could not be produced, and
    # the safer of the two is the one that promises no change.
    DeterministicResponseKind.BUNDLE_ACQUISITION_SET: BUNDLE_ACQUISITION_WORDING[
        BundleAcquisition.ALREADY_OWNED
    ],
    DeterministicResponseKind.BUNDLE_UNAVAILABLE: (
        "I wasn't able to work the room package out just now."
    ),
}
"""For a deterministic branch whose own wording could not be produced - a
clarification with no question on it, for instance."""


def compose(*parts: str | None) -> str:
    """One message from several sentences, skipping the ones that are absent."""
    return " ".join(part.strip() for part in parts if part and part.strip())


def fallback_for(view: ResponseOutcomeKind, bundle: BundleStatus | None = None) -> str:
    """The sentence a turn falls back to when generation could not be used.

    A whole-room turn reads a second table, because `FALLBACK_WORDING` is keyed
    by outcome kind and the three bundle statuses need three different
    sentences: calling a partial room complete is the one mistake this whole
    layer exists to prevent.
    """
    if view is ResponseOutcomeKind.ROOM_BUNDLE:
        assert bundle is not None, "a room bundle outcome carries its status"
        return BUNDLE_FALLBACK_WORDING[bundle]
    return FALLBACK_WORDING[view]
