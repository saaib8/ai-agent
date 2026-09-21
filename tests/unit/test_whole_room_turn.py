"""The whole-room turn, end to end without a database or a provider.

The rule the ordering exists to protect: a customer who says "design my living
room under SAR 15,000" has stated a budget, and that budget is theirs whatever
happens to the design afterwards. So the facts they gave land first and
unconditionally, the piece they asked to keep is verified and recorded second,
and only then is anything executed. A provider failure three steps later takes
none of it with it.

The other half is what execution may and may not write. A real room replaces
the bundle; an infeasible one and a refusal leave it exactly as it was, because
replacing a bundle the customer can buy with one they cannot loses something
for nothing.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.core.exceptions import CatalogUnavailableError, LLMUnavailableError
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    CustomerStateProposal,
    DesignAnchorIntent,
    PriceProposal,
)
from app.schemas.agent_state import (
    AgentStateV1,
    BundleItemStatus,
    ProductInteractionState,
    RoomProjectState,
)
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.agent_updates import (
    AgentStateUpdate,
    BundleLineSpec,
    ReplaceBundle,
    RoomProjectUpdate,
)
from app.schemas.bundle import (
    BundleLine,
    BundleStatus,
    BundleUnavailable,
    BundleUnavailableReason,
    RoomBundle,
    TotalUnavailableReason,
    UnmetNeed,
    UnmetReason,
)
from app.schemas.design import (
    DesignCategoryNeed,
    DesignPriority,
    InteriorDesignResult,
)
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import PriceConstraint
from app.schemas.grounding import TurnFailureCode
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.resolution import ResolvedProductReference
from app.schemas.retailer import RetailerContext
from app.services.agent_state import apply_update

from tests.unit.test_turn_coordinator import (
    FakeCapabilities,
    FakeDesign,
    FakeDesignDiscovery,
    FakeHydration,
    FakeOptimizer,
    FakeReferences,
    _coordinator,
    _resolved,
)

CONTEXT = RetailerContext(store_id=50)
ANCHOR_ID = 10


def product(product_id: int = ANCHOR_ID, *, price: str = "4000.00") -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Sofa {product_id}",
        name_arabic="أريكة",
        price_amount=Decimal(price),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


def bundle(
    *lines: BundleLine, status: BundleStatus = BundleStatus.COMPLETE
) -> RoomBundle:
    return RoomBundle(
        lines=lines,
        status=status,
        new_spend_total=Decimal("4000.00") if lines else None,
        currency="SAR" if lines else None,
        total_unavailable=None if lines else TotalUnavailableReason.NO_PRICED_LINES,
    )


def plan_with(count: int = 1) -> InteriorDesignResult:
    """A plan whose positions the fake optimizer's lines can point at."""
    return InteriorDesignResult(
        needs=tuple(
            DesignCategoryNeed(
                commerce_category="seating",
                commerce_subcategory="sofa",
                priority=DesignPriority.REQUIRED,
            )
            for _ in range(count)
        )
    )


def selected(product_id: int, *, need_index: int = 0) -> BundleLine:
    # `BundleLine.need_index` is execution-local and unchanged by M12E-4A: it
    # is a position in the plan being committed, not a durable id.
    return BundleLine(
        need_index=need_index,
        product=product(product_id),
        quantity=1,
        locked=False,
        acquisition=BundleAcquisition.TO_BUY,
        relaxation_depth=0,
    )


def handoff(anchor: DesignAnchorIntent | None = None, **kwargs: Any) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF, design_anchor=anchor, **kwargs
    )


def state_with(*specs: BundleLineSpec, presented: tuple[int, ...] = (ANCHOR_ID,)) -> AgentStateV1:
    base = AgentStateV1(
        product_interaction=ProductInteractionState(presented_product_ids=presented)
    )
    if not specs:
        return base
    return apply_update(
        base,
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                bundle_operations=(ReplaceBundle(added=specs),)
            )
        ),
    )


async def run(
    decision: CustomerAgentDecision,
    *,
    state: AgentStateV1 | None = None,
    message: str = "design my living room",
    **parts: Any,
) -> Any:
    coordinator, built = _coordinator(decision, **parts)
    result = await coordinator.run(
        CustomerTurnInput(
            message=message, state=state or state_with(), context=CONTEXT
        )
    )
    return result, built


def room(result: Any) -> RoomProjectState:
    project = result.state.room_project
    assert isinstance(project, RoomProjectState)
    return project


# ── the anchor ══════════════════════════════════════════════════════════════


async def test_a_room_can_be_planned_with_no_anchor_at_all() -> None:
    result, parts = await run(handoff())

    assert result.grounding.design_handoff_requested is True
    assert parts["design"].requests
    assert result.bundle_outcome is not None


async def test_an_anchor_becomes_a_locked_line_the_application_resolved() -> None:
    result, _ = await run(
        handoff(DesignAnchorIntent(reference=PresentedOrdinal(position=1))),
        references=FakeReferences(default=ResolvedProductReference(product_id=ANCHOR_ID)),
        hydration=FakeHydration(available=(ANCHOR_ID,)),
    )

    line = room(result).bundle_items[0]
    assert line.product_id == ANCHOR_ID
    assert line.status is BundleItemStatus.LOCKED
    assert line.quantity == 1


async def test_an_unstated_acquisition_defaults_to_being_bought() -> None:
    """The conservative direction: assuming they own it would understate what
    the room costs."""
    result, _ = await run(
        handoff(DesignAnchorIntent(reference=PresentedOrdinal(position=1))),
        references=FakeReferences(default=ResolvedProductReference(product_id=ANCHOR_ID)),
        hydration=FakeHydration(available=(ANCHOR_ID,)),
    )

    assert room(result).bundle_items[0].acquisition is BundleAcquisition.TO_BUY


async def test_an_explicit_already_owned_anchor_is_recorded_as_such() -> None:
    result, _ = await run(
        handoff(
            DesignAnchorIntent(
                reference=PresentedOrdinal(position=1),
                acquisition=BundleAcquisition.ALREADY_OWNED,
                quantity=2,
            )
        ),
        references=FakeReferences(default=ResolvedProductReference(product_id=ANCHOR_ID)),
        hydration=FakeHydration(available=(ANCHOR_ID,)),
    )

    line = room(result).bundle_items[0]
    assert line.acquisition is BundleAcquisition.ALREADY_OWNED
    assert line.quantity == 2


async def test_an_anchor_that_cannot_be_verified_creates_no_line() -> None:
    """Never a state line from a remembered fact."""
    result, parts = await run(
        handoff(DesignAnchorIntent(reference=PresentedOrdinal(position=1))),
        references=FakeReferences(default=ResolvedProductReference(product_id=ANCHOR_ID)),
        hydration=FakeHydration(available=()),
    )

    assert result.state.room_project is None or not room(result).bundle_items
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.PRODUCT_UNAVAILABLE
    assert not parts["design"].requests


async def test_an_anchor_already_in_the_bundle_locks_that_line() -> None:
    existing = state_with(
        BundleLineSpec(product_id=ANCHOR_ID, acquisition=BundleAcquisition.TO_BUY)
    )
    before = existing.room_project
    assert before is not None

    result, _ = await run(
        handoff(DesignAnchorIntent(reference=PresentedOrdinal(position=1))),
        state=existing,
        references=FakeReferences(default=ResolvedProductReference(product_id=ANCHOR_ID)),
        hydration=FakeHydration(available=(ANCHOR_ID,)),
    )

    line = next(i for i in room(result).bundle_items if i.product_id == ANCHOR_ID)
    assert line.line_id == before.bundle_items[0].line_id
    assert line.status is BundleItemStatus.LOCKED


async def test_two_lines_of_one_product_are_never_guessed_between() -> None:
    """"This sofa" names a product, not a line. Which physical line they meant
    is what bundle references settle, and those are M12E-4."""
    existing = state_with(
        BundleLineSpec(product_id=ANCHOR_ID, acquisition=BundleAcquisition.TO_BUY),
        BundleLineSpec(product_id=ANCHOR_ID, acquisition=BundleAcquisition.TO_BUY),
    )

    result, parts = await run(
        handoff(DesignAnchorIntent(reference=PresentedOrdinal(position=1))),
        state=existing,
        references=FakeReferences(default=ResolvedProductReference(product_id=ANCHOR_ID)),
        hydration=FakeHydration(available=(ANCHOR_ID,)),
    )

    assert result.grounding.deterministic_clarification is not None
    assert [i.status for i in room(result).bundle_items] == [
        BundleItemStatus.SUGGESTED,
        BundleItemStatus.SUGGESTED,
    ]
    assert not parts["design"].requests


# ── hard locks ══════════════════════════════════════════════════════════════


async def test_a_stale_hard_lock_stops_the_room_before_anything_runs() -> None:
    locked = state_with(
        BundleLineSpec(
            product_id=99,
            acquisition=BundleAcquisition.TO_BUY,
            status=BundleItemStatus.LOCKED,
        )
    )

    result, parts = await run(handoff(), state=locked, hydration=FakeHydration(available=()))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE
    assert not parts["design"].requests
    assert not parts["optimizer"].requests
    assert room(result).bundle_items[0].status is BundleItemStatus.LOCKED, "not unlocked"


async def test_locked_lines_keep_their_quantity_and_acquisition_into_the_optimizer() -> None:
    locked = state_with(
        BundleLineSpec(
            product_id=ANCHOR_ID,
            quantity=3,
            acquisition=BundleAcquisition.ALREADY_OWNED,
            status=BundleItemStatus.LOCKED,
        )
    )

    _, parts = await run(handoff(), state=locked, hydration=FakeHydration(available=(ANCHOR_ID,)))

    supplied = parts["optimizer"].requests[0].locked
    assert len(supplied) == 1
    assert supplied[0].quantity == 3
    assert supplied[0].acquisition is BundleAcquisition.ALREADY_OWNED


async def test_two_locked_lines_of_one_product_stay_two_optimizer_inputs() -> None:
    """Deduplicated for the read, expanded again afterwards: one row to fetch,
    two lines to preserve."""
    locked = state_with(
        BundleLineSpec(
            product_id=ANCHOR_ID,
            quantity=2,
            acquisition=BundleAcquisition.TO_BUY,
            status=BundleItemStatus.LOCKED,
        ),
        BundleLineSpec(
            product_id=ANCHOR_ID,
            quantity=1,
            acquisition=BundleAcquisition.ALREADY_OWNED,
            status=BundleItemStatus.LOCKED,
        ),
    )
    hydration = FakeHydration(available=(ANCHOR_ID,))

    _, parts = await run(handoff(), state=locked, hydration=hydration)

    supplied = parts["optimizer"].requests[0].locked
    assert [lock.quantity for lock in supplied] == [2, 1]
    assert {lock.acquisition for lock in supplied} == {
        BundleAcquisition.TO_BUY,
        BundleAcquisition.ALREADY_OWNED,
    }


async def test_the_anchor_quantity_reaches_the_specialist_without_identity() -> None:
    locked = state_with(
        BundleLineSpec(
            product_id=ANCHOR_ID,
            quantity=2,
            acquisition=BundleAcquisition.TO_BUY,
            status=BundleItemStatus.LOCKED,
        ),
        BundleLineSpec(
            product_id=ANCHOR_ID,
            quantity=1,
            acquisition=BundleAcquisition.ALREADY_OWNED,
            status=BundleItemStatus.LOCKED,
        ),
    )

    _, parts = await run(handoff(), state=locked, hydration=FakeHydration(available=(ANCHOR_ID,)))

    anchors = parts["design"].requests[0].anchors
    assert len(anchors) == 1, "one product, however many lines carried it"
    assert anchors[0].quantity == 3
    assert anchors[0].locked is True
    rendered = anchors[0].model_dump()
    for forbidden in ("product_id", "line_id", "acquisition", "price_amount"):
        assert forbidden not in rendered


# ── the design request ══════════════════════════════════════════════════════


async def test_the_brief_is_the_customers_own_message_trimmed() -> None:
    _, parts = await run(handoff(), message="  a calm living room, no TV unit  ")

    assert parts["design"].requests[0].design_brief == "a calm living room, no TV unit"


async def test_an_over_long_message_becomes_no_brief_rather_than_a_truncated_one() -> None:
    """Cut mid-phrase, "no TV unit" becomes "no TV". Silence is safer."""
    _, parts = await run(handoff(), message="x" * 5000)

    assert parts["design"].requests[0].design_brief is None


async def test_this_turns_budget_reaches_the_optimizer() -> None:
    """The budget is stated in the very message that asks for the room, so it
    must be folded in before execution, not after it."""
    decision = handoff(
        state_proposal=CustomerStateProposal(
            room_type="living room",
            room_budget=PriceProposal(max_amount="15000", currency="SAR"),
        )
    )

    result, parts = await run(decision, message="design my living room under SAR 15,000")

    assert parts["optimizer"].requests[0].budget == PriceConstraint.at_most(
        Decimal("15000"), "SAR"
    )
    assert parts["design"].requests[0].room_type == "living room"
    assert room(result).room_type == "living room"


async def test_the_specialist_never_learns_the_retailer() -> None:
    _, parts = await run(handoff())

    rendered = parts["design"].requests[0].model_dump()
    for forbidden in ("store_id", "context", "retailer_context"):
        assert forbidden not in rendered


async def test_capabilities_are_resolved_for_the_request_scope() -> None:
    _, parts = await run(handoff())

    assert parts["capabilities"].calls == [CONTEXT]


# ── failures ════════════════════════════════════════════════════════════════


async def test_a_design_provider_failure_is_not_a_catalog_verdict() -> None:
    result, parts = await run(handoff(), design=FakeDesign(error=LLMUnavailableError()))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.DESIGN_UNAVAILABLE
    assert not parts["optimizer"].requests


async def test_a_capability_failure_is_a_design_failure_not_zero_products() -> None:
    result, _ = await run(
        handoff(), capabilities=FakeCapabilities(error=CatalogUnavailableError())
    )

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.DESIGN_UNAVAILABLE


async def test_a_discovery_infrastructure_failure_is_never_zero_results() -> None:
    result, parts = await run(
        handoff(), design_discovery=FakeDesignDiscovery(error=CatalogUnavailableError())
    )

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.SEARCH_UNAVAILABLE
    assert not parts["optimizer"].requests


async def test_customer_facts_survive_a_design_failure() -> None:
    decision = handoff(
        state_proposal=CustomerStateProposal(
            room_type="living room",
            room_budget=PriceProposal(max_amount="15000", currency="SAR"),
        )
    )

    result, _ = await run(decision, design=FakeDesign(error=LLMUnavailableError()))

    assert room(result).room_type == "living room"
    assert room(result).budget is not None


async def test_a_verified_anchor_survives_a_design_failure() -> None:
    """They said to keep it. That is a fact about them, not about the plan."""
    result, _ = await run(
        handoff(DesignAnchorIntent(reference=PresentedOrdinal(position=1))),
        references=FakeReferences(default=ResolvedProductReference(product_id=ANCHOR_ID)),
        hydration=FakeHydration(available=(ANCHOR_ID,)),
        design=FakeDesign(error=LLMUnavailableError()),
    )

    line = room(result).bundle_items[0]
    assert line.product_id == ANCHOR_ID
    assert line.status is BundleItemStatus.LOCKED


# ── the commit ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status", [BundleStatus.COMPLETE, BundleStatus.PARTIAL])
async def test_a_real_room_is_committed(status: BundleStatus) -> None:
    outcome = RoomBundle(
        lines=(selected(20),),
        status=status,
        unmet=(
            (
                UnmetNeed(
                    need_index=1,
                    priority=DesignPriority.REQUIRED,
                    shortfall=1,
                    reason=UnmetReason.NO_CANDIDATES,
                ),
            )
            if status is BundleStatus.PARTIAL
            else ()
        ),
        new_spend_total=Decimal("4000.00"),
        currency="SAR",
    )

    result, _ = await run(handoff(), optimizer=FakeOptimizer(outcome),
        design=FakeDesign(plan_with()),
    )

    assert [i.product_id for i in room(result).bundle_items] == [20]
    assert room(result).bundle_items[0].status is BundleItemStatus.SUGGESTED
    assert room(result).bundle_revision == 1


async def test_an_infeasible_room_leaves_the_current_bundle_alone() -> None:
    existing = state_with(
        BundleLineSpec(product_id=77, acquisition=BundleAcquisition.TO_BUY)
    )
    outcome = RoomBundle(
        lines=(),
        status=BundleStatus.INFEASIBLE,
        new_spend_total=None,
        currency=None,
        total_unavailable=TotalUnavailableReason.NO_PRICED_LINES,
    )

    result, _ = await run(handoff(), state=existing, optimizer=FakeOptimizer(outcome),
        design=FakeDesign(plan_with()),
    )

    assert [i.product_id for i in room(result).bundle_items] == [77]
    assert result.bundle_outcome is outcome


async def test_an_unavailable_optimization_leaves_the_current_bundle_alone() -> None:
    existing = state_with(
        BundleLineSpec(product_id=77, acquisition=BundleAcquisition.TO_BUY)
    )
    outcome = BundleUnavailable(
        reason=BundleUnavailableReason.UNSUPPORTED_BUDGET_FORM
    )

    result, _ = await run(handoff(), state=existing, optimizer=FakeOptimizer(outcome),
        design=FakeDesign(plan_with()),
    )

    assert [i.product_id for i in room(result).bundle_items] == [77]
    assert result.bundle_outcome is outcome


async def test_a_locked_line_keeps_its_identity_across_the_commit() -> None:
    locked = state_with(
        BundleLineSpec(
            product_id=ANCHOR_ID,
            quantity=2,
            acquisition=BundleAcquisition.ALREADY_OWNED,
            status=BundleItemStatus.LOCKED,
        ),
        BundleLineSpec(product_id=55, acquisition=BundleAcquisition.TO_BUY),
    )
    before = locked.room_project
    assert before is not None
    locked_id = before.bundle_items[0].line_id

    result, _ = await run(
        handoff(),
        state=locked,
        hydration=FakeHydration(available=(ANCHOR_ID,)),
        optimizer=FakeOptimizer(bundle(selected(20))),
        design=FakeDesign(plan_with()),
    )

    kept = next(i for i in room(result).bundle_items if i.product_id == ANCHOR_ID)
    assert kept.line_id == locked_id
    assert kept.quantity == 2
    assert kept.acquisition is BundleAcquisition.ALREADY_OWNED
    assert kept.status is BundleItemStatus.LOCKED
    assert 55 not in [i.product_id for i in room(result).bundle_items], "old suggestion"


async def test_no_catalog_fact_is_persisted_by_the_commit() -> None:
    result, _ = await run(handoff(), optimizer=FakeOptimizer(bundle(selected(20))),
        design=FakeDesign(plan_with()),
    )

    rendered = str(room(result).model_dump())
    for forbidden in ("Sofa 20", "1000", "example.test", "Beige", "Modern"):
        assert forbidden not in rendered


async def test_the_outcome_reaches_the_result_for_the_response_phase() -> None:
    outcome = bundle(selected(20))

    result, _ = await run(handoff(), optimizer=FakeOptimizer(outcome),
        design=FakeDesign(plan_with()),
    )

    assert result.bundle_outcome is outcome
    assert "bundle_outcome" not in result.grounding.model_dump()


# ── the capability may simply not be configured ═════════════════════════════
#
# M12B made the design specialist optional, and M12E-2 briefly made it
# mandatory by requiring it to build the coordinator at all. A retailer who has
# not set up room planning still sells furniture: everything else must work,
# and only a request for a whole room should find it missing.


async def _unconfigured(
    decision: CustomerAgentDecision, **parts: Any
) -> Any:
    coordinator, built = _coordinator(decision, design=None, **parts)
    result = await coordinator.run(
        CustomerTurnInput(
            message="design my living room", state=state_with(), context=CONTEXT
        )
    )
    return result, built


async def test_a_coordinator_builds_without_the_design_capability() -> None:
    coordinator, _ = _coordinator(handoff(), design=None)

    assert coordinator is not None


async def test_an_answer_turn_works_without_it() -> None:
    result, _ = await _unconfigured(CustomerAgentDecision(action=AgentAction.ANSWER))

    assert result.grounding.failure is None
    assert result.grounding.design_handoff_requested is False


async def test_a_search_turn_works_without_it() -> None:
    result, _ = await _unconfigured(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(),
    )

    assert result.grounding.failure is None
    assert result.grounding.search is not None
    assert result.grounding.search.products


@pytest.mark.parametrize(
    "action", [AgentAction.PRODUCT_DETAIL, AgentAction.COMPARE], ids=str
)
def test_every_other_action_remains_constructible_without_it(
    action: AgentAction,
) -> None:
    """The coordinator dispatches on action; none of the others can reach the
    specialist, so none of them can be taken down by its absence."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=action,
            reference=PresentedOrdinal(position=1)
            if action is AgentAction.PRODUCT_DETAIL
            else None,
            comparison_references=()
            if action is AgentAction.PRODUCT_DETAIL
            else (PresentedOrdinal(position=1), PresentedOrdinal(position=2)),
        ),
        design=None,
    )

    assert coordinator is not None


async def test_a_room_request_reports_the_capability_missing() -> None:
    result, _ = await _unconfigured(handoff())

    assert result.grounding.design_handoff_requested is True
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.DESIGN_UNAVAILABLE


async def test_nothing_downstream_runs_when_it_is_missing() -> None:
    result, parts = await _unconfigured(handoff())

    assert parts["capabilities"].calls == []
    assert parts["design_discovery"].calls == []
    assert parts["optimizer"].requests == []
    assert result.bundle_outcome is None


async def test_the_customers_own_facts_still_survive() -> None:
    decision = handoff(
        state_proposal=CustomerStateProposal(
            room_type="living room",
            room_budget=PriceProposal(max_amount="15000", currency="SAR"),
        )
    )

    result, _ = await _unconfigured(decision)

    assert room(result).room_type == "living room"
    assert room(result).budget is not None


async def test_an_existing_bundle_is_left_exactly_as_it_was() -> None:
    existing = state_with(
        BundleLineSpec(product_id=77, acquisition=BundleAcquisition.TO_BUY)
    )
    before = existing.room_project
    assert before is not None

    coordinator, _ = _coordinator(handoff(), design=None)
    result = await coordinator.run(
        CustomerTurnInput(
            message="design my living room", state=existing, context=CONTEXT
        )
    )

    assert room(result).bundle_items == before.bundle_items
    assert room(result).bundle_revision == before.bundle_revision


async def test_an_absent_capability_never_raises() -> None:
    """A deployment choice is not a defect, so it does not surface as one."""
    result, _ = await _unconfigured(handoff())

    assert result.state is not None
