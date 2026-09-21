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
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.agent_updates import (
    AgentStateUpdate,
    DesignNeedSpec,
    PlannedBundleLineSpec,
    ReplaceDesignPlan,
    RoomProjectUpdate,
)
from app.schemas.agent_view import RoomProjectView
from app.schemas.bundle_reference import (
    BundleCategoryMatch,
    BundleItemOrdinal,
    BundleReferenceSelector,
)
from app.schemas.design import DesignPriority
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import PriceConstraint
from app.schemas.grounding import TurnFailureCode
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.resolution import (
    BundleReferenceFailureReason,
    BundleReferenceUnresolved,
    ResolvedBundleReference,
)
from app.schemas.response import DeterministicResponse, DeterministicResponseKind
from app.services.agent_state import apply_update
from app.services.agent_view import project_state
from app.services.bundle_cards import group_bundle_cards
from app.services.bundle_presentation import build_state_bundle_presentation
from app.services.bundle_reference import BundleReferenceResolver
from app.services.response_view import route_response
from app.taxonomy.registry import load_taxonomy
from pydantic import ValidationError

from tests.unit.test_turn_coordinator import FakeHydration, _coordinator

TAXONOMY = load_taxonomy()
RESOLVER = BundleReferenceResolver(TAXONOMY)
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


def resolve(
    selector: BundleReferenceSelector,
    state: AgentStateV1,
    products: tuple[ProductCandidate, ...],
) -> Any:
    return RESOLVER.resolve(selector, state.room_project, products)


def test_an_ordinal_names_the_card_at_that_position() -> None:
    state = room_with(line(10), line(11), needs=0)

    outcome = resolve(BundleItemOrdinal(ordinal=2), state, (product(10), product(11)))

    assert isinstance(outcome, ResolvedBundleReference)
    assert outcome.product_id == 11
    assert outcome.ordinal == 2


def test_an_ordinal_counts_cards_rather_than_lines() -> None:
    """Three lines, two cards: there is a second piece and no third."""
    state = room_with(line(10), line(10), line(11), needs=0)

    assert isinstance(
        resolve(BundleItemOrdinal(ordinal=2), state, (product(10), product(11))),
        ResolvedBundleReference,
    )
    beyond = resolve(BundleItemOrdinal(ordinal=3), state, (product(10), product(11)))
    assert isinstance(beyond, BundleReferenceUnresolved)
    assert beyond.reason is BundleReferenceFailureReason.ORDINAL_OUT_OF_RANGE


def test_an_ordinal_beyond_the_room_is_a_question() -> None:
    state = room_with(line(10), needs=0)

    outcome = resolve(BundleItemOrdinal(ordinal=4), state, (product(10),))

    assert isinstance(outcome, BundleReferenceUnresolved)
    assert outcome.reason is BundleReferenceFailureReason.ORDINAL_OUT_OF_RANGE


def test_no_room_means_nothing_to_count_into() -> None:
    outcome = RESOLVER.resolve(BundleItemOrdinal(ordinal=1), None, ())

    assert isinstance(outcome, BundleReferenceUnresolved)
    assert outcome.reason is BundleReferenceFailureReason.NO_BUNDLE


def test_a_unique_type_resolves() -> None:
    state = room_with(line(10), line(11), needs=0)
    products = (product(10), product(11, category="lighting", subcategory="floor-lamp"))

    outcome = resolve(
        BundleCategoryMatch(commerce_category="lighting", commerce_subcategory="floor-lamp"),
        state,
        products,
    )

    assert outcome.product_id == 11


def test_a_parent_category_resolves_when_only_one_card_matches() -> None:
    state = room_with(line(10), line(11), needs=0)
    products = (product(10), product(11, category="lighting", subcategory="floor-lamp"))

    outcome = resolve(BundleCategoryMatch(commerce_category="lighting"), state, products)

    assert outcome.product_id == 11


def test_two_pieces_of_one_kind_is_a_question_not_a_coin_toss() -> None:
    state = room_with(line(10), line(11), needs=0)

    outcome = resolve(
        BundleCategoryMatch(commerce_category="seating", commerce_subcategory="sofa"),
        state,
        (product(10), product(11)),
    )

    assert isinstance(outcome, BundleReferenceUnresolved)
    assert outcome.reason is BundleReferenceFailureReason.SEVERAL_CATEGORY_MATCHES


def test_a_type_the_room_does_not_hold_is_a_question() -> None:
    state = room_with(line(10), needs=0)

    outcome = resolve(
        BundleCategoryMatch(commerce_category="lighting", commerce_subcategory="floor-lamp"),
        state,
        (product(10),),
    )

    assert isinstance(outcome, BundleReferenceUnresolved)
    assert outcome.reason is BundleReferenceFailureReason.NO_CATEGORY_MATCH


def test_a_type_outside_the_vocabulary_names_nothing() -> None:
    """Refused rather than matched to whatever looks closest."""
    state = room_with(line(10), needs=0)

    outcome = resolve(
        BundleCategoryMatch(commerce_category="furniture", commerce_subcategory="couch"),
        state,
        (product(10),),
    )

    assert isinstance(outcome, BundleReferenceUnresolved)
    assert outcome.reason is BundleReferenceFailureReason.UNAPPROVED_COMMERCE_TYPE


def test_the_resolver_hardcodes_no_vocabulary() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/services/bundle_reference.py").read_text()
    for category in ("seating", "sofa", "lighting", "tables", "carpet"):
        assert category not in source, category


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


async def run(
    decision: CustomerAgentDecision,
    state: AgentStateV1,
    *,
    available: tuple[int, ...] | None = None,
    **parts: Any,
) -> Any:
    ids = available if available is not None else tuple(
        i.product_id for i in room(state).bundle_items
    )
    coordinator, built = _coordinator(
        decision, hydration=FakeHydration(available=ids), **parts
    )
    result = await coordinator.run(
        CustomerTurnInput(message="keep that one", state=state, context=CONTEXT)
    )
    return result, built


async def test_locking_changes_every_line_behind_the_card() -> None:
    state = room_with(line(10), line(10), line(11), needs=0)

    result, _ = await run(refine(), state)

    after = room(result.state)
    assert [i.status for i in after.bundle_items] == [
        BundleItemStatus.LOCKED,
        BundleItemStatus.LOCKED,
        BundleItemStatus.SUGGESTED,
    ]


async def test_locking_preserves_everything_else_about_the_line() -> None:
    state = room_with(
        line(10, quantity=3, acquisition=BundleAcquisition.ALREADY_OWNED, need_index=0),
        needs=1,
    )
    before = room(state).bundle_items[0]

    result, _ = await run(refine(), state)

    after = room(result.state).bundle_items[0]
    assert (after.line_id, after.product_id, after.quantity, after.need_id) == (
        before.line_id,
        before.product_id,
        before.quantity,
        before.need_id,
    )
    assert after.acquisition is BundleAcquisition.ALREADY_OWNED


async def test_a_room_edit_advances_the_revision_once() -> None:
    state = room_with(line(10), line(10), needs=0)

    result, _ = await run(refine(), state)

    assert room(result.state).bundle_revision == room(state).bundle_revision + 1


async def test_locking_something_already_kept_changes_nothing() -> None:
    state = room_with(line(10, locked=True), needs=0)

    result, _ = await run(refine(), state)

    assert room(result.state).bundle_revision == room(state).bundle_revision


async def test_unlocking_releases_without_replacing() -> None:
    state = room_with(line(10, locked=True), needs=0)

    result, parts = await run(refine(BundleInteractionOp.UNLOCK), state)

    assert room(result.state).bundle_items[0].status is BundleItemStatus.SUGGESTED
    assert not parts["design"].requests
    assert not parts["design_discovery"].calls
    assert not parts["optimizer"].requests
    assert result.bundle_outcome is None


async def test_nothing_is_planned_searched_or_optimised(
) -> None:
    state = room_with(line(10), needs=0)

    _, parts = await run(refine(), state)

    assert parts["capabilities"].calls == []
    assert parts["design"].requests == []
    assert parts["design_discovery"].calls == []
    assert parts["optimizer"].requests == []
    assert parts["pipeline"].calls == []


async def test_a_missing_locked_product_stops_the_turn() -> None:
    state = room_with(line(10, locked=True), line(11), needs=0)

    result, _ = await run(refine(), state, available=(11,))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE
    assert room(result.state).bundle_items == room(state).bundle_items


async def test_an_unresolvable_reference_changes_nothing() -> None:
    state = room_with(line(10), needs=0)
    decision = CustomerAgentDecision(
        action=AgentAction.BUNDLE_REFINE,
        bundle_interaction=BundleInteractionIntent(
            op=BundleInteractionOp.LOCK, selector=BundleItemOrdinal(ordinal=9)
        ),
    )

    result, _ = await run(decision, state)

    assert result.grounding.deterministic_clarification is not None
    assert (
        result.grounding.deterministic_clarification.bundle_reason
        is BundleReferenceFailureReason.ORDINAL_OUT_OF_RANGE
    )
    assert room(result.state).bundle_items == room(state).bundle_items


# ── the reply ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("op", "kind"),
    [
        (BundleInteractionOp.LOCK, DeterministicResponseKind.BUNDLE_KEPT),
        (BundleInteractionOp.UNLOCK, DeterministicResponseKind.BUNDLE_UNLOCKED),
    ],
)
async def test_a_local_change_is_acknowledged_without_a_model(
    op: BundleInteractionOp, kind: DeterministicResponseKind
) -> None:
    state = room_with(line(10, locked=op is BundleInteractionOp.UNLOCK), needs=0)

    result, _ = await run(refine(op), state)
    route = route_response(result)

    assert isinstance(route.primary, DeterministicResponse)
    assert route.primary.kind is kind
    assert result.bundle_outcome is None, "no room was chosen, so none is invented"


def test_the_acknowledgements_state_no_fact_and_ask_nothing() -> None:
    from app.services.response_wording import (
        BUNDLE_KEPT_WORDING,
        BUNDLE_UNLOCKED_WORDING,
    )

    for wording in (BUNDLE_KEPT_WORDING, BUNDLE_UNLOCKED_WORDING):
        assert not any(character.isdigit() for character in wording)
        assert "?" not in wording


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


def test_the_view_counts_cards_as_well_as_lines() -> None:
    state = room_with(line(10), line(10), line(11), needs=0)

    view = project_state(state).room_project
    assert view is not None
    assert view.bundle_line_count == 3
    assert view.bundle_card_count == 2


@pytest.mark.parametrize("forbidden", ["product_id", "line_id", "need_id", "group"])
def test_the_view_still_names_nothing(forbidden: str) -> None:
    for field in RoomProjectView.model_fields:
        assert forbidden not in field, field


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
    from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
    from app.schemas.agent_turn import CustomerTurnResult, TurnGrounding
    from app.schemas.bundle import BundleLine, BundleStatus, RoomBundle
    from app.services.bundle_presentation import build_bundle_presentation

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
    result = CustomerTurnResult(
        state=state,
        decision=CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        grounding=TurnGrounding(design_handoff_requested=True),
        bundle_outcome=outcome,
    )
    return build_bundle_presentation(result), rebuilt(state, *products)


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


def test_an_infeasible_room_is_not_projected_from_current_state() -> None:
    """It was never committed, so state describes a different room."""
    from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
    from app.schemas.agent_turn import CustomerTurnResult, TurnGrounding
    from app.schemas.bundle import BundleLine, BundleStatus, RoomBundle
    from app.services.bundle_presentation import build_bundle_presentation

    state = room_with(line(99), needs=0)
    outcome = RoomBundle(
        lines=(
            BundleLine(
                product=product(10),
                quantity=1,
                locked=True,
                acquisition=BundleAcquisition.TO_BUY,
            ),
        ),
        status=BundleStatus.INFEASIBLE,
        new_spend_total=Decimal("1000.00"),
        currency="SAR",
    )
    rendered = build_bundle_presentation(
        CustomerTurnResult(
            state=state,
            decision=CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
            grounding=TurnGrounding(design_handoff_requested=True),
            bundle_outcome=outcome,
        )
    )

    assert rendered is not None
    assert [i.name_english for i in rendered.items] == ["Item 10"], "the outcome's own"


async def test_a_reference_resolved_against_another_room_is_refused() -> None:
    """The revision guard, exercised directly.

    No ordinary turn can currently produce it: nothing between resolving the
    reference and applying the edit touches the bundle. It is defensive for
    E4C, where a refinement will re-optimise the room mid-turn - and a guard
    with no test is a guard nobody will notice breaking.
    """
    pre_turn = room_with(line(10), line(11), needs=0)
    # The same room after something else edited it: a different revision.
    working = apply_update(
        pre_turn,
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                bundle_operations=(
                    __import__(
                        "app.schemas.agent_updates", fromlist=["SetBundleLineStatus"]
                    ).SetBundleLineStatus(line_id=2, status=BundleItemStatus.LOCKED),
                )
            )
        ),
    )
    assert room(working).bundle_revision != room(pre_turn).bundle_revision

    coordinator, _ = _coordinator(refine(), hydration=FakeHydration(available=(10, 11)))
    primary = await coordinator._bundle_refine(
        refine(),
        working,
        pre_turn,
        CustomerTurnInput(message="keep that one", state=pre_turn, context=CONTEXT),
    )

    assert primary.clarification is not None
    assert (
        primary.clarification.bundle_reason
        is BundleReferenceFailureReason.STALE_BUNDLE_REFERENCE
    )
    assert primary.bundle_change is None
    assert room(primary.state).bundle_items == room(working).bundle_items


def test_the_model_never_learns_a_revision() -> None:
    """Staleness is the application's business. And this protects one turn, not
    a bundle shown several turns ago - that needs a transport token nothing
    supplies yet."""
    from app.schemas.agent_view import AgentStateView

    assert "revision" not in str(RoomProjectView.model_fields)
    for field in BundleInteractionIntent.model_fields:
        assert "revision" not in field
    assert "bundle_revision" not in str(AgentStateView.model_fields)


# ── a stale card never moves the others ═════════════════════════════════════
#
# The bug this closes: hydration was allowed to decide what the visible list
# contained. A lamp the catalog could not return disappeared from the surface,
# every card after it moved up one, and "the third one" silently meant a
# different piece from the one the customer was looking at.
#
# Membership and order come from state. Hydration supplies facts and nothing
# else.


def _abc() -> AgentStateV1:
    """Three cards - sofa, lamp, rug - in that order."""
    return room_with(line(10), line(11), line(12), needs=0)


SOFA = product(10, category="seating", subcategory="sofa")
LAMP = product(11, category="lighting", subcategory="floor-lamp")
RUG = product(12, category="decor", subcategory="carpet")


def test_a_stale_middle_card_does_not_renumber_the_one_after_it() -> None:
    state = _abc()

    third = resolve(BundleItemOrdinal(ordinal=3), state, (SOFA, RUG))

    assert isinstance(third, ResolvedBundleReference)
    assert third.product_id == 12, "still the rug, never compacted to position two"
    assert third.ordinal == 3


def test_the_stale_card_itself_still_occupies_its_position() -> None:
    state = _abc()

    second = resolve(BundleItemOrdinal(ordinal=2), state, (SOFA, RUG))

    assert isinstance(second, BundleReferenceUnresolved)
    assert second.reason is BundleReferenceFailureReason.TARGET_PRODUCT_UNAVAILABLE


def test_an_unrelated_stale_card_does_not_block_a_readable_target() -> None:
    state = _abc()

    first = resolve(BundleItemOrdinal(ordinal=1), state, (SOFA, RUG))

    assert isinstance(first, ResolvedBundleReference)
    assert first.product_id == 10


def test_the_visible_length_is_the_state_length() -> None:
    """Three cards exist, so there is no fourth - however many hydrate."""
    state = _abc()

    beyond = resolve(BundleItemOrdinal(ordinal=4), state, (SOFA,))

    assert isinstance(beyond, BundleReferenceUnresolved)
    assert beyond.reason is BundleReferenceFailureReason.ORDINAL_OUT_OF_RANGE


def test_a_category_match_refuses_over_a_room_it_cannot_fully_read() -> None:
    """The unreadable card might have been another lamp."""
    state = _abc()

    outcome = resolve(
        BundleCategoryMatch(commerce_category="lighting", commerce_subcategory="floor-lamp"),
        state,
        (SOFA, RUG),
    )

    assert isinstance(outcome, BundleReferenceUnresolved)
    assert outcome.reason is BundleReferenceFailureReason.BUNDLE_NOT_VERIFIABLE


def test_a_category_match_does_not_treat_a_stale_card_as_a_non_match() -> None:
    """Even when the readable cards contain exactly one of the named kind."""
    state = _abc()

    outcome = resolve(
        BundleCategoryMatch(commerce_category="seating", commerce_subcategory="sofa"),
        state,
        (SOFA, RUG),
    )

    assert isinstance(outcome, BundleReferenceUnresolved)
    assert outcome.reason is not BundleReferenceFailureReason.NO_CATEGORY_MATCH


async def test_a_stale_target_is_reported_as_a_fact_not_a_question() -> None:
    """Asking again cannot make a product readable."""
    state = _abc()
    decision = CustomerAgentDecision(
        action=AgentAction.BUNDLE_REFINE,
        bundle_interaction=BundleInteractionIntent(
            op=BundleInteractionOp.LOCK, selector=BundleItemOrdinal(ordinal=2)
        ),
    )

    result, _ = await run(decision, state, available=(10, 12))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.PRODUCT_UNAVAILABLE
    assert result.grounding.deterministic_clarification is None
    assert room(result.state).bundle_items == room(state).bundle_items


async def test_an_unverifiable_room_says_so_rather_than_blaming_their_piece() -> None:
    state = _abc()
    decision = CustomerAgentDecision(
        action=AgentAction.BUNDLE_REFINE,
        bundle_interaction=BundleInteractionIntent(
            op=BundleInteractionOp.LOCK,
            selector=BundleCategoryMatch(
                commerce_category="seating", commerce_subcategory="sofa"
            ),
        ),
    )

    result, _ = await run(decision, state, available=(10, 12))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.BUNDLE_NOT_VERIFIABLE
    assert room(result.state).bundle_revision == room(state).bundle_revision


async def test_a_readable_target_commits_despite_an_unrelated_stale_card() -> None:
    """Resolution did not depend on the stale card, so the edit stands. The
    room simply cannot be rendered in full afterwards."""
    from app.core.exceptions import BundlePresentationError

    state = _abc()
    decision = CustomerAgentDecision(
        action=AgentAction.BUNDLE_REFINE,
        bundle_interaction=BundleInteractionIntent(
            op=BundleInteractionOp.LOCK, selector=BundleItemOrdinal(ordinal=1)
        ),
    )

    result, _ = await run(decision, state, available=(10, 12))

    after = room(result.state)
    assert after.bundle_items[0].status is BundleItemStatus.LOCKED
    assert after.bundle_revision == room(state).bundle_revision + 1
    assert result.grounding.failure is None
    with pytest.raises(BundlePresentationError):
        rebuilt(result.state, SOFA, RUG)


async def test_a_stale_locked_piece_still_stops_the_turn_outright() -> None:
    """The stronger rule is unchanged: a lock cannot be dropped or unlocked."""
    state = room_with(line(10, locked=True), line(11), needs=0)

    result, _ = await run(refine(), state, available=(11,))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE
    assert room(result.state).bundle_items[0].status is BundleItemStatus.LOCKED


async def test_none_of_this_reaches_a_search_or_a_model() -> None:
    state = _abc()

    _, parts = await run(refine(), state, available=(10, 12))

    assert parts["design"].requests == []
    assert parts["design_discovery"].calls == []
    assert parts["optimizer"].requests == []
    assert parts["pipeline"].calls == []
