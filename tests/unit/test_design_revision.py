"""Recomposing a room the customer already has.

Four themes, and each exists because the obvious implementation gets it wrong.

**An exclusion is a predicate, not a deletion.** M12E-4C's `REMOVE_NEED` takes
out one durable role and must therefore identify exactly one. A revision
exclusion says *this role must not appear in the revised plan*, which two
matching roles make doubly true rather than ambiguous.

**Nothing is locked until every hard constraint holds.** One unresolvable
reference among several must leave no pieces preserved, or the customer ends up
with half of what they asked for and no way to tell.

**A contradiction is reported, never resolved.** Keeping a piece whose role is
also being removed is two clear instructions; picking one discards the other.

**An excluded role coming back refuses the whole plan.** Dropping it quietly
would be indistinguishable from the retailer not stocking it, and the customer
would be told their redesign succeeded.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from app.core.exceptions import CatalogUnavailableError, LLMResponseInvalidError
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarificationReason,
    CustomerAgentDecision,
    DesignRevisionIntent,
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
from app.schemas.bundle_reference import (
    BundleCategoryMatch,
    BundleItemOrdinal,
    DesignNeedCategoryMatch,
)
from app.schemas.design import (
    CurrentDesignNeed,
    DesignCategoryNeed,
    DesignPriority,
    DesignRevisionContext,
    DesignTask,
    ExcludedDesignRole,
    InteriorDesignRequest,
    InteriorDesignResult,
)
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.grounding import TurnFailureCode
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.resolution import DesignNeedFailureReason, DesignNeedUnresolved
from app.schemas.retailer import RetailerContext
from app.services.agent_state import apply_update
from app.services.design_revision import (
    conflicting_role,
    deduplicated_exclusions,
    project_current_plan,
)
from pydantic import ValidationError

from tests.unit.test_turn_coordinator import (
    FakeCapabilities,
    FakeDesign,
    FakeDesignDiscovery,
    FakeHydration,
    FakeOptimizer,
    _coordinator,
)

CONTEXT = RetailerContext(store_id=50)


def product(
    product_id: int,
    *,
    category: str = "seating",
    subcategory: str | None = "sofa",
    price: str = "1000.00",
) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Item {product_id}",
        name_arabic="منتج",
        price_amount=Decimal(price),
        price_unit="SAR",
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
    priority: DesignPriority = DesignPriority.REQUIRED,
    quantity: int = 1,
    intent: str | None = None,
) -> DesignNeedSpec:
    return DesignNeedSpec(
        commerce_category=category,
        commerce_subcategory=subcategory,
        priority=priority,
        quantity=quantity,
        semantic_intent=intent,
    )


def line(
    product_id: int, *, need_index: int | None = 0, locked: bool = False
) -> PlannedBundleLineSpec:
    return PlannedBundleLineSpec(
        product_id=product_id,
        quantity=1,
        acquisition=BundleAcquisition.TO_BUY,
        status=BundleItemStatus.LOCKED if locked else BundleItemStatus.SUGGESTED,
        need_index=need_index,
    )


def a_room(
    *,
    needs: tuple[DesignNeedSpec, ...] = (),
    lines: tuple[PlannedBundleLineSpec, ...] = (),
    budget: str | None = None,
) -> AgentStateV1:
    from app.schemas.discovery import PriceConstraint

    return apply_update(
        AgentStateV1(),
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                budget=PriceConstraint.at_most(Decimal(budget), "SAR")
                if budget
                else None,
                bundle_operations=(ReplaceDesignPlan(needs=needs, added=lines),),
            )
        ),
    )


def room(state: AgentStateV1) -> RoomProjectState:
    project = state.room_project
    assert isinstance(project, RoomProjectState)
    return project


def revise(
    *,
    removed: tuple[DesignNeedCategoryMatch, ...] = (),
    preserved: tuple[Any, ...] = (),
) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF,
        design_revision=DesignRevisionIntent(
            removed_needs=removed, preserved_items=preserved
        ),
    )


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


async def run(
    decision: CustomerAgentDecision,
    state: AgentStateV1,
    *,
    available: tuple[int, ...] | None = None,
    outcome: Any = None,
    design: Any = None,
    design_error: Exception | None = None,
    discovery_error: Exception | None = None,
) -> Any:
    project = state.room_project
    current = tuple(i.product_id for i in project.bundle_items) if project else ()
    ids = available if available is not None else (*current, 77)
    coordinator, parts = _coordinator(
        decision,
        capabilities=FakeCapabilities(
            (
                ("seating", "sofa"),
                ("lighting", "floor-lamp"),
                ("dining", "dining-table"),
            )
        ),
        hydration=FakeHydration(available=ids),
        design=design or FakeDesign(
            plan_of(category_need("seating", "sofa")), error=design_error
        ),
        optimizer=FakeOptimizer(outcome) if outcome is not None else None,
        design_discovery=FakeDesignDiscovery(error=discovery_error)
        if discovery_error
        else None,
    )
    result = await coordinator.run(
        CustomerTurnInput(message="redesign this room", state=state, context=CONTEXT)
    )
    return result, parts


def plan_of(*needs: DesignCategoryNeed) -> InteriorDesignResult:
    return InteriorDesignResult(needs=needs)


def category_need(category: str, subcategory: str | None = None) -> DesignCategoryNeed:
    return DesignCategoryNeed(
        commerce_category=category,
        commerce_subcategory=subcategory,
        priority=DesignPriority.REQUIRED,
    )


# ── the contract ════════════════════════════════════════════════════════════


def test_the_revision_intent_names_no_identity_and_no_figure() -> None:
    for forbidden in ("need_id", "line_id", "product_id", "revision", "price"):
        for field in DesignRevisionIntent.model_fields:
            assert forbidden not in field, field


def test_only_a_design_handoff_carries_revision_constraints() -> None:
    intent = DesignRevisionIntent(
        removed_needs=(DesignNeedCategoryMatch(commerce_category="tables"),)
    )
    assert CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF, design_revision=intent
    )
    for action in (AgentAction.ANSWER, AgentAction.SEARCH, AgentAction.BUNDLE_REFINE):
        with pytest.raises(ValidationError):
            CustomerAgentDecision(action=action, design_revision=intent)


def test_a_revision_describes_a_plan_that_exists() -> None:
    """An empty revision is an initial plan, and says so by being absent."""
    with pytest.raises(ValidationError):
        DesignRevisionContext(current_needs=())


def test_general_advice_revises_no_plan() -> None:
    context = DesignRevisionContext(
        current_needs=(
            CurrentDesignNeed(
                commerce_category="seating", priority=DesignPriority.REQUIRED
            ),
        )
    )
    with pytest.raises(ValidationError):
        InteriorDesignRequest(
            task=DesignTask.GENERAL_ADVICE, question="what suits walnut?",
            revision=context,
        )


def test_the_current_need_carries_design_meaning_and_no_identity() -> None:
    assert set(CurrentDesignNeed.model_fields) == {
        "commerce_category",
        "commerce_subcategory",
        "priority",
        "quantity",
        "seating_capacity",
        "semantic_intent",
    }


def test_the_three_design_carriers_share_one_intent_rule() -> None:
    """Not a third contract: a figure in design wording is refused everywhere.

    `ActiveSearchState` has its own, separate `semantic_intent` for searches;
    reaching for that one by name-similarity is the mistake this pins against.
    """
    from app.schemas.agent_state import RoomDesignNeedState

    for model, kwargs in (
        (CurrentDesignNeed, {"priority": DesignPriority.REQUIRED}),
        (DesignCategoryNeed, {"priority": DesignPriority.REQUIRED}),
        (
            RoomDesignNeedState,
            {"need_id": 1, "priority": DesignPriority.REQUIRED, "quantity": 1},
        ),
    ):
        with pytest.raises(ValidationError):
            model(commerce_category="seating", semantic_intent="under 200 cm", **kwargs)
        built = model(
            commerce_category="seating", semantic_intent="  light and airy  ", **kwargs
        )
        assert built.semantic_intent == "light and airy"


# ── projection ══════════════════════════════════════════════════════════════


def test_the_plan_projects_in_its_own_order_without_identity() -> None:
    state = a_room(
        needs=(
            need("lighting", "floor-lamp", priority=DesignPriority.OPTIONAL),
            need("seating", "sofa", quantity=2, intent="low and soft"),
        )
    )

    current = project_current_plan(room(state))

    assert [n.commerce_category for n in current] == ["lighting", "seating"]
    assert current[1].quantity == 2
    assert current[1].semantic_intent == "low and soft"
    assert current[0].priority is DesignPriority.OPTIONAL


def test_no_plan_projects_to_nothing() -> None:
    assert project_current_plan(None) == ()


# ── exclusion resolution ════════════════════════════════════════════════════


def resolve(
    selectors: tuple[DesignNeedCategoryMatch, ...], state: AgentStateV1
) -> Any:
    from app.services.bundle_reference import BundleReferenceResolver
    from app.taxonomy.registry import load_taxonomy

    return BundleReferenceResolver(load_taxonomy()).resolve_exclusions(
        selectors, room(state)
    )


def test_an_exact_pair_excludes_that_pair() -> None:
    state = a_room(needs=(need("dining", "dining-table"), need()))

    excluded = resolve(
        (
            DesignNeedCategoryMatch(
                commerce_category="dining", commerce_subcategory="dining-table"
            ),
        ),
        state,
    )

    assert excluded == (
        ExcludedDesignRole(
            commerce_category="dining", commerce_subcategory="dining-table"
        ),
    )


def test_two_roles_of_one_kind_do_not_make_an_exclusion_ambiguous() -> None:
    """The difference from E4C, and the whole point of a separate rule.

    Removing "the" dining chair from two is undefined and must be asked about.
    Saying the revised room has no dining chairs is clear, and having two of
    them makes it more applicable rather than less.
    """
    state = a_room(
        needs=(
            need("seating", "dining-chair"),
            need("seating", "dining-chair"),
            need(),
        )
    )

    excluded = resolve(
        (
            DesignNeedCategoryMatch(
                commerce_category="seating", commerce_subcategory="dining-chair"
            ),
        ),
        state,
    )

    assert excluded == (
        ExcludedDesignRole(
            commerce_category="seating", commerce_subcategory="dining-chair"
        ),
    )


def test_a_bare_category_excludes_the_whole_family() -> None:
    state = a_room(needs=(need("lighting", "floor-lamp"), need()))

    excluded = resolve(
        (DesignNeedCategoryMatch(commerce_category="lighting"),), state
    )

    assert excluded == (ExcludedDesignRole(commerce_category="lighting"),)


def test_a_role_the_plan_does_not_hold_is_asked_about() -> None:
    state = a_room(needs=(need(),))

    outcome = resolve(
        (DesignNeedCategoryMatch(commerce_category="tables"),), state
    )

    assert isinstance(outcome, DesignNeedUnresolved)
    assert outcome.reason is DesignNeedFailureReason.NO_NEED_MATCH


def test_an_unapproved_role_is_refused_not_matched_loosely() -> None:
    state = a_room(needs=(need(),))

    outcome = resolve(
        (DesignNeedCategoryMatch(commerce_category="luxury-lounge"),), state
    )

    assert isinstance(outcome, DesignNeedUnresolved)
    assert outcome.reason is DesignNeedFailureReason.UNAPPROVED_COMMERCE_TYPE


def test_an_unplanned_room_has_no_role_to_exclude() -> None:
    outcome = resolve(
        (DesignNeedCategoryMatch(commerce_category="seating"),), a_room()
    )

    assert isinstance(outcome, DesignNeedUnresolved)
    assert outcome.reason is DesignNeedFailureReason.NO_PLAN


def test_the_same_role_named_twice_is_one_constraint() -> None:
    roles = (
        ExcludedDesignRole(commerce_category="seating", commerce_subcategory="sofa"),
        ExcludedDesignRole(commerce_category="seating", commerce_subcategory="sofa"),
    )

    assert deduplicated_exclusions(roles) == roles[:1]


def test_a_family_and_one_of_its_members_stay_two_constraints() -> None:
    """Neither implies the other was meant, so collapsing either would widen or
    narrow what the customer ruled out."""
    roles = (
        ExcludedDesignRole(commerce_category="seating"),
        ExcludedDesignRole(commerce_category="seating", commerce_subcategory="sofa"),
    )

    assert deduplicated_exclusions(roles) == roles


# ── what the specialist is told ═════════════════════════════════════════════


async def test_an_existing_plan_makes_every_handoff_a_revision() -> None:
    """No marker from the model: "add a reading corner" is a recomposition."""
    state = a_room(needs=(need(), need("lighting", "floor-lamp")), lines=(line(10),))

    _, parts = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF), state
    )

    request = parts["design"].requests[0]
    assert request.revision is not None
    assert [n.commerce_category for n in request.revision.current_needs] == [
        "seating",
        "lighting",
    ]
    assert request.revision.excluded == ()


async def test_a_room_with_no_plan_is_not_a_revision() -> None:
    _, parts = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF), AgentStateV1()
    )

    assert parts["design"].requests[0].revision is None


async def test_a_committed_empty_plan_replans_from_scratch() -> None:
    """Reachable: a plan whose every role this retailer turned out not to stock
    commits as a complete room with no lines. There is nothing to preserve, so
    revising it and planning it afresh are the same thing (M12E-4D 8).
    """
    state = a_room(needs=(), lines=())

    _, parts = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF), state
    )

    assert parts["design"].requests[0].revision is None


async def test_exclusions_reach_the_specialist_as_hard_constraints() -> None:
    state = a_room(needs=(need("dining", "dining-table"), need()), lines=(line(10),))

    _, parts = await run(
        revise(
            removed=(
                DesignNeedCategoryMatch(
                    commerce_category="dining", commerce_subcategory="dining-table"
                ),
            )
        ),
        state,
    )

    request = parts["design"].requests[0]
    assert request.revision.excluded == (
        ExcludedDesignRole(
            commerce_category="dining", commerce_subcategory="dining-table"
        ),
    )
    assert len(parts["design"].requests) == 1, "one specialist call"


async def test_an_unresolvable_exclusion_asks_rather_than_replanning() -> None:
    state = a_room(needs=(need(),), lines=(line(10),))

    result, parts = await run(
        revise(removed=(DesignNeedCategoryMatch(commerce_category="tables"),)), state
    )

    assert parts["design"].requests == [], "nothing is planned on a bad constraint"
    assert (
        result.grounding.deterministic_clarification.need_reason
        is DesignNeedFailureReason.NO_NEED_MATCH
    )
    assert len(room(result.state).design_needs) == 1


# ── preserving pieces ═══════════════════════════════════════════════════════


async def test_a_visible_card_can_be_preserved_through_a_recomposition() -> None:
    state = a_room(needs=(need(),), lines=(line(10),))

    result, _ = await run(
        revise(preserved=(BundleItemOrdinal(ordinal=1),)),
        state,
        outcome=chosen(77),
    )

    kept = [
        item
        for item in room(result.state).bundle_items
        if item.status is BundleItemStatus.LOCKED
    ]
    assert [item.product_id for item in kept] == [10]


async def test_a_preserved_card_may_be_named_by_its_kind() -> None:
    state = a_room(needs=(need(),), lines=(line(10),))

    result, _ = await run(
        revise(preserved=(BundleCategoryMatch(commerce_category="seating"),)),
        state,
        outcome=chosen(77),
    )

    assert room(result.state).bundle_items[0].status is BundleItemStatus.LOCKED


async def test_several_preserved_cards_lock_in_one_transition() -> None:
    state = a_room(
        needs=(need(), need("lighting", "floor-lamp")),
        lines=(line(10, need_index=0), line(11, need_index=1)),
    )
    before = room(state).bundle_revision

    result, _ = await run(
        revise(
            preserved=(BundleItemOrdinal(ordinal=1), BundleItemOrdinal(ordinal=2))
        ),
        state,
        available=(10, 11, 77),
        outcome=chosen(77),
    )

    locked = {
        item.product_id
        for item in room(result.state).bundle_items
        if item.status is BundleItemStatus.LOCKED
    }
    assert locked == {10, 11}
    assert room(result.state).bundle_revision > before


async def test_preserving_what_is_already_kept_changes_nothing() -> None:
    """The preserve step alone, isolated from the commit that normally follows.

    A successful replan commits a new plan and bundle, which advances the
    revision on its own - so a failed redesign is the only way to observe that
    locking an already-locked card advanced nothing.
    """
    state = a_room(needs=(need(),), lines=(line(10, locked=True),))
    before = room(state).bundle_revision

    result, _ = await run(
        revise(preserved=(BundleItemOrdinal(ordinal=1),)),
        state,
        design_error=LLMResponseInvalidError(reason="provider"),
    )

    assert room(result.state).bundle_revision == before
    assert room(result.state).bundle_items[0].status is BundleItemStatus.LOCKED


async def test_one_bad_reference_preserves_nothing_at_all() -> None:
    """No partial preservation: every hard constraint holds, or none applies."""
    state = a_room(
        needs=(need(), need("lighting", "floor-lamp")),
        lines=(line(10, need_index=0), line(11, need_index=1)),
    )

    result, parts = await run(
        revise(
            preserved=(BundleItemOrdinal(ordinal=1), BundleItemOrdinal(ordinal=9))
        ),
        state,
        available=(10, 11),
    )

    assert parts["design"].requests == []
    assert all(
        item.status is BundleItemStatus.SUGGESTED
        for item in room(result.state).bundle_items
    )


async def test_a_stale_preserved_card_stops_the_recomposition() -> None:
    state = a_room(needs=(need(),), lines=(line(10, locked=True),))

    result, parts = await run(
        revise(preserved=(BundleItemOrdinal(ordinal=1),)), state, available=()
    )

    assert parts["design"].requests == []
    assert (
        result.grounding.failure.code is TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE
    )


async def test_a_preserved_piece_survives_a_specialist_failure() -> None:
    """They said to keep it. That is true whatever the redesign did next."""
    state = a_room(needs=(need(),), lines=(line(10),))

    result, _ = await run(
        revise(preserved=(BundleItemOrdinal(ordinal=1),)),
        state,
        design_error=LLMResponseInvalidError(reason="provider"),
    )

    assert room(result.state).bundle_items[0].status is BundleItemStatus.LOCKED


async def test_a_preserved_piece_survives_a_discovery_failure() -> None:
    state = a_room(needs=(need(),), lines=(line(10),))

    result, _ = await run(
        revise(preserved=(BundleItemOrdinal(ordinal=1),)),
        state,
        discovery_error=CatalogUnavailableError(),
    )

    assert room(result.state).bundle_items[0].status is BundleItemStatus.LOCKED


# ── contradictions ══════════════════════════════════════════════════════════


def test_an_unclassified_product_conflicts_with_nothing() -> None:
    """A missing commerce category is unverified, never "any"."""
    unclassified = product(10, category=None, subcategory=None)  # type: ignore[arg-type]

    assert (
        conflicting_role(
            (ExcludedDesignRole(commerce_category="seating"),), (unclassified,)
        )
        is None
    )


async def test_keeping_a_piece_whose_role_is_removed_is_asked_about() -> None:
    state = a_room(needs=(need(),), lines=(line(10),))

    result, parts = await run(
        revise(
            removed=(
                DesignNeedCategoryMatch(
                    commerce_category="seating", commerce_subcategory="sofa"
                ),
            ),
            preserved=(BundleItemOrdinal(ordinal=1),),
        ),
        state,
    )

    assert parts["design"].requests == []
    clarification = result.grounding.deterministic_clarification
    assert clarification.reason is (
        BlockingClarificationReason.CONTRADICTORY_ROOM_INSTRUCTIONS
    )
    assert clarification.need_reason is DesignNeedFailureReason.PRESERVED_ROLE_EXCLUDED


async def test_a_parent_exclusion_conflicts_with_a_preserved_member() -> None:
    state = a_room(needs=(need(),), lines=(line(10),))

    result, _ = await run(
        revise(
            removed=(DesignNeedCategoryMatch(commerce_category="seating"),),
            preserved=(BundleItemOrdinal(ordinal=1),),
        ),
        state,
    )

    assert (
        result.grounding.deterministic_clarification.need_reason
        is DesignNeedFailureReason.PRESERVED_ROLE_EXCLUDED
    )


async def test_nothing_is_locked_when_the_instructions_contradict() -> None:
    state = a_room(needs=(need(),), lines=(line(10),))

    result, _ = await run(
        revise(
            removed=(DesignNeedCategoryMatch(commerce_category="seating"),),
            preserved=(BundleItemOrdinal(ordinal=1),),
        ),
        state,
    )

    assert room(result.state).bundle_items[0].status is BundleItemStatus.SUGGESTED


async def test_an_unrelated_preserve_and_exclusion_proceed_together() -> None:
    state = a_room(
        needs=(need(), need("dining", "dining-table")),
        lines=(line(10, need_index=0), line(11, need_index=1)),
    )

    result, parts = await run(
        revise(
            removed=(
                DesignNeedCategoryMatch(
                    commerce_category="dining", commerce_subcategory="dining-table"
                ),
            ),
            preserved=(BundleItemOrdinal(ordinal=1),),
        ),
        state,
        available=(10, 11, 77),
        outcome=chosen(77),
    )

    assert len(parts["design"].requests) == 1
    assert result.grounding.deterministic_clarification is None


# ── what comes back ═════════════════════════════════════════════════════════


def specialist(result: InteriorDesignResult) -> Any:
    from app.services.interior_design import InteriorDesignAgent
    from app.taxonomy.registry import load_taxonomy

    class _Client:
        model = "test-model"

        async def parse(self, **_: Any) -> InteriorDesignResult:
            return result

    return InteriorDesignAgent(_Client(), load_taxonomy())  # type: ignore[arg-type]


def _capabilities(*pairs: tuple[str, str | None]) -> Any:
    from app.schemas.retailer import (
        RetailerCatalogCapabilities,
        RetailerCatalogCapability,
    )

    return RetailerCatalogCapabilities(
        capabilities=tuple(
            RetailerCatalogCapability(
                commerce_category=category, commerce_subcategory=subcategory
            )
            for category, subcategory in pairs
        )
    )


def revision_request(*excluded: ExcludedDesignRole) -> InteriorDesignRequest:
    return InteriorDesignRequest(
        task=DesignTask.ROOM_PLAN,
        design_brief="make it work for reading",
        catalog_capabilities=_capabilities(
            ("seating", "sofa"), ("dining", "dining-table")
        ),
        revision=DesignRevisionContext(
            current_needs=(
                CurrentDesignNeed(
                    commerce_category="seating", priority=DesignPriority.REQUIRED
                ),
            ),
            excluded=excluded,
        ),
    )


async def test_an_excluded_pair_coming_back_refuses_the_whole_plan() -> None:
    agent = specialist(
        plan_of(category_need("dining", "dining-table"), category_need("seating", "sofa"))
    )

    with pytest.raises(LLMResponseInvalidError):
        await agent.plan(
            revision_request(
                ExcludedDesignRole(
                    commerce_category="dining", commerce_subcategory="dining-table"
                )
            )
        )


async def test_an_excluded_family_coming_back_refuses_the_whole_plan() -> None:
    agent = specialist(plan_of(category_need("dining", "dining-table")))

    with pytest.raises(LLMResponseInvalidError):
        await agent.plan(
            revision_request(ExcludedDesignRole(commerce_category="dining"))
        )


async def test_the_exclusion_check_runs_before_the_capability_filter() -> None:
    """The masking bug this ordering exists to prevent.

    `_fulfillable` *drops* a role the retailer cannot supply. An excluded role
    reaching it would be deleted silently and be indistinguishable from a
    stocking decision - and the customer would be told their redesign happened
    while the one thing they ruled out had been quietly removed instead of
    honoured.
    """
    unsupported_and_excluded = category_need("decor", "carpet")
    agent = specialist(plan_of(unsupported_and_excluded))

    with pytest.raises(LLMResponseInvalidError):
        await agent.plan(
            revision_request(ExcludedDesignRole(commerce_category="decor"))
        )


async def test_a_plan_obeying_every_exclusion_is_accepted() -> None:
    agent = specialist(plan_of(category_need("seating", "sofa")))

    result = await agent.plan(
        revision_request(ExcludedDesignRole(commerce_category="dining"))
    )

    assert [n.commerce_subcategory for n in result.needs] == ["sofa"]


# ── the provider boundary ═══════════════════════════════════════════════════


def _reachable_field_names(schema: dict[str, Any]) -> set[str]:
    """Every property name in the request's type graph.

    Structure only. An earlier version of this guard matched the rendered
    schema string and fired on the word `rejected_product_ids` inside a
    docstring explaining that the field is absent - a guard that reads prose
    tests the wrong thing in both directions.
    """
    names: set[str] = set(schema.get("properties", {}))
    for definition in schema.get("$defs", {}).values():
        names |= set(definition.get("properties", {}))
    return names


def test_the_revision_request_reaches_no_identity() -> None:
    """`plan()` sends `model_dump_json()` wholesale, so whatever the type
    permits is exactly what the specialist sees (M12E-4D 40)."""
    names = _reachable_field_names(InteriorDesignRequest.model_json_schema())

    for forbidden in (
        "product_id",
        "line_id",
        "need_id",
        "bundle_revision",
        "store_id",
        "rejected_product_ids",
        "product_url",
        "image_url",
        "sku",
        "pinecone",
        "price_amount",
        "name_english",
    ):
        assert not {name for name in names if forbidden in name}, forbidden


def test_the_revision_request_still_carries_the_customers_budget() -> None:
    """The guard above must not be passing by excluding everything."""
    names = _reachable_field_names(InteriorDesignRequest.model_json_schema())

    assert "budget" in names
    assert "current_needs" in names
    assert "excluded" in names


def test_the_revision_request_carries_no_retailer_scope() -> None:
    definitions = set(InteriorDesignRequest.model_json_schema().get("$defs", {}))

    for forbidden in ("RetailerContext", "ProductCandidate", "AgentStateV1"):
        assert forbidden not in definitions


# ── commit and failure ══════════════════════════════════════════════════════


@pytest.mark.parametrize("status", [BundleStatus.COMPLETE, BundleStatus.PARTIAL])
async def test_a_real_revised_room_replaces_plan_and_bundle(
    status: BundleStatus,
) -> None:
    state = a_room(needs=(need(),), lines=(line(10),))
    design = FakeDesign(plan_of(category_need("lighting", "floor-lamp")))
    outcome = RoomBundle(
        lines=(
            BundleLine(
                need_index=0,
                product=product(77, category="lighting", subcategory="floor-lamp"),
                quantity=1,
                locked=False,
                acquisition=BundleAcquisition.TO_BUY,
                relaxation_depth=0,
            ),
        ),
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
        new_spend_total=Decimal("1000.00"),
        currency="SAR",
    )

    result, _ = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        state,
        design=design,
        outcome=outcome,
    )

    after = room(result.state)
    assert [n.commerce_category for n in after.design_needs] == ["lighting"]
    assert [i.product_id for i in after.bundle_items] == [77]


async def test_fresh_need_ids_are_allocated_and_old_ones_expire() -> None:
    state = a_room(needs=(need(),), lines=(line(10),))
    old_ids = {n.need_id for n in room(state).design_needs}
    design = FakeDesign(plan_of(category_need("lighting", "floor-lamp")))

    result, _ = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        state,
        design=design,
        outcome=chosen(77),
    )

    new_ids = {n.need_id for n in room(result.state).design_needs}
    assert new_ids.isdisjoint(old_ids)


async def test_a_preserved_lock_keeps_its_identity_and_loses_its_role() -> None:
    state = a_room(needs=(need(),), lines=(line(10, locked=True),))
    original = room(state).bundle_items[0]
    design = FakeDesign(plan_of(category_need("lighting", "floor-lamp")))

    result, _ = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        state,
        design=design,
        available=(10, 77),
        outcome=chosen(77),
    )

    kept = [i for i in room(result.state).bundle_items if i.product_id == 10]
    assert len(kept) == 1
    assert kept[0].line_id == original.line_id
    assert kept[0].status is BundleItemStatus.LOCKED
    assert kept[0].need_id is None, "the role it belonged to no longer exists"


@pytest.mark.parametrize(
    "failure",
    ["design", "discovery", "infeasible"],
)
async def test_a_failed_recomposition_leaves_the_old_room_intact(
    failure: str,
) -> None:
    """Compound change is atomic: the dining plan survives a failed redesign."""
    state = a_room(
        needs=(need("dining", "dining-table"), need()),
        lines=(line(10, need_index=1),),
    )
    kwargs: dict[str, Any] = {}
    if failure == "design":
        kwargs["design_error"] = LLMResponseInvalidError(reason="provider")
    elif failure == "discovery":
        kwargs["discovery_error"] = CatalogUnavailableError()
    else:
        kwargs["outcome"] = RoomBundle(
            lines=(),
            status=BundleStatus.INFEASIBLE,
            new_spend_total=None,
            currency=None,
            total_unavailable="no_priced_lines",
        )

    result, _ = await run(
        revise(
            removed=(
                DesignNeedCategoryMatch(
                    commerce_category="dining", commerce_subcategory="dining-table"
                ),
            )
        ),
        state,
        **kwargs,
    )

    after = room(result.state)
    assert [n.commerce_category for n in after.design_needs] == ["dining", "seating"]
    assert [i.product_id for i in after.bundle_items] == [10]


async def test_an_excluded_role_in_the_result_leaves_the_old_room() -> None:
    state = a_room(
        needs=(need("dining", "dining-table"), need()), lines=(line(10, need_index=1),)
    )
    # The real agent, because the refusal lives in its result validation -
    # exactly where the taxonomy refusal lives (M12E-4D 23).
    result, _ = await run(
        revise(
            removed=(
                DesignNeedCategoryMatch(
                    commerce_category="dining", commerce_subcategory="dining-table"
                ),
            )
        ),
        state,
        design=specialist(plan_of(category_need("dining", "dining-table"))),
        outcome=chosen(77),
    )

    after = room(result.state)
    assert [n.commerce_category for n in after.design_needs] == ["dining", "seating"]
    assert result.grounding.failure.code is TurnFailureCode.DESIGN_UNAVAILABLE


# ── the anchor is a hard constraint too (M12F 2) ════════════════════════════


ANCHOR_ID = 42
"""What `FakeReferences` resolves a presented selector to."""


def anchored(
    *, removed: tuple[DesignNeedCategoryMatch, ...] = ()
) -> CustomerAgentDecision:
    """Design around a product they were shown, while excluding roles."""
    from app.schemas.agent_decision import DesignAnchorIntent
    from app.schemas.product_reference import PresentedOrdinal

    return CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF,
        design_anchor=DesignAnchorIntent(reference=PresentedOrdinal(position=1)),
        design_revision=DesignRevisionIntent(removed_needs=removed)
        if removed
        else None,
    )


class UnclassifiedHydration:
    """A catalog row whose commerce classification was never reviewed."""

    def __init__(self, available: tuple[int, ...]) -> None:
        self.available = available
        self.calls: list[list[int]] = []

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[Any, ...]:
        self.calls.append(list(product_ids))
        return tuple(
            product(p, category=None, subcategory=None)  # type: ignore[arg-type]
            for p in product_ids
            if p in self.available
        )


async def test_an_anchor_whose_role_is_excluded_is_asked_about() -> None:
    """The gap M12F closes: E4D checked preserved cards and not anchors.

    "Design around this sofa" and "no seating in the new room" are the same
    contradiction whichever surface named the piece.
    """
    state = a_room(needs=(need(),), lines=(line(10),))

    result, parts = await run(
        anchored(removed=(DesignNeedCategoryMatch(commerce_category="seating"),)),
        state,
        available=(10, ANCHOR_ID),
    )

    assert parts["design"].requests == [], "nothing is planned on a contradiction"
    clarification = result.grounding.deterministic_clarification
    assert clarification.reason is (
        BlockingClarificationReason.CONTRADICTORY_ROOM_INSTRUCTIONS
    )
    assert clarification.need_reason is DesignNeedFailureReason.PRESERVED_ROLE_EXCLUDED


async def test_no_lock_is_written_when_the_anchor_contradicts_an_exclusion() -> None:
    """Neither the anchor's nor the preserved card's - resolution precedes all
    mutation, so a contradiction found late leaves nothing half-applied."""
    state = a_room(needs=(need(),), lines=(line(10),))

    result, _ = await run(
        anchored(removed=(DesignNeedCategoryMatch(commerce_category="seating"),)),
        state,
        available=(10, ANCHOR_ID),
    )

    after = room(result.state)
    assert all(item.status is BundleItemStatus.SUGGESTED for item in after.bundle_items)
    assert ANCHOR_ID not in [item.product_id for item in after.bundle_items]


async def test_an_anchor_of_an_unrelated_role_proceeds() -> None:
    state = a_room(needs=(need(), need("dining", "dining-table")), lines=(line(10),))

    result, parts = await run(
        anchored(
            removed=(
                DesignNeedCategoryMatch(
                    commerce_category="dining", commerce_subcategory="dining-table"
                ),
            )
        ),
        state,
        available=(10, ANCHOR_ID),
        outcome=chosen(77),
    )

    assert len(parts["design"].requests) == 1
    assert result.grounding.deterministic_clarification is None


async def test_an_anchor_with_no_exclusions_needs_no_role_at_all() -> None:
    """Nothing to prove, so an unreviewed classification is ordinary."""
    coordinator, parts = _coordinator(
        anchored(),
        capabilities=FakeCapabilities((("seating", "sofa"),)),
        hydration=cast(Any, UnclassifiedHydration((ANCHOR_ID,))),
        design=FakeDesign(plan_of(category_need("seating", "sofa"))),
        optimizer=FakeOptimizer(chosen(77)),
    )

    result = await coordinator.run(
        CustomerTurnInput(
            message="design around this", state=AgentStateV1(), context=CONTEXT
        )
    )

    assert len(parts["design"].requests) == 1
    assert result.grounding.deterministic_clarification is None


async def test_an_unverifiable_role_fails_closed_against_an_exclusion() -> None:
    """"Cannot show it conflicts" is not "shown not to conflict".

    A missing commerce category is unverified, not "any" - so it satisfies no
    exclusion. But locking the piece and handing it to the specialist as an
    anchor would be assuming a compatibility nobody established (M12F 2).
    """
    state = a_room(needs=(need(),), lines=(line(10),))
    coordinator, parts = _coordinator(
        anchored(removed=(DesignNeedCategoryMatch(commerce_category="seating"),)),
        capabilities=FakeCapabilities((("seating", "sofa"),)),
        hydration=cast(Any, UnclassifiedHydration((10, ANCHOR_ID))),
        design=FakeDesign(plan_of(category_need("seating", "sofa"))),
    )

    result = await coordinator.run(
        CustomerTurnInput(message="design around this", state=state, context=CONTEXT)
    )

    assert parts["design"].requests == []
    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.need_reason is DesignNeedFailureReason.PRESERVED_ROLE_EXCLUDED


async def test_the_anchor_and_preserved_cards_lock_in_one_transition() -> None:
    from app.schemas.agent_decision import DesignAnchorIntent
    from app.schemas.product_reference import PresentedOrdinal

    state = a_room(needs=(need(),), lines=(line(10),))
    decision = CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF,
        design_anchor=DesignAnchorIntent(reference=PresentedOrdinal(position=1)),
        design_revision=DesignRevisionIntent(
            preserved_items=(BundleItemOrdinal(ordinal=1),)
        ),
    )

    result, _ = await run(
        decision, state, available=(10, ANCHOR_ID), outcome=chosen(77)
    )

    locked = {
        item.product_id
        for item in room(result.state).bundle_items
        if item.status is BundleItemStatus.LOCKED
    }
    assert locked == {10, ANCHOR_ID}


async def test_a_preserve_is_not_written_when_the_anchor_contradicts() -> None:
    """The ordering the M12F restructure exists for.

    The preserved card is resolvable and the exclusion is unrelated to it; the
    contradiction is with the *anchor*, discovered later. If preservation were
    applied as it resolved, this card would already be locked by then.
    """
    from app.schemas.agent_decision import DesignAnchorIntent
    from app.schemas.product_reference import PresentedOrdinal

    state = a_room(
        needs=(need(), need("dining", "dining-table")),
        lines=(line(11, need_index=1), line(10, need_index=0)),
    )
    decision = CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF,
        design_anchor=DesignAnchorIntent(reference=PresentedOrdinal(position=1)),
        design_revision=DesignRevisionIntent(
            # The dining piece, which no exclusion forbids.
            preserved_items=(BundleItemOrdinal(ordinal=1),),
            # The anchor is a sofa, so this contradicts the anchor alone.
            removed_needs=(DesignNeedCategoryMatch(commerce_category="seating"),),
        ),
    )

    result, parts = await run(decision, state, available=(10, 11, ANCHOR_ID))

    assert parts["design"].requests == []
    assert all(
        item.status is BundleItemStatus.SUGGESTED
        for item in room(result.state).bundle_items
    ), "no preserve lock survives a contradiction found afterwards"
