"""What the customer is working toward, and choosing what goes with it."""

from __future__ import annotations

from typing import Any

from app.agents.views import session_brief
from app.agents.work import TurnWork
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1, GoalKind
from app.services.agent_state import set_goal

from tests.unit.agents.fakes import (
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


class _Looks:
    async def of(self, subcategory: str, context: Any) -> Any:
        from app.schemas.design import StockedLook

        return StockedLook(
            commerce_subcategory=subcategory, colours=("Beige", "Ruby"), styles=("Modern",)
        )


async def _matched(persona: AgentPersona, direction: dict[str, Any]) -> tuple[Any, FakePipeline]:
    pipeline = FakePipeline(ids=(10, 11))
    services = agent_services(
        pipeline=pipeline, hydration=FakeHydration(available=(42, 10, 11)), looks=_Looks()
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
    tool = LOOP._registry.get("search_products")
    assert tool is not None and tool.handler is not None
    work = TurnWork.starting(added.state)
    work.working = persona
    search = {"commerce_category": "seating", "commerce_subcategory": "sofa"}
    outcome = await tool.handler(
        tool.args.model_validate({"search": search, "match_goal": 1, "direction": direction}),
        tool_context(services),
        work,
    )
    return outcome, pipeline


async def test_a_search_matched_with_a_goal_leans_noras_way() -> None:
    direction = {
        "colours": ["Beige", "Olive"],
        "styles": ["Modern"],
        "avoid_colours": ["Ruby"],
        "why": "Warm neutrals tie the pair.",
    }

    outcome, pipeline = await _matched(AgentPersona.NORA, direction)

    lean = pipeline.calls[-1].lean
    # Olive is not stocked for the kind: left out, never matched to something near.
    assert lean.colours == ("Beige",) and lean.avoid_colours == ("Ruby",)
    assert outcome.facts["direction"]["why"] == "Warm neutrals tie the pair."
    assert "direction_dropped" in outcome.facts


async def test_only_nora_gives_a_direction() -> None:
    outcome, pipeline = await _matched(
        AgentPersona.SARA, {"colours": ["Beige"], "why": "Warm neutrals tie the pair."}
    )

    # Refused before anything is searched, so no unmatched cards replace the screen.
    assert outcome.code == "matching_is_noras" and outcome.facts["hand_to"] == "nora"
    assert pipeline.calls == []


async def test_a_direction_states_no_figure() -> None:
    outcome, _ = await _matched(
        AgentPersona.NORA, {"colours": ["Beige"], "why": "Keep it 20 cm shorter."}
    )

    assert outcome.code == "direction_why_states_a_figure"


def test_a_saved_session_from_before_goals_still_reads() -> None:
    assert AgentStateV1.model_validate({"schema_version": "agent_state_v5"}).goals == ()
