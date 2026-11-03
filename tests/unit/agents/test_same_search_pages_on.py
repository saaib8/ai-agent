"""The same search stated again is its next page, never its first page again.

"Show me some more" answered with a fresh search_products instead of
more_results used to start from the top, so the cards just shown came back.
A search whose criteria match the one on screen now leaves out what was shown;
only a change of criteria starts a fresh set (CLAUDE.md 13.5).
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

from app.agents.operations.search import SearchRunner
from app.agents.work import TurnWork
from app.schemas.agent_state import AgentStateV1
from app.schemas.query import RankingLean
from app.services.refinement_composer import SearchRefinementComposer
from app.taxonomy.seating import load_seating_semantics

from tests.unit.agents.fakes import (
    ATTRIBUTES,
    DIMENSIONS,
    TAXONOMY,
    FakeHydration,
    FakePipeline,
    agent_services,
    real_loop,
    tool_context,
)

LOOP = real_loop()
CANVASES = {"commerce_category": "decor", "commerce_subcategory": "art-canvas", "weight_max": "3"}


async def _call(name: str, args: dict[str, Any], state: AgentStateV1, services: Any) -> Any:
    tool = LOOP._registry.get(name)
    assert tool is not None and tool.handler is not None
    return await tool.handler(
        tool.args.model_validate(args), tool_context(services), TurnWork.starting(state)
    )


def _services() -> tuple[Any, FakePipeline]:
    pipeline = FakePipeline(ids=(10, 11))
    return agent_services(pipeline=pipeline, hydration=FakeHydration(available=(10, 11))), pipeline


async def test_the_same_search_again_leaves_out_what_was_shown() -> None:
    services, pipeline = _services()
    first = await _call("search_products", {"search": CANVASES}, AgentStateV1(), services)

    again = await _call("search_products", {"search": CANVASES}, first.state, services)

    assert set(pipeline.calls[-1].request.exclude_product_ids) == {10, 11}
    assert again.facts["next_page_of_the_search_on_screen"] is True


async def test_a_changed_search_starts_a_fresh_set() -> None:
    services, pipeline = _services()
    first = await _call("search_products", {"search": CANVASES}, AgentStateV1(), services)

    lighter = {**CANVASES, "weight_max": "2"}
    changed = await _call("search_products", {"search": lighter}, first.state, services)

    assert pipeline.calls[-1].request.exclude_product_ids == ()
    assert "next_page_of_the_search_on_screen" not in changed.facts


async def test_a_first_search_is_never_a_next_page() -> None:
    services, pipeline = _services()

    first = await _call("search_products", {"search": CANVASES}, AgentStateV1(), services)

    assert pipeline.calls[-1].request.exclude_product_ids == ()
    assert "next_page_of_the_search_on_screen" not in first.facts


async def test_a_search_ordered_anew_is_a_fresh_set_not_the_old_ones_page() -> None:
    """Nora matching a piece, or a search at the designer's size, leans the
    same criteria another way: its own first page, never the plain one's next."""
    services, pipeline = _services()
    first = await _call("search_products", {"search": CANVASES}, AgentStateV1(), services)
    sized = pipeline.calls[-1].model_copy(update={"lean": RankingLean(size_target_cm=Decimal(240))})

    run = await services.search.new(
        sized, first.state, tool_context(services).retailer, alongside=False
    )

    assert not run.continued
    assert pipeline.calls[-1].request.exclude_product_ids == ()
    assert pipeline.calls[-1].lean.size_target_cm == Decimal(240)


async def test_every_page_of_sofas_shows_sets_and_sectionals_beside_them() -> None:
    pipeline = FakePipeline(ids=(10, 11))
    services = dataclasses.replace(
        agent_services(pipeline=pipeline, hydration=FakeHydration(available=(10, 11))),
        search=SearchRunner(
            pipeline,  # type: ignore[arg-type]
            SearchRefinementComposer(ATTRIBUTES, DIMENSIONS, None),
            load_seating_semantics(taxonomy=TAXONOMY),
        ),
    )
    sofas = {"commerce_category": "seating", "commerce_subcategory": "sofa"}
    first = await _call("search_products", {"search": sofas}, AgentStateV1(), services)
    beside = pipeline.calls[-1].request.alongside_subcategories

    await _call("search_products", {"search": sofas}, first.state, services)
    assert pipeline.calls[-1].request.alongside_subcategories == beside != ()

    await _call("more_results", {}, first.state, services)
    assert pipeline.calls[-1].request.alongside_subcategories == beside
