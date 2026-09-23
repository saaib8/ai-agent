"""The deterministic, screen-driven room-swap path.

The coordinator wiring (`_apply_swap`, `_list_alternatives`) is exercised
end-to-end against the live stack; these lock in the pure pieces a live test
would not catch cheaply: that the synthesised decision a bundle action stands in
for is actually valid (a `BUNDLE_REFINE` with no interaction raised a 500 in the
first cut), and that the action union discriminates by `kind`.
"""

from __future__ import annotations

from app.schemas.agent_decision import AgentAction, BundleInteractionOp
from app.schemas.bundle_action import (
    BundleActionRequest,
    BundleAlternativesAction,
    BundleSwapAction,
)
from app.services.turn_coordinator import _bundle_action_decision
from pydantic import TypeAdapter


def test_a_swap_action_stands_in_for_a_valid_bundle_refinement() -> None:
    """A synthesised BUNDLE_REFINE must carry the change it makes, or the
    decision contract rejects it — which is exactly what 500'd the first cut."""
    decision = _bundle_action_decision(BundleSwapAction(bundle_ordinal=1, alternative_ordinal=2))

    assert decision.action is AgentAction.BUNDLE_REFINE
    assert decision.bundle_interaction is not None
    assert decision.bundle_interaction.op is BundleInteractionOp.REPLACE_PRODUCT


def test_an_alternatives_action_also_stands_in_for_a_valid_decision() -> None:
    decision = _bundle_action_decision(BundleAlternativesAction(bundle_ordinal=1))

    assert decision.action is AgentAction.BUNDLE_REFINE
    assert decision.bundle_interaction is not None


def test_the_action_union_discriminates_by_kind() -> None:
    adapter = TypeAdapter(BundleActionRequest)

    alt = adapter.validate_python({"kind": "list_alternatives", "bundle_ordinal": 3})
    swap = adapter.validate_python({"kind": "swap", "bundle_ordinal": 1, "alternative_ordinal": 2})

    assert isinstance(alt, BundleAlternativesAction)
    assert isinstance(swap, BundleSwapAction)
