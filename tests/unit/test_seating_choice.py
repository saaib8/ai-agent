"""Choosing a seating combination, typed or tapped, and what the reply knows.

A combination is chosen with its quantities - two of one 4-seater seat eight -
and a tap on the screen takes exactly the path the typed words take
(CLAUDE.md 17.1, 27.1).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    OfferedCombination,
    OfferedCombinationLine,
    SeatingOfferState,
)
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.search_action import CombinationAction
from app.schemas.seating_solution import SeatingShape
from app.services.numeric_guard import (
    build_allowance,
    check_numeric_policy,
    chosen_seating_counts,
)
from app.services.response_view import route_response
from app.services.seating_presentation import present_chosen_seating
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.seating import load_seating_semantics
from pydantic import ValidationError

from tests.unit.test_seating_question import SEPARATE, _bundle, _line, _solution
from tests.unit.test_turn_coordinator import (
    CONTEXT,
    SEATS_EIGHT,
    FakePipeline,
    FakeSeatingPlanner,
    _coordinator,
    _product,
    _state,
)

FOUR_SEATER, SIX_SEATER, SECOND_SIX, ARMCHAIR = 41, 61, 62, 71


class SeatedHydration:
    """The catalog as it stands: sofas with their recorded seats, one armchair."""

    def __init__(self, gone: tuple[int, ...] = ()) -> None:
        self.gone = gone

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[ProductCandidate, ...]:
        return tuple(self._row(p) for p in product_ids if p not in self.gone)

    @staticmethod
    def _row(product_id: int) -> ProductCandidate:
        if product_id == ARMCHAIR:
            return _product(product_id, subcategory="chair")
        seats = {FOUR_SEATER: 4, SIX_SEATER: 6, SECOND_SIX: 6}.get(product_id)
        row = _product(product_id)
        return row.model_copy(
            update={
                "commerce": CommerceClassification(
                    category="seating", subcategory="sofa", seating_capacity=seats
                )
            }
        )


def _combination(shape: SeatingShape, *lines: tuple[int, int]) -> OfferedCombination:
    return OfferedCombination(
        shape=shape,
        lines=tuple(OfferedCombinationLine(product_id=p, quantity=q) for p, q in lines),
    )


def _on_screen() -> AgentStateV1:
    return _state(request=None).model_copy(
        update={
            "active_search": ActiveSearchState(request=SEATS_EIGHT, revision=0),
            "seating_offer": SeatingOfferState(
                target_seats=8,
                offered_shapes=(SEPARATE, SeatingShape.SOFA_WITH_EXTRA_SEATS),
                shape_asked=True,
                shown=(
                    _combination(SEPARATE, (SIX_SEATER, 1), (SECOND_SIX, 1)),
                    _combination(SEPARATE, (FOUR_SEATER, 2)),
                    _combination(
                        SeatingShape.SOFA_WITH_EXTRA_SEATS, (SIX_SEATER, 1), (ARMCHAIR, 2)
                    ),
                ),
            ),
        }
    )


def _seating_coordinator(
    decision: CustomerAgentDecision | None = None,
    *,
    hydration: SeatedHydration | None = None,
    planner: FakeSeatingPlanner | None = None,
    reviewed_seats: bool = True,
) -> Any:
    coordinator, _ = _coordinator(
        decision or CustomerAgentDecision(action=AgentAction.ANSWER),
        hydration=hydration or SeatedHydration(),  # type: ignore[arg-type]
        seating=load_seating_semantics(taxonomy=load_taxonomy()) if reviewed_seats else None,
        pipeline=FakePipeline(ids=()),
        seating_planner=planner or FakeSeatingPlanner(_solution(SEPARATE)),
    )
    return coordinator


def _typed(position: int) -> CustomerAgentDecision:
    return CustomerAgentDecision(action=AgentAction.SHOW_SELECTION, combination_choice=position)


def _tap(op: str, position: int | None = None) -> CustomerTurnInput:
    return CustomerTurnInput(
        message="I'll take option 2",
        state=_on_screen(),
        context=CONTEXT,
        search_action=CombinationAction(op=op, position=position),  # type: ignore[arg-type]
    )


# ── what was chosen ─────────────────────────────────────────────────────────


async def test_two_of_one_sofa_are_chosen_as_two() -> None:
    coordinator = _seating_coordinator(_typed(2))

    result = await coordinator.run(
        CustomerTurnInput(message="i like option 2", state=_on_screen(), context=CONTEXT)
    )

    chosen = result.chosen_seating
    assert chosen is not None
    assert [(line.product_id, line.quantity) for line in chosen.lines] == [(FOUR_SEATER, 2)]
    assert chosen.total_seats == 8
    assert result.state.product_interaction.selected_product_ids[-1] == FOUR_SEATER


async def test_a_tap_chooses_exactly_what_the_typed_words_choose() -> None:
    typed = await _seating_coordinator(_typed(2)).run(
        CustomerTurnInput(message="i like option 2", state=_on_screen(), context=CONTEXT)
    )
    tapped = await _seating_coordinator().run(_tap("choose", 2))

    assert tapped.chosen_seating == typed.chosen_seating
    assert tapped.state.seating_offer == typed.state.seating_offer
    assert tapped.selection_added


async def test_an_armchair_seats_one_by_reviewed_data_not_the_catalog() -> None:
    result = await _seating_coordinator().run(_tap("choose", 3))

    chosen = result.chosen_seating
    assert chosen is not None
    chair = next(line for line in chosen.lines if line.product_id == ARMCHAIR)
    assert (chair.seats_each, chair.seats_are_confirmed, chair.quantity) == (1, False, 2)
    assert chosen.total_seats == 8


async def test_the_main_piece_leads_what_goes_with_it() -> None:
    result = await _seating_coordinator().run(_tap("choose", 3))

    assert result.focus is not None
    assert result.focus.commerce.seating_capacity == 6


async def test_a_piece_gone_from_the_catalog_claims_no_combination() -> None:
    hydration = SeatedHydration(gone=(SECOND_SIX,))

    result = await _seating_coordinator(hydration=hydration).run(_tap("choose", 1))

    assert result.chosen_seating is None
    offer = result.state.seating_offer
    assert offer is not None and offer.chosen is not None


async def test_a_tap_past_the_end_chooses_nothing() -> None:
    result = await _seating_coordinator().run(_tap("choose", 4))

    assert result.chosen_seating is None
    offer = result.state.seating_offer
    assert offer is not None and offer.chosen is None


# ── turning down and seeing more, tapped ────────────────────────────────────


async def test_a_tapped_turn_down_leaves_out_only_that_one() -> None:
    planner = FakeSeatingPlanner(_solution(SEPARATE))

    await _seating_coordinator(planner=planner, reviewed_seats=False).run(_tap("dismiss", 2))

    assert set(planner.calls[-1]["exclude"]) == {((FOUR_SEATER, 2),)}


async def test_tapped_more_leaves_out_everything_on_screen() -> None:
    planner = FakeSeatingPlanner(_solution(SEPARATE))

    result = await _seating_coordinator(planner=planner, reviewed_seats=False).run(_tap("more"))

    assert len(planner.calls[-1]["exclude"]) == 3
    offer = result.state.seating_offer
    assert offer is not None and len(offer.excluded) == 3


async def test_a_tap_with_no_combinations_on_screen_searches_nothing() -> None:
    planner = FakeSeatingPlanner(_solution(SEPARATE))
    turn = _tap("more").model_copy(update={"state": _state(request=None)})

    result = await _seating_coordinator(planner=planner).run(turn)

    assert not planner.calls
    assert result.grounding.deterministic_clarification is not None


@pytest.mark.parametrize(("op", "position"), [("choose", None), ("dismiss", None), ("more", 1)])
def test_a_tap_names_a_position_exactly_when_it_must(op: str, position: int | None) -> None:
    with pytest.raises(ValidationError):
        CombinationAction(op=op, position=position)  # type: ignore[arg-type]


# ── what the reply and the screen are told ──────────────────────────────────


async def test_the_reply_is_told_the_combination_seats_everyone() -> None:
    result = await _seating_coordinator().run(_tap("choose", 2))

    view = route_response(result).primary
    chosen = getattr(view, "chosen_seating", None)
    assert chosen is not None
    assert chosen.total_seats == 8 and chosen.target_seats == 8
    assert [(p.kind, p.quantity, p.seats_each) for p in chosen.pieces] == [("sofa", 2, 4)]


async def test_the_reply_may_say_two_four_seaters_seat_eight() -> None:
    result = await _seating_coordinator().run(_tap("choose", 2))
    view = route_response(result).primary
    chosen = getattr(view, "chosen_seating", None)
    assert chosen is not None

    allowance = build_allowance("i like option 2", counts=chosen_seating_counts(chosen))

    assert not check_numeric_policy(
        message="Two of these 4-seaters seat all 8 of you.",
        follow_up_question=None,
        allowance=allowance,
    )


def test_the_chosen_combination_is_drawn_with_its_quantities_and_no_budget() -> None:
    bundle = _bundle(SEPARATE, _line(FOUR_SEATER, 4, quantity=2))

    drawn = present_chosen_seating(bundle)

    assert [item.quantity for item in drawn.items] == [2]
    assert drawn.totals.new_spend_total == Decimal("2000")
    assert drawn.totals.budget_max_amount is None
