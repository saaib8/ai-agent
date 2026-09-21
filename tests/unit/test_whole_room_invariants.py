"""Invariants that only show up across a sequence of turns.

Each of these holds within one turn in tests elsewhere. What is checked here is
that they keep holding as turns compose - that an id issued three turns ago
cannot be handed out again, that a card's position survives into the turn where
the customer refers to it, and that money never stops being exact.

The identity rules exist because references are positional and durable state is
not. "The second one" is resolved against committed state, so any rule that let
an id be reused, or a position shift, would silently point a later turn at a
different product.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    BundleInteractionIntent,
    BundleInteractionOp,
    BundleReplacementIntent,
    BundleReplacementMode,
    CustomerAgentDecision,
)
from app.schemas.bundle import TotalUnavailableReason
from app.schemas.bundle_reference import BundleCategoryMatch, BundleItemOrdinal
from app.schemas.discovery import PriceConstraint
from app.schemas.resolution import (
    BundleReferenceFailureReason,
    BundleReferenceUnresolved,
)
from app.services.bundle_cards import group_bundle_cards
from app.services.bundle_money import SpendLine, new_spend
from app.services.bundle_presentation import build_state_bundle_presentation
from app.services.bundle_reference import BundleReferenceResolver
from app.taxonomy.registry import load_taxonomy

from tests.unit.test_design_revision import (
    a_room,
    category_need,
    chosen,
    line,
    need,
    plan_of,
    product,
    room,
    run,
)
from tests.unit.test_turn_coordinator import FakeDesign
from tests.unit.test_whole_room_e2e import bundle_of, refine

# ── identity is never handed out twice (M12F 8) ═════════════════════════════


async def test_a_line_id_is_never_reused_after_its_line_is_gone() -> None:
    """A dangling reference must stay dangling rather than resolve elsewhere."""
    state = a_room(
        needs=(need(), need("lighting", "floor-lamp")),
        lines=(line(10, need_index=0), line(11, need_index=1)),
    )
    retired = {item.line_id for item in room(state).bundle_items}

    result, _ = await run(
        refine(
            BundleInteractionOp.REPLACE_PRODUCT,
            replacement=BundleReplacementIntent(mode=BundleReplacementMode.ALTERNATIVE),
        ),
        state,
        available=(10, 11, 77),
        outcome=bundle_of((77, 0, "900.00"), (11, 1, "500.00")),
    )

    after = room(result.state)
    assert {item.line_id for item in after.bundle_items}.isdisjoint(retired)
    assert after.next_bundle_line_id > max(retired)


async def test_the_counter_rises_even_when_the_highest_line_is_removed() -> None:
    """Never `max(existing) + 1`: removing the newest line would then let the
    next one take its id, and a reference to the removed one would resolve."""
    state = a_room(
        needs=(need(), need("lighting", "floor-lamp")),
        lines=(line(10, need_index=0), line(11, need_index=1)),
    )
    highest = max(item.line_id for item in room(state).bundle_items)

    result, _ = await run(
        CustomerAgentDecision(
            action=AgentAction.BUNDLE_REFINE,
            bundle_interaction=BundleInteractionIntent(
                op=BundleInteractionOp.REMOVE_NEED,
                selector=BundleItemOrdinal(ordinal=2),
            ),
        ),
        state,
        available=(10, 11, 77),
        outcome=chosen(10),
    )

    after = room(result.state)
    assert after.next_bundle_line_id > highest
    assert highest not in {item.line_id for item in after.bundle_items}


async def test_refining_keeps_the_roles_while_recomposing_reissues_them() -> None:
    """The distinction the two commit paths exist to preserve."""
    state = a_room(needs=(need(),), lines=(line(10),))
    original = [n.need_id for n in room(state).design_needs]

    refined, _ = await run(
        refine(
            BundleInteractionOp.REPLACE_PRODUCT,
            replacement=BundleReplacementIntent(mode=BundleReplacementMode.ALTERNATIVE),
        ),
        state,
        available=(10, 77),
        outcome=chosen(77),
    )
    assert [n.need_id for n in room(refined.state).design_needs] == original

    replanned, _ = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        refined.state,
        available=(77, 78),
        design=FakeDesign(plan_of(category_need("seating", "sofa"))),
        outcome=chosen(78),
    )
    assert {n.need_id for n in room(replanned.state).design_needs}.isdisjoint(original)


async def test_a_rejection_does_not_outlive_the_role_it_was_made_for() -> None:
    """Rejections are scoped to a need. A replan retires the need, so a product
    turned down for a role that no longer exists is eligible again."""
    state = a_room(needs=(need(),), lines=(line(10),))

    rejected, _ = await run(
        refine(
            BundleInteractionOp.REPLACE_PRODUCT,
            replacement=BundleReplacementIntent(mode=BundleReplacementMode.ALTERNATIVE),
        ),
        state,
        available=(10, 77),
        outcome=chosen(77),
    )
    assert room(rejected.state).design_needs[0].rejected_product_ids == (10,)

    replanned, _ = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        rejected.state,
        available=(77, 78),
        design=FakeDesign(plan_of(category_need("seating", "sofa"))),
        outcome=chosen(78),
    )
    assert room(replanned.state).design_needs[0].rejected_product_ids == ()


@pytest.mark.parametrize("already_locked", [True, False])
async def test_the_revision_moves_only_when_the_room_actually_changes(
    already_locked: bool,
) -> None:
    state = a_room(needs=(need(),), lines=(line(10, locked=already_locked),))
    before = room(state).bundle_revision

    result, _ = await run(refine(BundleInteractionOp.LOCK), state)

    after = room(result.state).bundle_revision
    assert (after == before) is already_locked, "content, not effort"


# ── money stays exact (M12F 9) ══════════════════════════════════════════════


def test_the_spend_is_decimal_throughout_and_never_a_float() -> None:
    total, currency, unavailable = new_spend(
        [
            SpendLine(Decimal("1200.50"), "SAR", 2, BundleAcquisition.TO_BUY),
            SpendLine(Decimal("99.99"), "SAR", 1, BundleAcquisition.TO_BUY),
        ]
    )

    assert isinstance(total, Decimal)
    assert total == Decimal("2500.99"), "exact, not 2500.9899999"
    assert currency == "SAR" and unavailable is None


def test_two_currencies_are_refused_rather_than_converted() -> None:
    """No FX rate exists in this service, and inventing one would state a
    figure nobody can verify."""
    total, currency, unavailable = new_spend(
        [
            SpendLine(Decimal("1000"), "SAR", 1, BundleAcquisition.TO_BUY),
            SpendLine(Decimal("1000"), "USD", 1, BundleAcquisition.TO_BUY),
        ]
    )

    assert total is None and currency is None
    assert unavailable is TotalUnavailableReason.MIXED_PRICE_UNITS


def test_a_piece_they_own_is_absent_from_the_spend_not_priced_at_zero() -> None:
    total, currency, _ = new_spend(
        [
            SpendLine(Decimal("4000"), "SAR", 1, BundleAcquisition.ALREADY_OWNED),
            SpendLine(Decimal("600"), "SAR", 1, BundleAcquisition.TO_BUY),
        ]
    )

    assert total == Decimal("600"), "the owned piece contributes nothing"
    assert currency == "SAR"


def test_an_owned_only_room_has_no_total_rather_than_a_total_of_zero() -> None:
    """Zero would read as "this room is free", which is a different claim."""
    total, currency, unavailable = new_spend(
        [SpendLine(Decimal("4000"), "SAR", 1, BundleAcquisition.ALREADY_OWNED)]
    )

    assert total is None and currency is None
    assert unavailable is TotalUnavailableReason.NO_PRICED_LINES


@pytest.mark.parametrize(
    ("exclusive", "at_limit"), [(True, False), (False, True)]
)
def test_a_ceiling_is_honoured_exactly_as_the_customer_stated_it(
    exclusive: bool, at_limit: bool
) -> None:
    room_state = a_room(needs=(need(),), lines=(line(10),))
    budget = PriceConstraint(
        currency="SAR", max_amount=Decimal("1000.00"), max_exclusive=exclusive
    )
    project = room(room_state).model_copy(update={"budget": budget})

    presentation = _shown(project, [product(10)])

    assert presentation.totals.new_spend_total == Decimal("1000.00")
    assert presentation.totals.within_budget is at_limit


# ── references survive into the next turn (M12F 10) ═════════════════════════


def _resolver() -> BundleReferenceResolver:
    return BundleReferenceResolver(load_taxonomy())


def _resolved(outcome: Any) -> Any:
    assert not isinstance(outcome, BundleReferenceUnresolved), outcome
    return outcome


def _unresolved(outcome: Any) -> Any:
    assert isinstance(outcome, BundleReferenceUnresolved), outcome
    return outcome


def _shown(project: Any, products: Any) -> Any:
    presentation = build_state_bundle_presentation(project, products)
    assert presentation is not None
    return presentation


def test_a_cards_position_is_the_one_the_customer_was_shown() -> None:
    """Rendering and resolution read the same list, so "the second one" next
    turn is the second one they saw."""
    state = a_room(
        needs=(need(), need("decor", "carpet")),
        lines=(line(10, need_index=0), line(11, need_index=1)),
    )
    project = room(state)
    products = [product(10), product(11, category="decor", subcategory="carpet")]

    shown = _shown(project, products)
    resolved = _resolved(
        _resolver().resolve(BundleItemOrdinal(ordinal=2), project, products)
    )

    assert [item.grounding_ref for item in shown.items] == [1, 2]
    assert resolved.product_id == 11
    assert shown.items[1].name_english == "Item 11"


def test_a_missing_product_never_renumbers_the_cards_after_it() -> None:
    """The stale-card rule. Resolution is state-first, so position two is still
    the second line even when the first cannot currently be described."""
    state = a_room(
        needs=(need(), need("decor", "carpet")),
        lines=(line(10, need_index=0), line(11, need_index=1)),
    )
    project = room(state)
    only_second = [product(11, category="decor", subcategory="carpet")]

    cards = group_bundle_cards(project.bundle_items)
    resolved = _resolved(
        _resolver().resolve(BundleItemOrdinal(ordinal=2), project, only_second)
    )

    assert len(cards) == 2, "membership comes from state, not from the catalog"
    assert resolved.product_id == 11, "not compacted to position one"


def test_an_unverifiable_room_refuses_a_category_reference() -> None:
    """Fails closed: "the sofa" cannot be shown to be unique when part of the
    room cannot be read at all."""
    state = a_room(
        needs=(need(), need("decor", "carpet")),
        lines=(line(10, need_index=0), line(11, need_index=1)),
    )
    project = room(state)

    outcome = _unresolved(
        _resolver().resolve(
            BundleCategoryMatch(commerce_category="seating"), project, [product(10)]
        )
    )

    assert outcome.reason is BundleReferenceFailureReason.BUNDLE_NOT_VERIFIABLE
