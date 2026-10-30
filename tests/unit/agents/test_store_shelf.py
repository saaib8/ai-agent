"""The store shelf Nora designs from, the cache behind it, and the seat rule."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.agents.views import session_brief
from app.agents.work import TurnWork
from app.core.exceptions import TaxonomyConfigurationError
from app.repositories.catalog_cache import OverviewCache
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.catalog_overview import CatalogOverview, SeatingSpread, SubcategoryShelf
from app.services.agent_state import remember_placements
from app.services.catalog_capability import CatalogCapabilityService
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics
from redis.exceptions import ConnectionError as RedisConnectionError

from tests.unit.agents.fakes import (
    CONTEXT,
    TAXONOMY,
    FakeHydration,
    FakePipeline,
    agent_services,
    real_loop,
    tool_context,
)

LOOP = real_loop()
SEATING = load_seating_semantics(taxonomy=TAXONOMY)


def _shelf(category: str, kind: str, count: int, **extra: Any) -> SubcategoryShelf:
    return SubcategoryShelf(
        commerce_category=category,
        commerce_subcategory=kind,
        active_count=count,
        price_minimum=Decimal("990"),
        price_maximum=Decimal("8000"),
        **extra,
    )


OVERVIEW = CatalogOverview(
    store_id=CONTEXT.store_id,
    currency="SAR",
    shelves=(
        _shelf(
            "seating",
            "sofa",
            40,
            seating=SeatingSpread(known_count=30, minimum=2, maximum=4),
            colours=("Beige", "Grey"),
            styles=("Modern",),
            long_and_shallow=True,
        ),
        _shelf("seating", "chair", 8, implied_seats=1, colours=("Olive",)),
        _shelf("seating", "chaise-lounge", 2),
        _shelf("decor", "carpet", 12),
    ),
)


class _Shelves:
    def __init__(self, overview: CatalogOverview = OVERVIEW) -> None:
        self.overview_calls = 0
        self._overview = overview

    async def overview(self, context: Any) -> CatalogOverview:
        self.overview_calls += 1
        return self._overview


def _services(**extra: Any) -> Any:
    return agent_services(
        capabilities=_Shelves(),  # type: ignore[arg-type]
        seating=SEATING,
        room_pieces=load_room_pieces(taxonomy=TAXONOMY, seating=SEATING),
        **extra,
    )


async def _shelf_call(args: dict[str, Any], services: Any) -> Any:
    tool = LOOP._registry.get("store_shelf")
    assert tool is not None and tool.handler is not None
    return await tool.handler(
        tool.args.model_validate(args), tool_context(services), TurnWork.starting(AgentStateV1())
    )


async def test_the_shelf_lists_families_with_ranges_and_never_a_count() -> None:
    outcome = await _shelf_call({"room_type": "living_room"}, _services())

    seating = next(f for f in outcome.facts["families"] if f["label"] == "seating")
    sofa = seating["kinds"][0]
    assert sofa["kind"] == "sofa" and sofa["in_this_room"] is True
    assert (sofa["price_from"], sofa["price_to"]) == ("990", "8000")
    # How wide the choice is, as a word; how many the store holds is never said.
    assert sofa["range_depth"] == "many"
    assert next(k for k in seating["kinds"] if k["kind"] == "chaise-lounge")["range_depth"] == "few"
    assert "40" not in str(outcome.facts) and "active_count" not in str(outcome.facts)
    assert outcome.facts["currency"] == "SAR"


async def test_a_kind_outside_the_vocabulary_is_never_called_unstocked() -> None:
    outcome = await _shelf_call({"kinds": ["sofa", "couch"]}, _services())

    assert outcome.facts["not_approved_kinds"] == ["couch"]
    assert "not_stocked" not in outcome.facts


def test_the_room_is_a_closed_choice() -> None:
    import pydantic

    tool = LOOP._registry.get("store_shelf")
    assert tool is not None
    with pytest.raises(pydantic.ValidationError):
        tool.args.model_validate({"room_type": "living room"})


async def test_a_close_look_gives_colours_styles_and_seats() -> None:
    outcome = await _shelf_call({"kinds": ["sofa", "chair", "chaise-lounge", "bed"]}, _services())

    sofa, chair, chaise = outcome.facts["kinds"]
    assert sofa["colours"] == ["Beige", "Grey"] and sofa["styles"] == ["Modern"]
    assert (sofa["seats_from"], sofa["seats_to"]) == (2, 4) and sofa["long_and_shallow"]
    assert chair["seats_each"] == 1
    # A room may hold one; it never fills a seat - said as not counted, never 0.
    assert chaise["seats_counted"] is False and "seats_each" not in chaise
    assert outcome.facts["not_stocked"] == ["bed"]


def test_only_nora_and_salman_read_the_shelf() -> None:
    tool = LOOP._registry.get("store_shelf")
    assert tool is not None
    assert tool.owners == frozenset({AgentPersona.NORA, AgentPersona.SALMAN})


def test_the_reviewed_seat_rule_never_counts_a_chaise_or_recliner() -> None:
    assert SEATING.never_counted_types() == ("chaise-lounge", "recliner")
    assert not SEATING.counts_as_seats("recliner") and SEATING.counts_as_seats("sofa")


def test_a_type_cannot_both_seat_and_never_count(tmp_path: Path) -> None:
    source = tmp_path / "seating.yaml"
    source.write_text(
        "version: t\nimplied_capacity: {chair: 1}\nmulti_seat: [sofa]\n"
        "never_counts_as_seats: [chair]\n"
    )

    with pytest.raises(TaxonomyConfigurationError):
        load_seating_semantics(source, taxonomy=TAXONOMY)


# ── the cache ───────────────────────────────────────────────────────────────


class _Redis:
    def __init__(self, broken: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.broken = broken

    async def get(self, key: str) -> str | None:
        if self.broken:
            raise RedisConnectionError("down")
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int) -> None:
        if self.broken:
            raise RedisConnectionError("down")
        self.values[key] = value


class _Repository:
    def __init__(self) -> None:
        self.scans = 0

    async def catalog_overview(self, context: Any, size: Any) -> list[Any]:
        self.scans += 1
        return []


def _service(redis: _Redis) -> tuple[CatalogCapabilityService, _Repository]:
    repository = _Repository()
    cache = OverviewCache(redis, ttl_s=300, timeout_s=1)  # type: ignore[arg-type]
    service = CatalogCapabilityService(repository, TAXONOMY, SEATING, cache=cache)  # type: ignore[arg-type]
    return service, repository


async def test_the_overview_is_scanned_once_then_read_from_the_cache() -> None:
    redis = _Redis()
    service, repository = _service(redis)

    first = await service.overview(CONTEXT)
    second = await service.overview(CONTEXT)

    assert repository.scans == 1 and first == second
    (key,) = redis.values
    assert key.startswith(f"zory:store:{CONTEXT.store_id}:catalog_overview:")


async def test_an_unreachable_cache_reads_the_catalog_instead() -> None:
    service, repository = _service(_Redis(broken=True))

    await service.overview(CONTEXT)
    await service.overview(CONTEXT)

    assert repository.scans == 2


async def test_another_stores_summary_is_never_read_back() -> None:
    redis = _Redis()
    service, repository = _service(redis)
    await service.overview(CONTEXT)
    (key,) = redis.values
    redis.values[key] = CatalogOverview(store_id=CONTEXT.store_id + 1).model_dump_json()

    await service.overview(CONTEXT)

    assert repository.scans == 2


# ── Phase 1 review follow-ups ───────────────────────────────────────────────


async def test_noras_placements_reach_every_agent_in_the_brief() -> None:
    from app.schemas.design import Placement

    services = agent_services()
    tool = LOOP._registry.get("note_room")
    assert tool is not None and tool.handler is not None
    noted = await tool.handler(
        tool.args.model_validate({"room_kind": "living_room"}),
        tool_context(services),
        TurnWork.starting(AgentStateV1()),
    )
    state = remember_placements(
        noted.state, (Placement(piece="the sofa", wall="TV wall", note="facing the screen"),)
    )

    brief = (await session_brief(services, state, tool_context(services))).context()

    assert brief["room"]["placements"] == [
        {"piece": "the sofa", "wall": "TV wall", "note": "facing the screen"}
    ]


async def test_a_direction_whose_looks_are_unknown_is_said_so_not_unstocked() -> None:
    pipeline = FakePipeline(ids=(10, 11))
    services = agent_services(pipeline=pipeline, hydration=FakeHydration(available=(42, 10, 11)))
    tool = LOOP._registry.get("search_products")
    assert tool is not None and tool.handler is not None
    work = TurnWork.starting(AgentStateV1())
    work.working = AgentPersona.NORA
    args = {
        "search": {"commerce_category": "decor", "commerce_subcategory": "carpet"},
        "match_with": [{"kind": "presented_ordinal", "position": 1}],
        "direction": {"colours": ["Beige"], "why": "Warm under the sofa."},
    }

    outcome = await tool.handler(tool.args.model_validate(args), tool_context(services), work)

    assert outcome.facts["direction"] == "looks_unknown"
    assert outcome.facts["matched_with"]


async def test_the_room_guide_marks_kinds_but_never_puts_one_first() -> None:
    outcome = await _shelf_call({"room_type": "living_room"}, _services())

    seating = next(f for f in outcome.facts["families"] if f["label"] == "seating")
    # The widest choice first; the guide's mark does not move a kind up or down.
    assert [k["kind"] for k in seating["kinds"]] == ["sofa", "chair", "chaise-lounge"]
    assert all(k.get("in_this_room") in (True, None) for k in seating["kinds"])


async def test_nora_plans_from_the_kinds_the_store_stocks_names_only() -> None:
    from app.agents.tools.shelf import stocked_kinds

    stocks = (await stocked_kinds(_services(), CONTEXT))["store_stocks"]

    assert stocks == {"decor": ["carpet"], "seating": ["sofa", "chair", "chaise-lounge"]}


async def test_ask_about_gives_what_the_store_carries_of_the_kind() -> None:
    """The answers Sara offers come from stock: the kind's colours and seats,
    or the family's stocked kinds before one is chosen (CLAUDE.md 10.2)."""
    tool = LOOP._registry.get("ask_about")
    assert tool is not None and tool.handler is not None

    async def ask(search: dict[str, Any]) -> dict[str, Any]:
        outcome = await tool.handler(
            tool.args.model_validate({"search": search}),
            tool_context(_services()),
            TurnWork.starting(AgentStateV1()),
        )
        return outcome.facts

    sofa = await ask({"commerce_category": "seating", "commerce_subcategory": "sofa"})
    assert sofa["this_store_has"]["colours"] == ["Beige", "Grey"]
    assert (sofa["this_store_has"]["seats_from"], sofa["this_store_has"]["seats_to"]) == (2, 4)

    recliner = await ask({"commerce_category": "seating", "commerce_subcategory": "recliner"})
    assert "this_store_has" not in recliner and recliner["not_stocked"] == "recliner"

    seating = await ask({"commerce_category": "seating"})
    assert [k["kind"] for k in seating["kinds_stocked"]] == ["sofa", "chair", "chaise-lounge"]
    assert all("price_from" not in k for k in seating["kinds_stocked"])
    assert "price_to" not in sofa["this_store_has"], "a shelf price is never an opening figure"
