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
    TurnFailureCode.PRODUCT_UNAVAILABLE: (
        "I wasn't able to pull up that product just now."
    ),
    TurnFailureCode.COMPARISON_TARGET_UNAVAILABLE: (
        "I wasn't able to put those side by side just now."
    ),
    TurnFailureCode.REFERENCE_UNRESOLVED: (
        "I wasn't able to work out which product you meant."
    ),
    TurnFailureCode.RESPONSE_UNAVAILABLE: (
        "I wasn't able to put a reply together just now."
    ),
}
"""Total over the failure codes, so a new one cannot fall through to silence.

`RESPONSE_UNAVAILABLE` is present for completeness only. The response layer
never emits it - a failure to generate becomes the fallback below, which needs
no model and therefore cannot itself fail.
"""

DESIGN_HANDOFF_WORDING = (
    "I can't help with planning a whole room yet, but I can help you find "
    "individual pieces."
)
"""Claims nothing about a design, a product selection, spatial analysis, or
when the capability might arrive."""

SIDE_NOTICE_WORDING: dict[SideEffectNotice, str] = {
    SideEffectNotice.SELECTION_NOT_UPDATED: "I wasn't able to save that selection.",
    SideEffectNotice.SELECTION_NOT_REMOVED: "I wasn't able to remove that selection.",
    SideEffectNotice.FOCUS_NOT_CHANGED: "I wasn't able to switch to that product.",
}

FALLBACK_WORDING: dict[ResponseOutcomeKind, str] = {
    ResponseOutcomeKind.ANSWER: "I'm not able to answer that just now.",
    ResponseOutcomeKind.SEARCH_RESULTS: "Here's what I found.",
    ResponseOutcomeKind.ZERO_RESULTS: (
        "I couldn't find anything matching that. It's worth trying a different "
        "description."
    ),
    ResponseOutcomeKind.PRODUCT_DETAIL: "Here are the details for that one.",
    ResponseOutcomeKind.COMPARISON: "Here's how those compare.",
    ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION: (
        "Could you tell me a little more about what you're after?"
    ),
}
"""What the customer gets when generation could not be used.

Each says only what the application already knows to be true. The cards are
rendered either way, so a bare "here's what I found" beside real products is a
usable answer rather than an apology.
"""

DETERMINISTIC_FALLBACK: dict[DeterministicResponseKind, str] = {
    DeterministicResponseKind.MODEL_CLARIFICATION: FALLBACK_WORDING[
        ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION
    ],
    DeterministicResponseKind.HANDLED_FAILURE: FAILURE_WORDING[
        TurnFailureCode.RESPONSE_UNAVAILABLE
    ],
    DeterministicResponseKind.DESIGN_HANDOFF: DESIGN_HANDOFF_WORDING,
}
"""For a deterministic branch whose own wording could not be produced - a
clarification with no question on it, for instance."""


def compose(*parts: str | None) -> str:
    """One message from several sentences, skipping the ones that are absent."""
    return " ".join(part.strip() for part in parts if part and part.strip())
