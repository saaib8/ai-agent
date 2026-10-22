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
from typing import Any

import pytest
from app.core.exceptions import LLMResponseInvalidError
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    DesignRevisionIntent,
)
from app.schemas.agent_state import AgentStateV1, BundleItemStatus, RoomProjectState
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
)
from app.schemas.bundle_reference import (
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
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.retailer import RetailerContext
from app.services.agent_state import apply_update
from app.services.design_revision import (
    conflicting_role,
    deduplicated_exclusions,
    project_current_plan,
)
from pydantic import ValidationError

STOCKED_WITH_RANGE = 12
"""A capability depth that is not the thing under test.

Every capability carries how many products back it. These suites are about
which *types* a plan may use, so they give each one an unremarkable range -
enough that nothing is refused for being thin, and a number no assertion here
reads.
"""

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


# ── preserving pieces ═══════════════════════════════════════════════════════


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
                commerce_category=category,
                commerce_subcategory=subcategory,
                active_product_count=STOCKED_WITH_RANGE,
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


# ── the anchor is a hard constraint too (M12F 2) ════════════════════════════
