"""A seat count the asked type never reaches, but another type does in one
piece: "a sofa for six" is shown sofa sets, as the best fit (CLAUDE.md 27.1).

Only when the asked type itself cannot seat them. Everything else they asked
is kept, and the reply is told which type they asked for so it can frame the
cards as good news rather than a refusal.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.catalog_overview import CatalogOverview, SeatingSpread, SubcategoryShelf
from app.schemas.discovery import PriceConstraint, ProductSearchRequest, SeatingCapacityConstraint
from app.services.response_view import route_response
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.seating import load_seating_semantics

from tests.unit.test_turn_coordinator import (
    FakeCapabilities,
    FakePipeline,
    _coordinator,
    _resolved,
    _state,
    _turn,
)

SEATING = load_seating_semantics(taxonomy=load_taxonomy())


def _shelf(subcategory: str, low: int, high: int, price: str) -> SubcategoryShelf:
    return SubcategoryShelf(
        commerce_category="seating",
        commerce_subcategory=subcategory,
        active_count=5,
        price_minimum=Decimal(price),
        price_maximum=Decimal("9000"),
        seating=SeatingSpread(known_count=5, minimum=low, maximum=high),
        colours=(),
    )


OVERVIEW = CatalogOverview(
    store_id=50,
    currency="SAR",
    shelves=(
        _shelf("sofa", 2, 4, "990"),
        _shelf("sofa-set", 6, 7, "2450"),
        # Seats six too, but only when they ask for sofa beds.
        _shelf("sofa-bed", 3, 6, "500"),
    ),
)


class Catalog(FakeCapabilities):
    async def overview(self, context: Any) -> CatalogOverview:
        return OVERVIEW


class ByType(FakePipeline):
    """Finds products only for the types given, recording every search."""

    def __init__(self, found: dict[str, tuple[int, ...]]) -> None:
        super().__init__()
        self.found = found

    async def execute(self, resolved: Any, context: Any, **kwargs: Any) -> Any:
        self.ids = self.found.get(resolved.request.commerce_subcategory, ())
        return await super().execute(resolved, context, **kwargs)


def _seats(subcategory: str, seats: int, **fields: Any) -> ProductSearchRequest:
    return ProductSearchRequest(
        commerce_category="seating",
        commerce_subcategory=subcategory,
        seating_capacity=SeatingCapacityConstraint(min_capacity=seats),
        **fields,
    )


async def _run(request: ProductSearchRequest, found: dict[str, tuple[int, ...]]) -> Any:
    pipeline = ByType(found)
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(request),
        pipeline=pipeline,
        capabilities=Catalog(),
        seating=SEATING,
    )
    result = await coordinator.run(_turn(_state(request=None), "a sofa for 6"))
    return result, pipeline


async def test_a_sofa_for_six_is_shown_the_sofa_set_that_seats_them() -> None:
    result, pipeline = await _run(_seats("sofa", 6), {"sofa-set": (31, 32)})

    searched = [call.request.commerce_subcategory for call in pipeline.calls]
    assert searched == ["sofa", "sofa-set"]
    assert result.offered_instead_of == "sofa"
    assert result.state.active_search.request.commerce_subcategory == "sofa-set"
    assert result.state.product_interaction.presented_product_ids == (31, 32)
    # The seat count is kept: a set that seats six, not any set.
    assert pipeline.calls[1].request.seating_capacity.min_capacity == 6

    view = route_response(result).primary
    assert view.offered_instead_of == "sofa"  # type: ignore[union-attr]


async def test_a_sofa_bed_is_never_the_type_offered_instead() -> None:
    _, pipeline = await _run(_seats("sofa", 6), {"sofa-bed": (40,)})

    assert "sofa-bed" not in [call.request.commerce_subcategory for call in pipeline.calls]


async def test_a_count_the_asked_type_can_seat_is_never_switched() -> None:
    """A purple sofa for three found nothing because of the colour - sofas do
    seat three - so no other type is searched."""
    result, pipeline = await _run(_seats("sofa", 3, colors_any_of=("Purple",)), {"sofa-set": (31,)})

    assert [call.request.commerce_subcategory for call in pipeline.calls] == ["sofa"]
    assert result.offered_instead_of is None


async def test_when_the_other_type_finds_nothing_the_empty_search_stands() -> None:
    result, _ = await _run(_seats("sofa", 6), {})

    assert result.offered_instead_of is None
    assert result.state.active_search.request.commerce_subcategory == "sofa"


async def test_offering_the_closest_total_is_the_seat_counts_one_question() -> None:
    """ "The closest is about 3,700 - shall I show it?" A yes must show the
    combinations, not ask which shape first."""
    from app.schemas.seating_solution import SeatingSolution, SeatingSolutionOutcome

    from tests.unit.test_turn_coordinator import FakeSeatingPlanner

    planner = FakeSeatingPlanner(
        SeatingSolution(
            target_seats=9,
            budget_amount=Decimal("1500"),
            currency="SAR",
            outcome=SeatingSolutionOutcome.NONE_WITHIN_BUDGET,
            closest_total=Decimal("3700"),
        )
    )
    request = _seats("sofa", 9).model_copy(
        update={"price": PriceConstraint.at_most(Decimal("1500"), "SAR")}
    )
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(request),
        pipeline=FakePipeline(ids=()),
        seating_planner=planner,
    )

    result = await coordinator.run(_turn(_state(request=None), "sofas for 9 under 1500"))

    assert result.seating_solution is not None
    assert result.seating_solution.closest_total == Decimal("3700")
    offer = result.state.seating_offer
    assert offer is not None and offer.target_seats == 9 and offer.shape_asked
