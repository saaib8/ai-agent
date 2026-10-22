"""The right drawer, refusals that say what is right, and a screen that shows
only what the agent presents."""

from __future__ import annotations

from typing import Any

from app.agents.context import ToolContext
from app.agents.loop import refusal_facts
from app.agents.screen import presentation
from app.agents.trace import TurnTrace
from app.agents.work import TurnWork
from app.core.exceptions import UnknownCommerceSubcategoryError
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.language import ReplyLanguage
from app.services.agent_state import keep_screen

from tests.unit.agents.fakes import (
    FakeHydration,
    FakePipeline,
    agent_services,
    real_loop,
    tool_context,
)

LOOP = real_loop()


async def _call(name: str, args: dict[str, Any], work: TurnWork, services: Any) -> Any:
    """A tool run through the loop, as an agent's call is - refusals included."""
    tool = LOOP._registry.get(name)
    assert tool is not None
    return await LOOP.execute(
        AgentPersona.SARA,
        tool,
        tool.args.model_validate(args),
        "c1",
        work,
        tool_context(services),
        TurnTrace(store_id=50, path="typed"),
    )


async def test_a_type_named_under_the_wrong_family_finds_its_own() -> None:
    pipeline = FakePipeline(ids=(10,))
    services = agent_services(pipeline=pipeline, hydration=FakeHydration(available=(10,)))
    work = TurnWork.starting(AgentStateV1())
    search = {"commerce_category": "bedroom", "commerce_subcategory": "nightstand"}

    answer = await _call("search_products", {"search": {**search}}, work, services)

    assert '"status": "ok"' in str(answer.content)
    request = pipeline.calls[-1].request
    assert (request.commerce_category, request.commerce_subcategory) == ("tables", "nightstand")


def test_a_refusal_says_what_is_right() -> None:
    context = ToolContext(retailer=tool_context().retailer, services=agent_services())
    refused = UnknownCommerceSubcategoryError(
        category="bedroom", subcategory="nightstand", product_id=42
    )

    facts = refusal_facts(refused, context)

    assert facts == {"category": "bedroom", "subcategory": "nightstand", "belongs_to": "tables"}


async def test_a_list_the_agent_rejects_never_reaches_the_screen() -> None:
    services = agent_services(
        pipeline=FakePipeline(ids=(10, 11)), hydration=FakeHydration(available=(10, 11))
    )
    work = TurnWork.starting(AgentStateV1())
    search = {"commerce_category": "bedroom", "commerce_subcategory": "bed"}
    await _call("search_products", {"search": search}, work, services)

    shown = presentation(work, work.staged, (), ReplyLanguage.EN, "unchanged")
    kept = keep_screen(work.staged, work.pre_turn)

    assert shown is None or not shown.products
    assert work.staged.product_interaction.presented_product_ids == (10, 11)
    assert kept.product_interaction.presented_product_ids == ()
    assert kept.active_search is None
