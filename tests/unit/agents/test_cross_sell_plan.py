"""Nora's plan for what goes with a pick, beyond the first grid: the chips are
its kinds, a typed search for a planned kind leans to it, the piece just added
is remembered, and a plan for a piece on its own is kept for the next shopper
(CLAUDE.md 10.3)."""

from __future__ import annotations

from typing import Any

from app.agents.team import _reuse_saved_plan
from app.agents.tools.shopping import _leaning_to_the_plan
from app.agents.work import TurnWork
from app.repositories.cross_sell_plans import CrossSellPlanCache
from app.schemas.agent_state import (
    AgentStateV1,
    CrossSellKindPlan,
    CrossSellPlan,
    ProductInteractionState,
)
from app.schemas.agent_updates import AddItems, ProductInteractionUpdate, RemoveItems
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    ProductSearchRequest,
)
from app.schemas.query import ConstraintSemantics, ResolvedSearch
from app.services.agent_state import _interaction
from app.services.cross_sell import live_plan
from app.taxonomy.dimensions import DimensionRole

from tests.unit.agents.fakes import (
    CONTEXT,
    OFF_SCREEN,
    FakeHydration,
    agent_services,
    tool_context,
)

RUG = CrossSellKindPlan(kind="carpet", colours=("Ivory",), styles=("Modern",), size_ratio=1.2)
RUGS = ProductSearchRequest(commerce_category="decor", commerce_subcategory="carpet")


def _cart(*ids: int, plan: CrossSellPlan | None = None, last: int | None = None) -> AgentStateV1:
    return AgentStateV1(
        product_interaction=ProductInteractionState(
            selected_product_ids=ids, last_added_product_id=last
        ),
        cross_sell_plan=plan,
    )


# ── the piece just added ─────────────────────────────────────────────────────


def test_the_piece_just_added_is_remembered_even_when_added_again() -> None:
    start = ProductInteractionState(selected_product_ids=(1, 2), last_added_product_id=2)

    again = _interaction(start, ProductInteractionUpdate(selected_product_ids=AddItems(items=(1,))))
    gone = _interaction(
        again, ProductInteractionUpdate(selected_product_ids=RemoveItems(items=(1,)))
    )

    assert again.selected_product_ids == (1, 2), "a re-add keeps its place in the cart"
    assert again.last_added_product_id == 1, "but it is the piece just added"
    assert gone.last_added_product_id is None, "a piece that left the cart is not just added"


# ── a plan only while its piece is in the cart ──────────────────────────────


def test_a_plan_lapses_when_its_piece_leaves_the_cart() -> None:
    plan = CrossSellPlan(anchor_product_id=OFF_SCREEN, kinds=(RUG,))

    assert live_plan(_cart(OFF_SCREEN, plan=plan)) == plan
    assert live_plan(_cart(7, plan=plan)) is None


# ── a typed search for a planned kind ───────────────────────────────────────


def _resolved(request: ProductSearchRequest) -> ResolvedSearch:
    return ResolvedSearch(request=request, semantics=ConstraintSemantics())


async def test_a_typed_search_for_a_planned_kind_leans_to_the_plan() -> None:
    plan = CrossSellPlan(anchor_product_id=OFF_SCREEN, kinds=(RUG,))
    services = agent_services(hydration=FakeHydration(available=(OFF_SCREEN,)))

    lean, facts = await _leaning_to_the_plan(
        services,
        _cart(OFF_SCREEN, plan=plan, last=OFF_SCREEN),
        tool_context(services),
        _resolved(RUGS),
    )

    assert lean is not None
    assert (lean.colours, lean.styles) == (("Ivory",), ("Modern",))
    assert "leaning_to_the_plan_for" in facts


async def test_a_kind_she_did_not_plan_or_a_lapsed_plan_leans_nothing() -> None:
    plan = CrossSellPlan(anchor_product_id=OFF_SCREEN, kinds=(RUG,))
    services = agent_services(hydration=FakeHydration(available=(OFF_SCREEN,)))
    lamps = ProductSearchRequest(commerce_category="lighting", commerce_subcategory="floor-lamp")

    unplanned = await _leaning_to_the_plan(
        services, _cart(OFF_SCREEN, plan=plan), tool_context(services), _resolved(lamps)
    )
    lapsed = await _leaning_to_the_plan(
        services, _cart(7, plan=plan), tool_context(services), _resolved(RUGS)
    )

    assert unplanned == (None, {}) and lapsed == (None, {})


async def test_their_own_size_wins_over_her_size() -> None:
    from decimal import Decimal

    from app.schemas.dimensions import DimensionStatus, NormalisedDimensions

    from tests.unit.agents.fakes import _product

    class _SizedHydration(FakeHydration):
        async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[Any, ...]:
            return tuple(
                _product(p).model_copy(
                    update={
                        "dimensions": NormalisedDimensions(
                            status=DimensionStatus.NORMALISED,
                            length_cm=Decimal("90"),
                            width_cm=Decimal("220"),
                        )
                    }
                )
                for p in product_ids
            )

    plan = CrossSellPlan(anchor_product_id=OFF_SCREEN, kinds=(RUG,))
    services = agent_services(hydration=_SizedHydration())
    sized = RUGS.model_copy(
        update={
            "dimensions": (
                DimensionConstraint(
                    role=DimensionRole.LENGTH,
                    kind=DimensionConstraintKind.MAX,
                    max_cm=Decimal("200"),
                ),
            )
        }
    )

    hers, _ = await _leaning_to_the_plan(
        services,
        _cart(OFF_SCREEN, plan=plan, last=OFF_SCREEN),
        tool_context(services),
        _resolved(RUGS),
    )
    # Only the request is read; its strengths are query understanding's.
    with_their_size = ResolvedSearch.model_construct(request=sized, semantics=ConstraintSemantics())
    theirs, _ = await _leaning_to_the_plan(
        services,
        _cart(OFF_SCREEN, plan=plan, last=OFF_SCREEN),
        tool_context(services),
        with_their_size,
    )

    assert hers is not None and hers.size_target_cm == Decimal("264")
    assert theirs is not None and theirs.size_target_cm is None


# ── a plan kept for the next shopper ────────────────────────────────────────


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.data[key] = value


async def test_a_kept_plan_is_read_back_only_for_its_store() -> None:
    from app.schemas.retailer import RetailerContext

    cache = CrossSellPlanCache(_FakeRedis(), ttl_s=60, timeout_s=1, plan_version="v1")  # type: ignore[arg-type]

    await cache.save(CONTEXT, OFF_SCREEN, (RUG,))

    assert await cache.load(CONTEXT, OFF_SCREEN) == (RUG,)
    assert await cache.load(RetailerContext(store_id=CONTEXT.store_id + 1), OFF_SCREEN) is None


def _turn(cache: CrossSellPlanCache | None) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        context=SimpleNamespace(retailer=CONTEXT, services=SimpleNamespace(cross_sell_plans=cache))
    )


async def test_a_kept_plan_is_reused_for_the_piece_on_its_own() -> None:
    cache = CrossSellPlanCache(_FakeRedis(), ttl_s=60, timeout_s=1, plan_version="v1")  # type: ignore[arg-type]
    await cache.save(CONTEXT, OFF_SCREEN, (RUG,))

    alone = await _reuse_saved_plan(
        _turn(cache), TurnWork.starting(_cart(OFF_SCREEN, last=OFF_SCREEN))
    )
    beside_more = await _reuse_saved_plan(
        _turn(cache), TurnWork.starting(_cart(7, OFF_SCREEN, last=OFF_SCREEN))
    )

    assert alone is not None
    record, plan = alone
    assert plan == CrossSellPlan(anchor_product_id=OFF_SCREEN, kinds=(RUG,))
    assert record.tool == "cross_sell_advice" and "reused_plan" in record.facts
    assert beside_more is None, "with more in the cart, she plans around all of it"


async def test_nothing_is_reused_without_a_kept_plan() -> None:
    cache = CrossSellPlanCache(_FakeRedis(), ttl_s=60, timeout_s=1, plan_version="v1")  # type: ignore[arg-type]

    assert (
        await _reuse_saved_plan(_turn(cache), TurnWork.starting(_cart(OFF_SCREEN, last=OFF_SCREEN)))
        is None
    )
    assert (
        await _reuse_saved_plan(_turn(None), TurnWork.starting(_cart(OFF_SCREEN, last=OFF_SCREEN)))
        is None
    )


# ── what the review asked to keep out ───────────────────────────────────────


async def test_a_sorted_search_is_never_leaned_to_the_plan() -> None:
    from app.schemas.discovery import ProductSort

    plan = CrossSellPlan(anchor_product_id=OFF_SCREEN, kinds=(RUG,))
    services = agent_services(hydration=FakeHydration(available=(OFF_SCREEN,)))
    cheapest = RUGS.model_copy(update={"sort": ProductSort.PRICE_ASC})

    leaned = await _leaning_to_the_plan(
        services,
        _cart(OFF_SCREEN, plan=plan, last=OFF_SCREEN),
        tool_context(services),
        _resolved(cheapest),
    )

    assert leaned == (None, {}), "'the cheapest rugs' means the cheapest"


async def test_an_older_pieces_plan_does_not_lean_their_words() -> None:
    plan = CrossSellPlan(anchor_product_id=OFF_SCREEN, kinds=(RUG,))
    services = agent_services(hydration=FakeHydration(available=(OFF_SCREEN,)))

    moved_on = await _leaning_to_the_plan(
        services, _cart(OFF_SCREEN, 7, plan=plan, last=7), tool_context(services), _resolved(RUGS)
    )

    assert moved_on == (None, {})


async def test_a_shopper_with_taste_of_their_own_gets_a_plan_of_their_own() -> None:
    from app.schemas.agent_state import CustomerPreferenceState

    cache = CrossSellPlanCache(_FakeRedis(), ttl_s=60, timeout_s=1, plan_version="v1")  # type: ignore[arg-type]
    await cache.save(CONTEXT, OFF_SCREEN, (RUG,))
    with_taste = _cart(OFF_SCREEN, last=OFF_SCREEN).model_copy(
        update={"customer_preferences": CustomerPreferenceState(avoid_colours=("Grey",))}
    )

    assert await _reuse_saved_plan(_turn(cache), TurnWork.starting(with_taste)) is None


# ── the plan in every agent's brief, and the chips as buttons ───────────────


async def test_the_plan_reaches_the_brief_while_its_piece_is_the_newest() -> None:
    from app.agents.views import session_brief

    plan = CrossSellPlan(anchor_product_id=OFF_SCREEN, kinds=(RUG,))
    services = agent_services(hydration=FakeHydration(available=(OFF_SCREEN, 7)))

    newest = await session_brief(
        services, _cart(OFF_SCREEN, plan=plan, last=OFF_SCREEN), tool_context(services)
    )
    moved_on = await session_brief(
        services, _cart(OFF_SCREEN, 7, plan=plan, last=7), tool_context(services)
    )

    assert newest.designer_plan is not None
    rug = newest.designer_plan.kinds[0]
    assert (rug.kind, rug.colours, rug.styles, rug.sized_to_the_piece) == (
        "carpet",
        ("Ivory",),
        ("Modern",),
        True,
    )
    assert moved_on.designer_plan is None


def test_sara_is_told_a_planned_kind_needs_no_look_questions() -> None:
    from app.prompts.agents.v2 import agent_instructions
    from app.schemas.agent_persona import AgentPersona

    sara = " ".join(agent_instructions(AgentPersona.SARA).split())

    assert "A kind in the brief's designer_plan is answered already" in sara


def test_a_chip_naming_an_offered_kind_becomes_its_button() -> None:
    from app.agents.loop import json_facts
    from app.agents.screen import presentation
    from app.agents.work import ToolRecord, ToolStatus
    from app.schemas.agent_persona import AgentPersona
    from app.schemas.language import ReplyLanguage
    from app.schemas.text_choice import TextReplyChoice

    work = TurnWork.starting(AgentStateV1())
    work.record(
        ToolRecord(
            agent=AgentPersona.SALMAN,
            tool="what_goes_with",
            status=ToolStatus.OK,
            facts=json_facts({"kinds": [{"kind": "carpet", "family": "decor", "label": "rugs"}]}),
        )
    )
    chips = (
        TextReplyChoice(label="Rugs", value="Rugs"),
        TextReplyChoice(label="Design the whole room", value="Design the whole room"),
    )

    shown = presentation(work, AgentStateV1(), chips, ReplyLanguage.EN)

    assert shown is not None
    rugs, room = shown.choices
    assert rugs.product_action is not None
    assert (rugs.product_action.category, rugs.product_action.subcategory) == ("decor", "carpet")
    assert room.product_action is None, "any other chip stays words"
