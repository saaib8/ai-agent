"""What the customer is working toward, and choosing what goes with it."""

from __future__ import annotations

from typing import Any

from app.agents.views import session_brief
from app.agents.work import TurnWork
from app.schemas.agent_state import AgentStateV1, GoalKind
from app.schemas.design import (
    DesignCategoryNeed,
    DesignDirection,
    DesignGuidance,
    DesignPriority,
    DesignTask,
    GuidanceTopic,
    InteriorDesignResult,
)
from app.services.agent_state import set_goal

from tests.unit.agents.fakes import (
    FakeDesign,
    FakeHydration,
    FakePipeline,
    agent_services,
    real_loop,
    tool_context,
)

LOOP = real_loop()


async def _call(name: str, args: dict[str, Any], state: AgentStateV1, services: Any) -> Any:
    tool = LOOP._registry.get(name)
    assert tool is not None and tool.handler is not None
    return await tool.handler(
        tool.args.model_validate(args), tool_context(services), TurnWork.starting(state)
    )


async def test_a_seating_goal_counts_the_seats_its_sofas_give() -> None:
    services = agent_services(hydration=FakeHydration(available=(42,)))
    opened = await _call(
        "update_goal",
        {"action": "open", "kind": "seating", "summary": "seat 10 in the living room", "seats": 10},
        AgentStateV1(),
        services,
    )
    added = await _call(
        "add_to_cart",
        {"items": [{"which": {"kind": "presented_ordinal", "position": 1}}], "for_goal": 1},
        opened.state,
        services,
    )

    brief = (await session_brief(services, added.state, tool_context(services))).context()

    goal = brief["goals"][0]
    assert goal["kind"] == "seating" and goal["seats"] == 10
    assert [m["pick"] for m in goal["members"]] == [1]
    # The fake sofa records no seats: nothing is counted that nobody recorded.
    assert goal["seats_covered"] == 0 and goal["seats_left"] == 10


async def test_a_goal_is_closed_by_the_agent_never_by_code() -> None:
    state, _ = set_goal(
        AgentStateV1(), goal=None, kind=GoalKind.PIECE, summary="a rug for the sofa"
    )

    done = await _call("update_goal", {"action": "done", "goal": 1}, state, agent_services())

    assert done.state.goals[0].status == "done"


async def test_a_search_matched_with_a_goal_leans_the_designers_way() -> None:
    direction = DesignDirection(colours=("Beige",), styles=("Modern",), avoid_colours=("Red",))
    design = FakeDesign(
        result=InteriorDesignResult(
            guidance=(
                DesignGuidance(topic=GuidanceTopic.COLOR, summary="Warm neutrals tie the pair."),
            ),
            needs=(
                DesignCategoryNeed(
                    commerce_category="seating",
                    commerce_subcategory="sofa",
                    priority=DesignPriority.REQUIRED,
                    direction=direction,
                ),
            ),
        )
    )
    pipeline = FakePipeline(ids=(10, 11))
    services = agent_services(
        pipeline=pipeline, design=design, hydration=FakeHydration(available=(42, 10, 11))
    )
    state, _ = set_goal(
        AgentStateV1(), goal=None, kind=GoalKind.SEATING, summary="seat 10", seats=10
    )
    added = await _call(
        "add_to_cart",
        {"items": [{"which": {"kind": "presented_ordinal", "position": 1}}], "for_goal": 1},
        state,
        services,
    )
    search = {"commerce_category": "seating", "commerce_subcategory": "sofa"}

    outcome = await _call(
        "search_products", {"search": search, "match_goal": 1}, added.state, services
    )

    asked = design.requests[-1]
    assert asked.task is DesignTask.MATCH and asked.match_kind.commerce_subcategory == "sofa"
    assert len(asked.anchors) == 1
    lean = pipeline.calls[-1].lean
    assert lean.colours == ("Beige",) and lean.avoid_colours == ("Red",)
    assert outcome.facts["direction"]["why"] == ["Warm neutrals tie the pair."]


async def test_a_search_matched_with_nothing_asks_no_designer() -> None:
    design = FakeDesign()
    services = agent_services(design=design)
    search = {"commerce_category": "seating", "commerce_subcategory": "sofa"}

    await _call("search_products", {"search": search}, AgentStateV1(), services)

    assert design.requests == []


def test_a_saved_session_from_before_goals_still_reads() -> None:
    assert AgentStateV1.model_validate({"schema_version": "agent_state_v5"}).goals == ()


async def test_the_specialist_keeps_to_the_kind_being_chosen() -> None:
    from app.schemas.design import InteriorDesignRequest, MatchKind, StockedLook
    from app.services.interior_design import InteriorDesignAgent

    from tests.unit.agents.fakes import TAXONOMY, _product

    class Client:
        model = "test-model"

        async def parse(self, **kwargs: Any) -> Any:
            return InteriorDesignResult(
                needs=(
                    DesignCategoryNeed(
                        commerce_category="decor",
                        commerce_subcategory="carpet",
                        priority=DesignPriority.OPTIONAL,
                        direction=DesignDirection(colours=("Beige", "Purple"), size_ratio=0.8),
                    ),
                )
            )

    agent = InteriorDesignAgent(Client(), TAXONOMY)  # type: ignore[arg-type]
    from app.services.design_facts import project_anchors

    from tests.unit.agents.fakes import DIMENSIONS

    result = await agent.plan(
        InteriorDesignRequest(
            task=DesignTask.MATCH,
            match_kind=MatchKind(commerce_category="seating", commerce_subcategory="sofa"),
            anchors=project_anchors((_product(1),), dimensions=DIMENSIONS),
            catalog_capabilities=await _capabilities(),
            stocked_looks=(StockedLook(commerce_subcategory="sofa", colours=("Beige",)),),
        )
    )

    need = result.needs[0]
    assert need.commerce_subcategory == "sofa"
    assert need.direction is not None and need.direction.colours == ("Beige",)
    assert need.direction.size_ratio == 0.8


async def _capabilities() -> Any:
    from tests.unit.agents.fakes import FakeCapabilities

    return await FakeCapabilities().capabilities(tool_context().retailer)
