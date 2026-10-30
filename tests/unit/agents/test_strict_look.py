"""A look they chose is kept strictly, and the agent knows when it has run out."""

from __future__ import annotations

from typing import Any

from app.agents.work import TurnWork
from app.schemas.agent_state import AgentStateV1

from tests.unit.agents.fakes import (
    FakeHydration,
    FakePipeline,
    agent_services,
    real_loop,
    tool_context,
)

LOOP = real_loop()
SOFAS = {"commerce_category": "seating", "commerce_subcategory": "sofa"}


async def _call(name: str, args: dict[str, Any], state: AgentStateV1, services: Any) -> Any:
    tool = LOOP._registry.get(name)
    assert tool is not None and tool.handler is not None
    return await tool.handler(
        tool.args.model_validate(args), tool_context(services), TurnWork.starting(state)
    )


async def test_more_like_this_keeps_its_colour_and_styles_as_requirements() -> None:
    pipeline = FakePipeline(ids=(10, 11))
    services = agent_services(pipeline=pipeline, hydration=FakeHydration(available=(10, 11, 42)))
    shown = await _call("search_products", {"search": SOFAS}, AgentStateV1(), services)

    await _call(
        "more_like_this",
        {"which": {"kind": "presented_ordinal", "position": 1}},
        shown.state,
        services,
    )

    request = pipeline.calls[-1].request
    assert request.colors_any_of == ("Beige",)
    assert request.styles_all_of == ("Modern",)


async def test_a_search_says_whether_anything_more_matches() -> None:
    services = agent_services(
        pipeline=FakePipeline(ids=(10, 11)), hydration=FakeHydration(available=(10, 11))
    )

    outcome = await _call("search_products", {"search": SOFAS}, AgentStateV1(), services)

    assert outcome.facts["more_after_this_page"] is False


async def test_colours_said_plainly_reach_the_search_with_their_strength() -> None:
    pipeline = FakePipeline(ids=(10,))
    services = agent_services(pipeline=pipeline, hydration=FakeHydration(available=(10,)))
    search = {**SOFAS, "colours_required": ["Beige"], "styles_wanted": ["Modern"]}

    outcome = await _call("search_products", {"search": search}, AgentStateV1(), services)

    ran = pipeline.calls[-1]
    assert ran.request.colors_any_of == ("Beige",)
    assert [(p.canonical_value, str(p.strength)) for p in ran.semantic_preferences] == [
        ("Modern", "preferred")
    ]
    assert outcome.facts["searched_for"]["colours_required"] == ["Beige"]
    assert outcome.facts["searched_for"]["styles_wanted"] == ["Modern"]


async def test_a_search_without_the_colour_says_so() -> None:
    services = agent_services(
        pipeline=FakePipeline(ids=(10,)), hydration=FakeHydration(available=(10,))
    )

    outcome = await _call("search_products", {"search": SOFAS}, AgentStateV1(), services)

    assert outcome.facts["searched_for"] == {"kind": "sofa"}
