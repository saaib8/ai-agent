"""The deterministic, screen-driven room-swap path.

The coordinator wiring (`_apply_swap`, `_list_alternatives`) is exercised
end-to-end against the live stack; these lock in the pure pieces a live test
would not catch cheaply: that the synthesised decision a bundle action stands in
for is actually valid (a `BUNDLE_REFINE` with no interaction raised a 500 in the
first cut), and that the action union discriminates by `kind`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.agent_decision import AgentAction, BundleInteractionOp
from app.schemas.agent_state import RoomProjectState, SwapBudgetOfferStage
from app.schemas.agent_turn import RoomSwapContext, SwapBudgetOffer
from app.schemas.bundle import BundleStatus, RoomBundle
from app.schemas.bundle_action import (
    BundleActionRequest,
    BundleAlternativesAction,
    BundleSwapAction,
    SwapAlternativesAction,
    SwapConfirmAction,
    SwapDeclineAction,
    SwapDismissAction,
    SwapKeepOriginalAction,
)
from app.schemas.discovery import PriceConstraint
from app.schemas.reply_choice import ReplyChoice
from app.services.room_presentation import swap_offer_choices
from app.services.turn_coordinator import _bundle_action_decision, _over_budget
from pydantic import TypeAdapter, ValidationError


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
    confirm = adapter.validate_python({"kind": "swap_confirm"})
    decline = adapter.validate_python({"kind": "swap_decline"})
    keep = adapter.validate_python({"kind": "swap_keep_original"})
    show = adapter.validate_python({"kind": "swap_alternatives"})
    dismiss = adapter.validate_python({"kind": "swap_dismiss"})

    assert isinstance(alt, BundleAlternativesAction)
    assert isinstance(swap, BundleSwapAction)
    assert isinstance(confirm, SwapConfirmAction)
    assert isinstance(decline, SwapDeclineAction)
    assert isinstance(keep, SwapKeepOriginalAction)
    assert isinstance(show, SwapAlternativesAction)
    assert isinstance(dismiss, SwapDismissAction)


def test_the_yes_no_swap_answers_stand_in_for_valid_decisions() -> None:
    """The follow-ups to an over-budget swap carry no ordinal, so they stand in
    for a plain request rather than a refinement that must name a selector."""
    assert _bundle_action_decision(SwapConfirmAction()).action is AgentAction.ANSWER
    assert _bundle_action_decision(SwapDeclineAction()).action is AgentAction.ANSWER
    assert _bundle_action_decision(SwapKeepOriginalAction()).action is AgentAction.ANSWER
    assert _bundle_action_decision(SwapDismissAction()).action is AgentAction.ANSWER
    # Showing cheaper options for the slot is a search, worded like one.
    assert _bundle_action_decision(SwapAlternativesAction()).action is AgentAction.SEARCH


def _offer(stage: SwapBudgetOfferStage) -> SwapBudgetOffer:
    return SwapBudgetOffer(
        stage=stage,
        new_spend_total=Decimal(11400),
        budget_max=Decimal(10000),
        overage=Decimal(1400),
        currency="SAR",
    )


def _kinds(choices: tuple[ReplyChoice, ...]) -> list[str]:
    return [c.bundle_action.kind for c in choices if c.bundle_action is not None]


def test_keep_budget_offers_two_explicit_choices_not_a_yes_no() -> None:
    """Declining to stretch offers the two ways to stay in budget: keep the room
    already built, or look for a cheaper version of just that one piece."""
    stretch = swap_offer_choices(_offer(SwapBudgetOfferStage.STRETCH))
    assert _kinds(stretch) == ["swap_confirm", "swap_decline"]

    alternatives = swap_offer_choices(_offer(SwapBudgetOfferStage.ALTERNATIVES))
    assert _kinds(alternatives) == ["swap_keep_original", "swap_alternatives"]
    assert [c.label for c in alternatives] == ["Keep my original room", "Show cheaper options"]


def test_a_swap_context_names_a_piece_by_ordinal_and_role() -> None:
    """Which piece a list of alternatives is for, so a tap swaps it - no id,
    an ordinal and a role, exactly like every other room reference."""
    context = RoomSwapContext(bundle_ordinal=3, role="sofa")
    assert context.bundle_ordinal == 3 and context.role == "sofa"
    with pytest.raises(ValidationError):
        RoomSwapContext(bundle_ordinal=0, role="sofa")
    with pytest.raises(ValidationError):
        RoomSwapContext(bundle_ordinal=1, role="")


def test_a_chip_performs_at_most_one_kind_of_action() -> None:
    """A yes/no swap chip carries a bundle_action; it cannot also be a product
    action - the two are different requests and a chip is exactly one tap."""
    ReplyChoice(label="Yes", value="Yes, stretch it", bundle_action=SwapConfirmAction())
    with pytest.raises(ValidationError):
        ReplyChoice(
            label="Yes",
            value="Yes",
            bundle_action=SwapConfirmAction(),
            product_action={"kind": "goes_with", "pick": 1},
        )


def _room(budget: PriceConstraint | None) -> RoomProjectState:
    return RoomProjectState(budget=budget)


def _bundle(total: Decimal | None, currency: str | None) -> RoomBundle:
    if total is None:
        return RoomBundle(status=BundleStatus.COMPLETE, total_unavailable="no_priced_lines")
    return RoomBundle(status=BundleStatus.COMPLETE, new_spend_total=total, currency=currency)


def test_over_budget_reports_the_gap_only_when_the_total_exceeds_a_plain_ceiling() -> None:
    ceiling = PriceConstraint(currency="SAR", max_amount=Decimal(10000))

    # Over: the gap is returned.
    assert _over_budget(_bundle(Decimal(11400), "SAR"), _room(ceiling)) == Decimal(1400)
    # At or under the ceiling: within budget, nothing to offer.
    assert _over_budget(_bundle(Decimal(10000), "SAR"), _room(ceiling)) is None
    assert _over_budget(_bundle(Decimal(9930), "SAR"), _room(ceiling)) is None


def test_over_budget_never_compares_across_currencies_or_without_a_ceiling() -> None:
    ceiling = PriceConstraint(currency="SAR", max_amount=Decimal(10000))
    a_range = PriceConstraint(currency="SAR", min_amount=Decimal(1), max_amount=Decimal(10000))

    # Another currency cannot be compared without inventing a rate.
    assert _over_budget(_bundle(Decimal(11400), "USD"), _room(ceiling)) is None
    # No budget at all, or a range rather than a plain ceiling: nothing to break.
    assert _over_budget(_bundle(Decimal(11400), "SAR"), _room(None)) is None
    assert _over_budget(_bundle(Decimal(11400), "SAR"), _room(a_range)) is None
