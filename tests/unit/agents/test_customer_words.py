"""The customer's own words reach the ranking, whatever the agent's search kept."""

from __future__ import annotations

from typing import Any

from app.agents.work import TurnWork
from app.schemas.agent_state import AgentStateV1
from app.services.query_document import build_query_document, has_semantic_intent, ranking_words

from tests.unit.agents.fakes import (
    FakeHydration,
    FakePipeline,
    agent_services,
    real_loop,
    tool_context,
)

LOOP = real_loop()
BEDS = {"commerce_category": "bedroom", "commerce_subcategory": "bed"}


async def _call(
    name: str, args: dict[str, Any], state: AgentStateV1, services: Any, said: str | None
) -> Any:
    tool = LOOP._registry.get(name)
    assert tool is not None and tool.handler is not None
    return await tool.handler(
        tool.args.model_validate(args),
        tool_context(services, customer_said=said),
        TurnWork.starting(state),
    )


def _services() -> tuple[Any, FakePipeline]:
    pipeline = FakePipeline(ids=(10, 11))
    return agent_services(
        pipeline=pipeline, hydration=FakeHydration(available=(10, 11, 42))
    ), pipeline


def test_their_words_lose_the_figures_and_keep_the_meaning() -> None:
    assert (
        ranking_words("show me beige king beds under 2000 SAR") == "show me beige king beds under"
    )
    assert ranking_words("220 cm wide velvet sofa") == "wide velvet sofa"
    assert ranking_words("أبي كنبة بيج ب ٣٠٠٠ ريال") == "أبي كنبة بيج ب"
    assert ranking_words("1500") is None


async def test_a_search_ranks_on_their_words_even_when_the_agent_left_them_out() -> None:
    services, pipeline = _services()

    await _call("search_products", {"search": BEDS}, AgentStateV1(), services, "beige king beds")

    resolved = pipeline.calls[-1]
    assert resolved.customer_words == "beige king beds"
    assert has_semantic_intent(resolved)
    assert build_query_document(resolved).endswith("Customer said: beige king beds")


async def test_a_tap_gives_no_words() -> None:
    services, pipeline = _services()

    await _call("search_products", {"search": BEDS}, AgentStateV1(), services, None)

    assert pipeline.calls[-1].customer_words is None


async def test_the_next_page_ranks_on_the_same_words() -> None:
    services, pipeline = _services()
    first = await _call(
        "search_products", {"search": BEDS}, AgentStateV1(), services, "beige king beds"
    )

    await _call("more_results", {}, first.state, services, "show me more")

    assert pipeline.calls[-1].customer_words == "beige king beds"


async def test_a_search_of_ours_never_takes_their_message() -> None:
    services, pipeline = _services()
    first = await _call("search_products", {"search": BEDS}, AgentStateV1(), services, "beds")

    await _call(
        "more_like_this",
        {"which": {"kind": "presented_ordinal", "position": 1}},
        first.state,
        services,
        "more like the first one",
    )

    assert pipeline.calls[-1].customer_words is None
