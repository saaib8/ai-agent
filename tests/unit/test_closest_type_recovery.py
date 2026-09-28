"""A type the store carries none of, recovered by offering the closest it does.

"A candle" at a shop that stocks candlesticks, "a recliner" where it stocks
lounge chairs: instead of a dead end, the closest stocked type is searched -
everything else the customer asked kept - and the reply is told which type they
asked for so it can say "we don't carry that, but here's the nearest thing".

Generic across categories, and honest at its edges: a type the store *does*
stock (empty only because of filters), a family it stocks nothing in, or a
picker that finds nothing close all keep the plain zero result.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.catalog_overview import CatalogOverview, SubcategoryShelf
from app.schemas.discovery import ProductSearchRequest
from app.services.response_view import route_response

from tests.unit.test_turn_coordinator import (
    FakeCapabilities,
    FakePipeline,
    _coordinator,
    _resolved,
    _state,
    _turn,
)


def _shelf(category: str, subcategory: str, price: str = "50") -> SubcategoryShelf:
    return SubcategoryShelf(
        commerce_category=category,
        commerce_subcategory=subcategory,
        active_count=5,
        price_minimum=Decimal(price),
        price_maximum=Decimal("9000"),
        colours=(),
    )


# Store 50-shaped: decor stocks candlesticks and vases but no candles; seating
# stocks chairs and lounge chairs but no recliners; fitness is stocked nowhere.
OVERVIEW = CatalogOverview(
    store_id=50,
    currency="SAR",
    shelves=(
        _shelf("decor", "candlestick"),
        _shelf("decor", "vase", "80"),
        _shelf("decor", "mirror", "200"),
        _shelf("seating", "chair"),
        _shelf("seating", "lounge-chair", "300"),
        _shelf("seating", "sofa", "990"),
    ),
)


class Catalog(FakeCapabilities):
    async def overview(self, context: Any) -> CatalogOverview:
        return OVERVIEW


class ByType(FakePipeline):
    """Finds products only for the subcategories named, recording each search."""

    def __init__(self, found: dict[str, tuple[int, ...]]) -> None:
        super().__init__()
        self.found = found

    async def execute(self, resolved: Any, context: Any, **kwargs: Any) -> Any:
        self.ids = self.found.get(resolved.request.commerce_subcategory, ())
        return await super().execute(resolved, context, **kwargs)


class FakeClosestType:
    """The closest-type judgement, forced to a chosen answer for the test."""

    def __init__(self, pick: str | None) -> None:
        self.pick = pick
        self.calls: list[dict[str, Any]] = []

    async def closest(
        self,
        *,
        asked_subcategory: str,
        commerce_category: str,
        offered: Any,
        context: Any,
    ) -> str | None:
        self.calls.append(
            {
                "asked": asked_subcategory,
                "category": commerce_category,
                "offered": tuple(offered),
            }
        )
        return self.pick


def _request(category: str, subcategory: str) -> ProductSearchRequest:
    return ProductSearchRequest(commerce_category=category, commerce_subcategory=subcategory)


async def _run(
    request: ProductSearchRequest,
    *,
    pick: str | None,
    found: dict[str, tuple[int, ...]],
    message: str = "show me something",
) -> tuple[Any, ByType, FakeClosestType]:
    pipeline = ByType(found)
    picker = FakeClosestType(pick)
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(request),
        pipeline=pipeline,
        capabilities=Catalog(),
        closest_type=picker,
    )
    result = await coordinator.run(_turn(_state(request=None), message))
    return result, pipeline, picker


async def test_a_candle_is_recovered_with_the_closest_stocked_type() -> None:
    result, pipeline, picker = await _run(
        _request("decor", "candle"), pick="candlestick", found={"candlestick": (10, 11)}
    )

    searched = [call.request.commerce_subcategory for call in pipeline.calls]
    assert searched == ["candle", "candlestick"]  # exact request first, then the closest
    assert result.unstocked_type == "candle"
    assert result.state.active_search.request.commerce_subcategory == "candlestick"
    assert result.state.product_interaction.presented_product_ids == (10, 11)
    # The picker was offered the real stocked decor siblings, candle excluded.
    assert set(picker.calls[0]["offered"]) == {"candlestick", "vase", "mirror"}

    view = route_response(result).primary
    assert view.unstocked_type == "candle"  # type: ignore[union-attr]
    assert view.offered_instead_of is None  # type: ignore[union-attr]


async def test_it_is_generic_across_categories() -> None:
    """The same recovery serves seating: a recliner becomes a lounge chair."""
    result, _, picker = await _run(
        _request("seating", "recliner"), pick="lounge-chair", found={"lounge-chair": (20,)}
    )

    assert result.unstocked_type == "recliner"
    assert result.state.active_search.request.commerce_subcategory == "lounge-chair"
    assert set(picker.calls[0]["offered"]) == {"chair", "lounge-chair", "sofa"}


async def test_a_stocked_type_is_never_substituted() -> None:
    """A vase IS stocked; an empty vase search is tight filters, not absence, so
    the type is never switched - that stays the relaxation layer's job."""
    result, pipeline, picker = await _run(
        _request("decor", "vase"), pick="candlestick", found={}
    )

    assert result.unstocked_type is None
    assert picker.calls == []  # never consulted for a stocked type
    assert [call.request.commerce_subcategory for call in pipeline.calls] == ["vase"]


async def test_a_family_stocked_with_nothing_stays_honest() -> None:
    """A treadmill at a furniture store: no fitness shelves at all, so there is
    no honest sibling to offer and the picker is never even reached."""
    result, _, picker = await _run(
        _request("fitness", "treadmill"), pick="anything", found={}
    )

    assert result.unstocked_type is None
    assert picker.calls == []


async def test_a_declined_pick_keeps_the_honest_zero_result() -> None:
    result, pipeline, _ = await _run(
        _request("decor", "candle"), pick=None, found={"candlestick": (10,)}
    )

    assert result.unstocked_type is None
    # Only the exact request ran; nothing was substituted.
    assert [call.request.commerce_subcategory for call in pipeline.calls] == ["candle"]


async def test_when_the_substitute_finds_nothing_the_empty_search_stands() -> None:
    result, _, _ = await _run(
        _request("decor", "candle"), pick="candlestick", found={}
    )

    assert result.unstocked_type is None
    assert result.state.active_search.request.commerce_subcategory == "candle"
