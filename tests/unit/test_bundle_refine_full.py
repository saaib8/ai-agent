"""Refining a room the customer already has.

Three themes run through this.

**Nothing is claimed that was not chosen.** A rejection, a new phrase and the
room they produce commit together or not at all - otherwise state would say the
current sofa was picked for a reason it never satisfied.

**A product change is a whole-room change.** A cheaper sofa may afford a better
rug and a dearer one may push an optional piece out, so every role is
discovered afresh and the whole room optimised again.

**A fact the customer stated outlives a search that failed.** "I already own
that" is true whatever happens next, and discarding it because a catalog was
unreachable would answer a statement with an unrelated error.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.core.exceptions import CatalogUnavailableError
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    BundleInteractionIntent,
    BundleInteractionOp,
    BundleReplacementIntent,
    BundleReplacementMode,
    CustomerAgentDecision,
)
from app.schemas.agent_state import AgentStateV1, BundleItemStatus, RoomProjectState
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.agent_updates import (
    AgentStateUpdate,
    DesignNeedSpec,
    PlannedBundleLineSpec,
    ReplaceDesignPlan,
    RoomProjectUpdate,
)
from app.schemas.bundle import (
    BundleLine,
    BundleStatus,
    RoomBundle,
    UnmetNeed,
    UnmetReason,
)
from app.schemas.bundle_reference import BundleItemOrdinal, DesignNeedCategoryMatch
from app.schemas.design import DesignPriority
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import MAX_EXCLUDED_PRODUCT_IDS, PriceConstraint
from app.schemas.grounding import TurnFailureCode
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.refinement import SemanticIntentOp, SemanticIntentRefinement
from app.schemas.resolution import DesignNeedFailureReason
from app.schemas.response import DeterministicResponse, DeterministicResponseKind
from app.schemas.retailer import RetailerContext
from app.services.agent_state import apply_update
from app.services.response_view import route_response
from pydantic import ValidationError

from tests.unit.test_turn_coordinator import (
    FakeDesignDiscovery,
    FakeHydration,
    FakeOptimizer,
    _coordinator,
)

CONTEXT = RetailerContext(store_id=50)


def product(
    product_id: int,
    *,
    price: str = "1000.00",
    unit: str = "SAR",
    category: str = "seating",
    subcategory: str | None = "sofa",
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


def need(
    category: str = "seating",
    subcategory: str | None = "sofa",
    *,
    intent: str | None = None,
) -> DesignNeedSpec:
    return DesignNeedSpec(
        commerce_category=category,
        commerce_subcategory=subcategory,
        priority=DesignPriority.REQUIRED,
        quantity=1,
        semantic_intent=intent,
    )


def line(
    product_id: int, *, need_index: int | None = 0, locked: bool = False,
    acquisition: BundleAcquisition = BundleAcquisition.TO_BUY,
) -> PlannedBundleLineSpec:
    return PlannedBundleLineSpec(
        product_id=product_id,
        quantity=1,
        acquisition=acquisition,
        status=BundleItemStatus.LOCKED if locked else BundleItemStatus.SUGGESTED,
        need_index=need_index,
    )


def a_room(
    *, needs: tuple[DesignNeedSpec, ...] = (), lines: tuple[PlannedBundleLineSpec, ...] = (),
    budget: str | None = None,
) -> AgentStateV1:
    return apply_update(
        AgentStateV1(),
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                budget=PriceConstraint.at_most(Decimal(budget), "SAR") if budget else None,
                bundle_operations=(ReplaceDesignPlan(needs=needs, added=lines),),
            )
        ),
    )


def room(state: AgentStateV1) -> RoomProjectState:
    project = state.room_project
    assert isinstance(project, RoomProjectState)
    return project


def chosen(product_id: int, *, need_index: int = 0) -> RoomBundle:
    return RoomBundle(
        lines=(
            BundleLine(
                need_index=need_index,
                product=product(product_id),
                quantity=1,
                locked=False,
                acquisition=BundleAcquisition.TO_BUY,
                relaxation_depth=0,
            ),
        ),
        status=BundleStatus.COMPLETE,
        new_spend_total=Decimal("1000.00"),
        currency="SAR",
    )


def intent(op: BundleInteractionOp, **kwargs: Any) -> CustomerAgentDecision:
    kwargs.setdefault("selector", BundleItemOrdinal(ordinal=1))
    return CustomerAgentDecision(
        action=AgentAction.BUNDLE_REFINE,
        bundle_interaction=BundleInteractionIntent(op=op, **kwargs),
    )


def replace_with(
    mode: BundleReplacementMode, wording: SemanticIntentRefinement | None = None
) -> CustomerAgentDecision:
    return intent(
        BundleInteractionOp.REPLACE_PRODUCT,
        replacement=BundleReplacementIntent(mode=mode, semantic_intent=wording),
    )


async def run(
    decision: CustomerAgentDecision,
    state: AgentStateV1,
    *,
    available: tuple[int, ...] | None = None,
    outcome: Any = None,
    discovery_error: Exception | None = None,
) -> Any:
    current = tuple(i.product_id for i in room(state).bundle_items)
    ids = available if available is not None else (*current, 77)
    coordinator, parts = _coordinator(
        decision,
        hydration=FakeHydration(available=ids),
        optimizer=FakeOptimizer(outcome) if outcome is not None else None,
        design_discovery=FakeDesignDiscovery(error=discovery_error)
        if discovery_error
        else None,
    )
    result = await coordinator.run(
        CustomerTurnInput(message="change that", state=state, context=CONTEXT)
    )
    return result, parts


def one_sofa(**kwargs: Any) -> AgentStateV1:
    """A room with a single sofa filling a single role."""
    return a_room(needs=(need(),), lines=(line(10),), **kwargs)


# ── the contract ════════════════════════════════════════════════════════════


def test_the_five_operations_are_exactly_what_is_implemented() -> None:
    assert {op.value for op in BundleInteractionOp} == {
        "lock",
        "unlock",
        "set_acquisition",
        "replace_product",
        "remove_need",
    }


def test_the_model_speaks_of_intent_not_of_persistence() -> None:
    """LOCK and UNLOCK are what the customer means; status is how we store it.

    The reducer keeps a generic `SetBundleLineStatus`, but that vocabulary is
    the application's. A model emitting a status would be authoring the shape
    of our state rather than reporting what the customer asked for, and a later
    change to how locking is stored would become a change to the provider
    schema (CLAUDE.md 3.3).
    """
    values = {op.value for op in BundleInteractionOp}
    assert "lock" in values and "unlock" in values
    assert not {value for value in values if "status" in value}


@pytest.mark.parametrize(
    "op",
    [
        BundleInteractionOp.LOCK,
        BundleInteractionOp.UNLOCK,
        BundleInteractionOp.SET_ACQUISITION,
        BundleInteractionOp.REPLACE_PRODUCT,
    ],
)
def test_only_a_removal_may_name_a_role_directly(op: BundleInteractionOp) -> None:
    """Every other operation acts on a product, which a role need not have."""
    extra: dict[str, Any] = {}
    if op is BundleInteractionOp.SET_ACQUISITION:
        extra["acquisition"] = BundleAcquisition.TO_BUY
    if op is BundleInteractionOp.REPLACE_PRODUCT:
        extra["replacement"] = BundleReplacementIntent(
            mode=BundleReplacementMode.ALTERNATIVE
        )
    with pytest.raises(ValidationError):
        BundleInteractionIntent(
            op=op,
            selector=BundleItemOrdinal(ordinal=1),
            need_selector=DesignNeedCategoryMatch(commerce_category="lighting"),
            **extra,
        )


def test_only_a_removal_may_omit_the_card() -> None:
    for op in (
        BundleInteractionOp.LOCK,
        BundleInteractionOp.UNLOCK,
    ):
        with pytest.raises(ValidationError):
            BundleInteractionIntent(op=op, selector=None)


def test_only_a_semantic_replacement_carries_wording() -> None:
    with pytest.raises(ValidationError):
        BundleReplacementIntent(mode=BundleReplacementMode.SEMANTIC)
    with pytest.raises(ValidationError):
        BundleReplacementIntent(
            mode=BundleReplacementMode.CHEAPER,
            semantic_intent=SemanticIntentRefinement(
                op=SemanticIntentOp.SET, value="lighter"
            ),
        )


def test_there_is_no_premium_or_better_mode() -> None:
    """Price is not quality, so there is no mode that could conflate them."""
    values = {mode.value for mode in BundleReplacementMode}
    assert values == {"alternative", "cheaper", "more_expensive", "semantic"}


def test_each_operation_carries_only_what_it_acts_on() -> None:
    with pytest.raises(ValidationError):
        BundleInteractionIntent(
            op=BundleInteractionOp.LOCK,
            selector=BundleItemOrdinal(ordinal=1),
            acquisition=BundleAcquisition.TO_BUY,
        )
    with pytest.raises(ValidationError):
        BundleInteractionIntent(
            op=BundleInteractionOp.SET_ACQUISITION,
            selector=BundleItemOrdinal(ordinal=1),
        )
    with pytest.raises(ValidationError):
        BundleInteractionIntent(op=BundleInteractionOp.REPLACE_PRODUCT, selector=None)


def test_a_removal_names_a_card_or_a_role_and_one_of_them() -> None:
    assert BundleInteractionIntent(
        op=BundleInteractionOp.REMOVE_NEED,
        need_selector=DesignNeedCategoryMatch(commerce_category="lighting"),
    )
    with pytest.raises(ValidationError):
        BundleInteractionIntent(
            op=BundleInteractionOp.REMOVE_NEED,
            selector=BundleItemOrdinal(ordinal=1),
            need_selector=DesignNeedCategoryMatch(commerce_category="lighting"),
        )


@pytest.mark.parametrize(
    "forbidden", ["product_id", "line_id", "need_id", "revision", "price", "amount"]
)
def test_the_model_names_no_identity_and_no_figure(forbidden: str) -> None:
    for model in (BundleInteractionIntent, BundleReplacementIntent, DesignNeedCategoryMatch):
        for field in model.model_fields:
            assert forbidden not in field, f"{model.__name__}.{field}"


# ── replacement ═════════════════════════════════════════════════════════════


async def test_an_alternative_excludes_the_current_product_and_nothing_else() -> None:
    state = one_sofa()

    _, parts = await run(
        replace_with(BundleReplacementMode.ALTERNATIVE), state, outcome=chosen(77)
    )

    applied = next(iter(parts["design_discovery"].overrides.values()))

    assert applied.exclude_product_ids == (10,)
    assert applied.price is None
    assert applied.semantic_intent is None


async def test_cheaper_bounds_strictly_below_the_current_price() -> None:
    state = one_sofa()

    _, parts = await run(
        replace_with(BundleReplacementMode.CHEAPER), state, outcome=chosen(77)
    )

    price = next(iter(parts["design_discovery"].overrides.values())).price
    assert price is not None
    assert price.max_amount == Decimal("1000.00")
    assert price.max_exclusive is True
    assert price.min_amount is None
    assert price.currency == "SAR"


async def test_more_expensive_bounds_strictly_above_the_current_price() -> None:
    state = one_sofa()

    _, parts = await run(
        replace_with(BundleReplacementMode.MORE_EXPENSIVE), state, outcome=chosen(77)
    )

    price = next(iter(parts["design_discovery"].overrides.values())).price
    assert price is not None
    assert price.min_amount == Decimal("1000.00")
    assert price.min_exclusive is True
    assert price.max_amount is None


async def test_a_semantic_replacement_stages_the_new_wording() -> None:
    state = a_room(needs=(need(intent="visually light"),), lines=(line(10),))

    result, parts = await run(
        replace_with(
            BundleReplacementMode.SEMANTIC,
            SemanticIntentRefinement(op=SemanticIntentOp.SET, value="more minimal"),
        ),
        state,
        outcome=chosen(77),
    )

    staged = next(iter(parts["design_discovery"].overrides.values()))
    assert staged.semantic_intent is not None
    assert staged.semantic_intent.value == "more minimal"
    assert room(result.state).design_needs[0].semantic_intent == "more minimal"


async def test_new_wording_replaces_the_old_rather_than_joining_it() -> None:
    """Three generations of fuzzy language would describe a thing nobody asked
    for."""
    state = a_room(needs=(need(intent="visually light reading chair"),), lines=(line(10),))

    result, _ = await run(
        replace_with(
            BundleReplacementMode.SEMANTIC,
            SemanticIntentRefinement(op=SemanticIntentOp.SET, value="more minimal"),
        ),
        state,
        outcome=chosen(77),
    )

    assert room(result.state).design_needs[0].semantic_intent == "more minimal"
    assert "visually light" not in (room(result.state).design_needs[0].semantic_intent or "")


async def test_clearing_the_wording_leaves_the_rest_of_the_role_alone() -> None:
    state = a_room(needs=(need(intent="visually light"),), lines=(line(10),))

    result, _ = await run(
        replace_with(
            BundleReplacementMode.SEMANTIC,
            SemanticIntentRefinement(op=SemanticIntentOp.CLEAR),
        ),
        state,
        outcome=chosen(77),
    )

    after = room(result.state).design_needs[0]
    assert after.semantic_intent is None
    assert after.commerce_subcategory == "sofa"
    assert after.priority is DesignPriority.REQUIRED


async def test_a_successful_replacement_persists_the_rejection() -> None:
    state = one_sofa()

    result, _ = await run(
        replace_with(BundleReplacementMode.ALTERNATIVE), state, outcome=chosen(77)
    )

    after = room(result.state)
    assert after.design_needs[0].rejected_product_ids == (10,)
    assert [i.product_id for i in after.bundle_items] == [77]
    assert after.bundle_items[0].status is BundleItemStatus.SUGGESTED
    assert after.design_needs[0].need_id == room(state).design_needs[0].need_id


async def test_a_prior_rejection_survives_a_later_one() -> None:
    first, _ = await run(
        replace_with(BundleReplacementMode.ALTERNATIVE), one_sofa(), outcome=chosen(77)
    )
    state = first.state


    result, _ = await run(
        replace_with(BundleReplacementMode.ALTERNATIVE),
        state,
        available=(77, 88),
        outcome=chosen(88),
    )

    assert room(result.state).design_needs[0].rejected_product_ids == (10, 77)


# ── rollback ════════════════════════════════════════════════════════════════


async def test_nothing_is_staged_when_no_replacement_is_found() -> None:
    """Their current sofa is untouched, and so is everything about its role."""
    state = a_room(needs=(need(intent="visually light"),), lines=(line(10),))
    before = room(state)
    empty = RoomBundle(
        lines=(),
        status=BundleStatus.PARTIAL,
        unmet=(
            UnmetNeed(
                need_index=0,
                priority=DesignPriority.REQUIRED,
                shortfall=1,
                reason=UnmetReason.NO_CANDIDATES,
            ),
        ),
        new_spend_total=None,
        currency=None,
        total_unavailable=__import__(
            "app.schemas.bundle", fromlist=["TotalUnavailableReason"]
        ).TotalUnavailableReason.NO_PRICED_LINES,
    )

    result, _ = await run(
        replace_with(
            BundleReplacementMode.SEMANTIC,
            SemanticIntentRefinement(op=SemanticIntentOp.SET, value="more minimal"),
        ),
        state,
        outcome=empty,
    )

    after = room(result.state)
    assert [i.product_id for i in after.bundle_items] == [10], "their sofa stayed"
    assert after.design_needs[0].semantic_intent == "visually light"
    assert after.design_needs[0].rejected_product_ids == ()
    assert after.bundle_revision == before.bundle_revision
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.NO_REPLACEMENT_CANDIDATE


async def test_a_budget_that_will_not_stretch_is_not_an_empty_catalog() -> None:
    state = one_sofa()
    unaffordable = RoomBundle(
        lines=(),
        status=BundleStatus.PARTIAL,
        unmet=(
            UnmetNeed(
                need_index=0,
                priority=DesignPriority.REQUIRED,
                shortfall=1,
                reason=UnmetReason.BUDGET_EXHAUSTED,
            ),
        ),
        new_spend_total=None,
        currency=None,
        total_unavailable=__import__(
            "app.schemas.bundle", fromlist=["TotalUnavailableReason"]
        ).TotalUnavailableReason.NO_PRICED_LINES,
    )

    result, _ = await run(
        replace_with(BundleReplacementMode.CHEAPER), state, outcome=unaffordable
    )

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.REPLACEMENT_NOT_FEASIBLE


async def test_a_replacement_that_returns_the_same_product_is_not_one() -> None:
    state = one_sofa()

    result, _ = await run(
        replace_with(BundleReplacementMode.ALTERNATIVE), state, outcome=chosen(10)
    )

    assert result.grounding.failure is not None
    assert [i.product_id for i in room(result.state).bundle_items] == [10]


async def test_an_unusable_current_price_cannot_bound_a_search() -> None:
    state = a_room(needs=(need(),), lines=(line(10),))
    coordinator, _ = _coordinator(
        replace_with(BundleReplacementMode.CHEAPER),
        hydration=FakeHydration(available=(10,), price="0.00"),
    )

    result = await coordinator.run(
        CustomerTurnInput(message="cheaper please", state=state, context=CONTEXT)
    )

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.PRODUCT_UNAVAILABLE


# ── acquisition ═════════════════════════════════════════════════════════════


async def test_ownership_without_a_budget_changes_nothing_else() -> None:
    state = one_sofa()

    result, parts = await run(
        intent(
            BundleInteractionOp.SET_ACQUISITION,
            acquisition=BundleAcquisition.ALREADY_OWNED,
        ),
        state,
    )

    after = room(result.state)
    assert after.bundle_items[0].acquisition is BundleAcquisition.ALREADY_OWNED
    assert after.bundle_items[0].product_id == 10
    assert after.bundle_revision == room(state).bundle_revision + 1
    assert parts["design_discovery"].calls == []
    assert parts["optimizer"].requests == []


async def test_ownership_with_a_budget_re_costs_the_room() -> None:
    state = one_sofa(budget="15000")

    result, parts = await run(
        intent(
            BundleInteractionOp.SET_ACQUISITION,
            acquisition=BundleAcquisition.ALREADY_OWNED,
        ),
        state,
        outcome=chosen(77),
    )

    assert parts["optimizer"].requests, "a budget makes ownership change the sums"
    assert result.bundle_outcome is not None


async def test_ownership_survives_a_refresh_that_failed() -> None:
    """They told us something true. An unreachable catalog does not undo it."""
    state = one_sofa(budget="15000")

    result, _ = await run(
        intent(
            BundleInteractionOp.SET_ACQUISITION,
            acquisition=BundleAcquisition.ALREADY_OWNED,
        ),
        state,
        discovery_error=CatalogUnavailableError(),
    )

    assert room(result.state).bundle_items[0].acquisition is BundleAcquisition.ALREADY_OWNED
    assert result.grounding.failure is not None


async def test_ownership_is_reversible() -> None:
    state = a_room(
        needs=(need(),),
        lines=(line(10, acquisition=BundleAcquisition.ALREADY_OWNED),),
    )

    result, _ = await run(
        intent(
            BundleInteractionOp.SET_ACQUISITION, acquisition=BundleAcquisition.TO_BUY
        ),
        state,
    )

    assert room(result.state).bundle_items[0].acquisition is BundleAcquisition.TO_BUY


# ── keeping and releasing ═══════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        (BundleInteractionOp.LOCK, BundleItemStatus.LOCKED),
        (BundleInteractionOp.UNLOCK, BundleItemStatus.SUGGESTED),
    ],
)
async def test_the_application_turns_intent_into_status(
    op: BundleInteractionOp, expected: BundleItemStatus
) -> None:
    """The one place the customer's word becomes our stored vocabulary.

    Group-wide, and with no search behind it: keeping a piece says what may not
    change later, which is not a request to choose anything now.
    """
    state = a_room(
        needs=(need(),),
        lines=(
            line(10, locked=op is BundleInteractionOp.UNLOCK),
            line(10, locked=op is BundleInteractionOp.UNLOCK),
        ),
    )

    result, parts = await run(intent(op), state)

    assert [i.status for i in room(result.state).bundle_items] == [expected, expected]
    assert parts["design_discovery"].calls == []


# ── removing a role ═════════════════════════════════════════════════════════


async def test_removing_a_role_takes_its_products_with_it() -> None:
    state = a_room(
        needs=(need(), need("lighting", "floor-lamp")),
        lines=(line(10, need_index=0), line(11, need_index=1)),
    )

    result, _ = await run(
        intent(
            BundleInteractionOp.REMOVE_NEED,
            selector=None,
            need_selector=DesignNeedCategoryMatch(
                commerce_category="lighting", commerce_subcategory="floor-lamp"
            ),
        ),
        state,
        outcome=chosen(10),
    )

    after = room(result.state)
    assert [n.commerce_category for n in after.design_needs] == ["seating"]
    assert 11 not in [i.product_id for i in after.bundle_items]


async def test_a_visible_card_names_the_role_it_fills() -> None:
    """Removal by card: "take that one out" while looking at it.

    The card is resolved to the durable role behind it, and the role is what
    goes - so the product cannot come back under the same heading next turn.
    """
    # The named card fills the SECOND role, so removing "the first need" or
    # "the card's own position" would take the wrong one out.
    state = a_room(
        needs=(need("lighting", "floor-lamp"), need()),
        lines=(line(10, need_index=1), line(11, need_index=0)),
    )

    result, _ = await run(
        intent(BundleInteractionOp.REMOVE_NEED, selector=BundleItemOrdinal(ordinal=1)),
        state,
        outcome=chosen(11, need_index=1),
    )

    after = room(result.state)
    assert [n.commerce_category for n in after.design_needs] == ["lighting"]
    assert 10 not in [i.product_id for i in after.bundle_items]


async def test_a_card_filling_two_roles_is_not_guessed_between() -> None:
    """One product can fill two roles, and removing "it" is then two answers.

    Product identity cannot settle which role they meant, so the question is
    asked rather than halved.
    """
    state = a_room(
        needs=(need(), need()),
        lines=(line(10, need_index=0), line(10, need_index=1)),
    )

    result, _ = await run(
        intent(BundleInteractionOp.REMOVE_NEED, selector=BundleItemOrdinal(ordinal=1)),
        state,
        available=(10,),
    )

    assert (
        result.grounding.deterministic_clarification.need_reason
        is DesignNeedFailureReason.CARD_SPANS_SEVERAL_NEEDS
    )
    assert len(room(result.state).design_needs) == 2


async def test_a_role_the_plan_does_not_hold_is_asked_about() -> None:
    """An approved kind of thing that is simply not in this plan.

    Nothing is removed on a near miss: the plan has no table, and quietly
    taking out the nearest thing would be a different room.
    """
    state = a_room(needs=(need(),), lines=(line(10),))

    result, _ = await run(
        intent(
            BundleInteractionOp.REMOVE_NEED,
            selector=None,
            need_selector=DesignNeedCategoryMatch(commerce_category="tables"),
        ),
        state,
        available=(10,),
    )

    assert (
        result.grounding.deterministic_clarification.need_reason
        is DesignNeedFailureReason.NO_NEED_MATCH
    )
    assert len(room(result.state).design_needs) == 1


async def test_a_role_with_nothing_in_it_can_still_be_removed() -> None:
    state = a_room(needs=(need(), need("lighting", "floor-lamp")), lines=(line(10),))

    result, _ = await run(
        intent(
            BundleInteractionOp.REMOVE_NEED,
            selector=None,
            need_selector=DesignNeedCategoryMatch(commerce_category="lighting"),
        ),
        state,
        outcome=chosen(10),
    )

    assert [n.commerce_category for n in room(result.state).design_needs] == ["seating"]


async def test_removing_a_role_supersedes_keeping_the_piece_in_it() -> None:
    state = a_room(
        needs=(need(), need("lighting", "floor-lamp")),
        lines=(line(10, need_index=0), line(11, need_index=1, locked=True)),
    )

    result, _ = await run(
        intent(
            BundleInteractionOp.REMOVE_NEED,
            selector=None,
            need_selector=DesignNeedCategoryMatch(commerce_category="lighting"),
        ),
        state,
        available=(10, 11, 77),
        outcome=chosen(10),
    )

    assert 11 not in [i.product_id for i in room(result.state).bundle_items]


async def test_a_removal_survives_a_refresh_that_failed() -> None:
    state = a_room(
        needs=(need(), need("lighting", "floor-lamp")),
        lines=(line(10, need_index=0),),
    )

    result, _ = await run(
        intent(
            BundleInteractionOp.REMOVE_NEED,
            selector=None,
            need_selector=DesignNeedCategoryMatch(commerce_category="lighting"),
        ),
        state,
        discovery_error=CatalogUnavailableError(),
    )

    assert [n.commerce_category for n in room(result.state).design_needs] == ["seating"]


async def test_two_roles_of_one_kind_are_not_guessed_between() -> None:
    state = a_room(
        needs=(need("lighting", "floor-lamp"), need("lighting", "floor-lamp")),
        lines=(),
    )

    result, _ = await run(
        intent(
            BundleInteractionOp.REMOVE_NEED,
            selector=None,
            need_selector=DesignNeedCategoryMatch(commerce_category="lighting"),
        ),
        state,
        available=(),
    )

    assert result.grounding.deterministic_clarification is not None
    assert (
        result.grounding.deterministic_clarification.need_reason
        is DesignNeedFailureReason.SEVERAL_NEED_MATCHES
    )
    assert len(room(result.state).design_needs) == 2


# ── whole-room discipline ═══════════════════════════════════════════════════


async def test_every_role_is_discovered_afresh() -> None:
    state = a_room(
        needs=(need(), need("lighting", "floor-lamp"), need("decor", "carpet")),
        lines=(line(10, need_index=0),),
    )

    _, parts = await run(
        replace_with(BundleReplacementMode.ALTERNATIVE), state, outcome=chosen(77)
    )

    assert len(parts["design_discovery"].calls) == 1
    plan = parts["design_discovery"].calls[0][1]
    assert len(plan.needs) == 3, "the whole plan, not just the changed role"


async def test_no_specialist_is_consulted() -> None:
    state = one_sofa()

    _, parts = await run(
        replace_with(BundleReplacementMode.ALTERNATIVE), state, outcome=chosen(77)
    )

    assert parts["design"].requests == []


async def test_an_unrelated_lock_stays_hard_through_a_replacement() -> None:
    state = a_room(
        needs=(need(), need("lighting", "floor-lamp")),
        lines=(line(10, need_index=0), line(11, need_index=1, locked=True)),
    )

    _, parts = await run(
        replace_with(BundleReplacementMode.ALTERNATIVE),
        state,
        available=(10, 11, 77),
        outcome=chosen(77),
    )

    locked = parts["optimizer"].requests[0].locked
    assert [lock.product.product_id for lock in locked] == [11]


async def test_the_target_lock_is_released_for_its_own_replacement() -> None:
    """Asking for another one is permission to move that one."""
    state = a_room(needs=(need(),), lines=(line(10, locked=True),))

    _, parts = await run(
        replace_with(BundleReplacementMode.ALTERNATIVE),
        state,
        available=(10, 77),
        outcome=chosen(77),
    )

    assert parts["optimizer"].requests[0].locked == ()


# ── the reply ═══════════════════════════════════════════════════════════════


async def test_a_refined_room_reuses_the_room_bundle_route() -> None:
    state = one_sofa()

    result, _ = await run(
        replace_with(BundleReplacementMode.ALTERNATIVE), state, outcome=chosen(77)
    )
    route = route_response(result)

    from app.schemas.response import ResponseGroundingView, ResponseOutcomeKind

    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.ROOM_BUNDLE


async def test_a_change_whose_refresh_failed_says_both_things() -> None:
    state = one_sofa(budget="15000")

    result, _ = await run(
        intent(
            BundleInteractionOp.SET_ACQUISITION,
            acquisition=BundleAcquisition.ALREADY_OWNED,
        ),
        state,
        discovery_error=CatalogUnavailableError(),
    )
    route = route_response(result)

    assert isinstance(route.primary, DeterministicResponse)
    assert route.primary.kind is DeterministicResponseKind.BUNDLE_CHANGED_NOT_REFRESHED


def test_the_dual_truth_wording_states_the_change_as_done() -> None:
    from app.services.response_wording import BUNDLE_CHANGED_NOT_REFRESHED_WORDING

    wording = BUNDLE_CHANGED_NOT_REFRESHED_WORDING.lower()
    assert "i've made that change" in wording
    assert "couldn't make" not in wording
    assert not any(character.isdigit() for character in wording)


@pytest.mark.parametrize(
    "code",
    [
        TurnFailureCode.NO_REPLACEMENT_CANDIDATE,
        TurnFailureCode.REPLACEMENT_NOT_FEASIBLE,
    ],
)
def test_replacement_failures_say_the_current_choice_stands(code: TurnFailureCode) -> None:
    from app.services.response_wording import FAILURE_WORDING

    wording = FAILURE_WORDING[code].lower()
    assert "left your current choice" in wording
    assert not any(character.isdigit() for character in wording)


# ── bounds ══════════════════════════════════════════════════════════════════


async def test_rejections_stay_within_the_shared_ceiling() -> None:
    from app.services.turn_coordinator import _with_rejection

    full = tuple(range(1, MAX_EXCLUDED_PRODUCT_IDS + 1))
    state = one_sofa()
    project = room(state)
    stuffed = project.model_copy(
        update={
            "design_needs": (
                project.design_needs[0].model_copy(
                    update={"rejected_product_ids": full}
                ),
            )
        }
    )

    merged = _with_rejection(stuffed, project.design_needs[0].need_id, 999)

    assert len(merged) == MAX_EXCLUDED_PRODUCT_IDS
    assert merged[-1] == 999


def test_rejections_stay_scoped_to_their_own_role() -> None:
    """A sofa turned down for one role says nothing about another."""
    from app.services.turn_coordinator import _with_rejection

    state = a_room(
        needs=(need(), need("lighting", "floor-lamp")), lines=(line(10, need_index=0),)
    )
    project = room(state)
    second = project.design_needs[1].need_id

    assert _with_rejection(project, second, 10) == (10,)
    assert project.design_needs[0].rejected_product_ids == ()
