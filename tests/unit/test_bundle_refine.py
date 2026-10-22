"""Naming a piece of the room, and changing whether it stays.

The invariant everything here protects: "the second one" must mean the second
piece the customer was *shown*. Cards merge lines, so counting lines would
point somewhere else; the optimiser sorts locks by product id while the commit
preserves state order, so grouping the outcome would order them differently
again. One grouping rule, over state, used by both renderings.

The second theme is restraint. Locking changes nothing about what the room
contains or costs, so nothing is searched, planned or re-optimised, and
"you can change it later" stays permission rather than becoming a replacement.
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
    CustomerAgentDecision,
    ProductInteractionIntent,
    ProductInteractionOp,
)
from app.schemas.agent_state import (
    AgentStateV1,
    BundleItemStatus,
    RoomProjectState,
)
from app.schemas.agent_updates import (
    AgentStateUpdate,
    DesignNeedSpec,
    PlannedBundleLineSpec,
    ReplaceDesignPlan,
    RoomProjectUpdate,
)
from app.schemas.bundle_reference import (
    BundleCategoryMatch,
    BundleItemOrdinal,
)
from app.schemas.design import DesignPriority
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import PriceConstraint
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.product_reference import PresentedOrdinal
from app.services.agent_state import apply_update
from app.services.bundle_cards import group_bundle_cards
from app.services.bundle_presentation import build_state_bundle_presentation
from app.taxonomy.registry import load_taxonomy
from pydantic import ValidationError

TAXONOMY = load_taxonomy()
CONTEXT = __import__("app.schemas.retailer", fromlist=["RetailerContext"]).RetailerContext(
    store_id=50
)


def product(
    product_id: int,
    *,
    category: str = "seating",
    subcategory: str | None = "sofa",
    price: str = "1000.00",
    unit: str = "SAR",
) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Item {product_id}",
        name_arabic="منتج",
        price_amount=Decimal(price),
        price_unit=unit,
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(category=category, subcategory=subcategory),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


def line(
    product_id: int,
    *,
    quantity: int = 1,
    acquisition: BundleAcquisition = BundleAcquisition.TO_BUY,
    locked: bool = False,
    need_index: int | None = None,
) -> PlannedBundleLineSpec:
    return PlannedBundleLineSpec(
        product_id=product_id,
        quantity=quantity,
        acquisition=acquisition,
        status=BundleItemStatus.LOCKED if locked else BundleItemStatus.SUGGESTED,
        need_index=need_index,
    )


def room_with(
    *lines: PlannedBundleLineSpec, needs: int = 1, budget: str | None = None
) -> AgentStateV1:
    state = apply_update(
        AgentStateV1(),
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                budget=PriceConstraint.at_most(Decimal(budget), "SAR") if budget else None,
                bundle_operations=(
                    ReplaceDesignPlan(
                        needs=tuple(
                            DesignNeedSpec(
                                commerce_category="seating",
                                commerce_subcategory="sofa",
                                priority=DesignPriority.REQUIRED,
                            )
                            for _ in range(needs)
                        ),
                        added=lines,
                    ),
                )
            )
        ),
    )
    return state


def room(state: AgentStateV1) -> RoomProjectState:
    project = state.room_project
    assert isinstance(project, RoomProjectState)
    return project


# ── E4A regression ══════════════════════════════════════════════════════════


def test_a_durable_plan_does_not_require_a_room_type() -> None:
    """A plan may exist for a room the customer never named."""
    state = room_with(line(10, need_index=0))

    assert room(state).room_type is None
    assert len(room(state).design_needs) == 1


# ── canonical cards ═════════════════════════════════════════════════════════


def test_lines_group_by_product_acquisition_and_lock() -> None:
    state = room_with(
        line(10, quantity=1),
        line(10, quantity=2),
        line(10, locked=True),
        line(10, acquisition=BundleAcquisition.ALREADY_OWNED, locked=True),
        line(11),
        needs=0,
    )

    cards = group_bundle_cards(room(state).bundle_items)

    assert len(cards) == 4
    assert cards[0].quantity == 3, "identical lines merge"
    assert cards[0].line_ids == (1, 2)
    assert [c.locked for c in cards] == [False, True, True, False]


def test_cards_follow_the_first_contributing_state_line() -> None:
    """Durable and needs no catalog fact, so it survives into a later turn."""
    state = room_with(line(30), line(20), line(30), needs=0)

    assert [c.product_id for c in group_bundle_cards(room(state).bundle_items)] == [
        30,
        20,
    ]


def test_a_card_may_span_several_design_needs() -> None:
    state = room_with(line(10, need_index=0), line(10, need_index=1), needs=2)

    cards = group_bundle_cards(room(state).bundle_items)
    assert len(cards) == 1
    assert cards[0].need_ids == (1, 2)


def test_a_card_belonging_to_no_need_has_none() -> None:
    state = room_with(line(10), needs=0)

    assert group_bundle_cards(room(state).bundle_items)[0].need_ids == ()


# ── selectors ═══════════════════════════════════════════════════════════════


def test_there_is_no_focused_bundle_selector() -> None:
    """Nothing tracks a focused card, so nothing may claim one."""
    import app.schemas.bundle_reference as module

    assert not hasattr(module, "FocusedBundleItem")


# ── the decision contract ═══════════════════════════════════════════════════


def refine(
    op: BundleInteractionOp = BundleInteractionOp.LOCK, **kwargs: Any
) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.BUNDLE_REFINE,
        bundle_interaction=BundleInteractionIntent(
            op=op, selector=BundleItemOrdinal(ordinal=1)
        ),
        **kwargs,
    )


def test_a_room_edit_needs_the_change_it_makes() -> None:
    with pytest.raises(ValidationError):
        CustomerAgentDecision(action=AgentAction.BUNDLE_REFINE)


def test_only_a_room_edit_carries_one() -> None:
    with pytest.raises(ValidationError):
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            bundle_interaction=BundleInteractionIntent(
                op=BundleInteractionOp.LOCK, selector=BundleItemOrdinal(ordinal=1)
            ),
        )


def test_a_room_edit_makes_one_change_not_two() -> None:
    """Two identity-changing effects from one decision would leave the reply
    able to describe only one of them."""
    with pytest.raises(ValidationError):
        refine(
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=1)
            )
        )


def test_a_room_edit_carries_no_product_reference_or_anchor() -> None:
    with pytest.raises(ValidationError):
        refine(reference=PresentedOrdinal(position=1))


@pytest.mark.parametrize("forbidden", ["product_id", "line_id", "need_id", "revision", "price"])
def test_the_model_cannot_name_identity(forbidden: str) -> None:
    for model in (BundleInteractionIntent, BundleItemOrdinal, BundleCategoryMatch):
        for field in model.model_fields:
            assert forbidden not in field, f"{model.__name__}.{field}"


def test_the_operation_vocabulary_is_exactly_what_is_implemented() -> None:
    """A placeholder the application would refuse is worse than no member.

    `reject_product` is absent for a concrete reason rather than for scope:
    leaving a role deliberately empty needs a durable state nothing has, and
    without it the next optimisation would quietly refill it.
    """
    assert {op.value for op in BundleInteractionOp} == {
        "lock",
        "unlock",
        "set_acquisition",
        "replace_product",
        "remove_need",
    }
    assert "reject_product" not in {op.value for op in BundleInteractionOp}


# ── execution ═══════════════════════════════════════════════════════════════


# ── the reply ═══════════════════════════════════════════════════════════════


# ── the room, rebuilt from state ════════════════════════════════════════════


def rebuilt(state: AgentStateV1, *products: ProductCandidate) -> Any:
    return build_state_bundle_presentation(room(state), products)


def test_a_room_can_be_rendered_without_an_optimisation() -> None:
    state = room_with(line(10, quantity=2), line(11), needs=0)

    rendered = rebuilt(state, product(10, price="500.00"), product(11, price="250.00"))

    assert [i.grounding_ref for i in rendered.items] == [1, 2]
    assert rendered.items[0].quantity == 2
    assert rendered.items[0].new_spend_line_total == Decimal("1000.00")
    assert rendered.totals.new_spend_total == Decimal("1250.00")
    assert rendered.totals.currency == "SAR"


def test_an_owned_piece_still_has_no_fake_zero() -> None:
    state = room_with(
        line(10, acquisition=BundleAcquisition.ALREADY_OWNED, locked=True), needs=0
    )

    rendered = rebuilt(state, product(10))

    assert rendered.items[0].new_spend_line_total is None
    assert rendered.items[0].unit_price == Decimal("1000.00")
    assert rendered.totals.new_spend_total is None


def test_mixed_units_are_never_summed() -> None:
    state = room_with(line(10), line(11), needs=0)

    rendered = rebuilt(state, product(10, unit="SAR"), product(11, unit="USD"))

    assert rendered.totals.new_spend_total is None
    assert rendered.totals.currency is None
    assert rendered.totals.total_unavailable is not None


@pytest.mark.parametrize(
    ("budget", "exclusive", "within"),
    [("1000.00", False, True), ("999.99", False, False), ("1000.00", True, False)],
)
def test_the_budget_bound_is_evaluated_at_its_exact_endpoint(
    budget: str, exclusive: bool, within: bool
) -> None:
    state = apply_update(
        room_with(line(10), needs=0),
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                budget=PriceConstraint(
                    currency="SAR",
                    max_amount=Decimal(budget),
                    max_exclusive=exclusive,
                )
            )
        ),
    )

    rendered = rebuilt(state, product(10, price="1000.00"))

    assert rendered.totals.within_budget is within


def test_an_unsupported_budget_form_gets_no_compliance_claim() -> None:
    state = apply_update(
        room_with(line(10), needs=0),
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                budget=PriceConstraint(currency="SAR", min_amount=Decimal("100"))
            )
        ),
    )

    rendered = rebuilt(state, product(10))

    assert rendered.totals.within_budget is None


def test_a_room_with_an_unreadable_piece_is_not_rendered_at_all() -> None:
    """Dropping the card would show a room missing a piece the customer has,
    and would renumber everything after it."""
    from app.core.exceptions import BundlePresentationError

    state = room_with(line(10), line(11), needs=0)
    before = room(state).bundle_items

    with pytest.raises(BundlePresentationError):
        rebuilt(state, product(10))

    assert room(state).bundle_items == before, "state is not a rendering decision"


def test_the_state_renderer_touches_no_float() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/services/bundle_presentation.py").read_text()
    assert "float(" not in source


# ── the model-facing view ═══════════════════════════════════════════════════




# ── first view == next view ═════════════════════════════════════════════════
#
# The divergence this phase closed: the optimiser sorts locked lines by product
# id, while the commit preserves state order. Grouping the outcome for the
# first rendering and state for the next would put the same room in two
# different orders, and "the second one" would mean two different pieces.




def _committed(
    *lines: PlannedBundleLineSpec, products: tuple[ProductCandidate, ...]
) -> tuple[Any, Any]:
    """The room as first shown, and as rebuilt from state afterwards."""
    from app.schemas.bundle import BundleLine, BundleStatus, RoomBundle
    from app.services.bundle_presentation import present_room_bundle

    state = room_with(*lines, needs=0)
    by_id = {p.product_id: p for p in products}
    outcome = RoomBundle(
        # Deliberately the order the optimiser would produce: locked first,
        # sorted by product id, then the rest.
        lines=tuple(
            BundleLine(
                need_index=None,
                product=by_id[item.product_id],
                quantity=item.quantity,
                locked=item.status is BundleItemStatus.LOCKED,
                acquisition=item.acquisition,
                relaxation_depth=None if item.status is BundleItemStatus.LOCKED else 0,
            )
            for item in sorted(
                room(state).bundle_items,
                key=lambda i: (i.status is not BundleItemStatus.LOCKED, i.product_id),
            )
        ),
        status=BundleStatus.COMPLETE,
        new_spend_total=Decimal("1.00"),
        currency="SAR",
    )
    return present_room_bundle(outcome, room(state)), rebuilt(state, *products)


def _shape(rendered: Any) -> list[tuple[Any, ...]]:
    assert rendered is not None
    return [
        (
            item.grounding_ref,
            item.name_english,
            item.quantity,
            item.acquisition,
            item.locked,
        )
        for item in rendered.items
    ]


def test_the_room_first_shown_is_the_room_rebuilt_next_turn() -> None:
    products = (product(30), product(20), product(11))
    first, later = _committed(
        line(30),
        line(20, locked=True),
        line(11),
        products=products,
    )

    assert _shape(first) == _shape(later)
    assert [i.name_english for i in first.items] == ["Item 30", "Item 20", "Item 11"]


def test_quantities_and_merging_agree_across_the_two_renderings() -> None:
    products = (product(10), product(11))
    first, later = _committed(
        line(10, quantity=2),
        line(10, quantity=1),
        line(11, acquisition=BundleAcquisition.ALREADY_OWNED, locked=True),
        products=products,
    )

    assert _shape(first) == _shape(later)
    assert first.items[0].quantity == 3






# ── a stale card never moves the others ═════════════════════════════════════
#
# The bug this closes: hydration was allowed to decide what the visible list
# contained. A lamp the catalog could not return disappeared from the surface,
# every card after it moved up one, and "the third one" silently meant a
# different piece from the one the customer was looking at.
#
# Membership and order come from state. Hydration supplies facts and nothing
# else.


SOFA = product(10, category="seating", subcategory="sofa")
