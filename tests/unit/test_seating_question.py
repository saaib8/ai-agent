"""Asking first when no single piece seats enough, and what follows.

A seat count no single piece meets gets one question before anything is shown -
which shape, from real prices, and the colour if none is known - asked once.
The answer picks the shape; a combination on screen can then be chosen.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.agent_state import (
    AgentStateV1,
    OfferedCombination,
    OfferedCombinationLine,
    SeatingOfferState,
)
from app.schemas.agent_updates import AgentStateUpdate, CustomerPreferenceUpdate
from app.schemas.discovery import ProductSearchRequest
from app.schemas.response import SeatingSolutionGroundingView
from app.schemas.seating_solution import (
    SeatingAnswer,
    SeatingBundle,
    SeatingBundleLine,
    SeatingShape,
    SeatingShapeOption,
    SeatingSolution,
    SeatingSolutionOutcome,
)
from app.schemas.session import SessionEnvelope, new_session
from app.services.agent_state import apply_update, commit_search_results
from app.services.numeric_guard import (
    build_allowance,
    check_numeric_policy,
    seating_counts,
    seating_figures,
)
from app.services.seating_presentation import seating_choices
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.seating import load_seating_semantics

from tests.unit.test_turn_coordinator import (
    SEATS_EIGHT,
    FakePipeline,
    FakeSeatingPlanner,
    _coordinator,
    _resolved,
    _state,
    _turn,
)

SEPARATE, EXTRA = SeatingShape.SEPARATE_SOFAS, SeatingShape.SOFA_WITH_EXTRA_SEATS


def _line(product_id: int, seats: int, quantity: int = 1) -> SeatingBundleLine:
    return SeatingBundleLine(
        product_id=product_id,
        name=f"piece {product_id}",
        commerce_subcategory="sofa" if seats > 1 else "chair",
        unit_price=Decimal("1000"),
        quantity=quantity,
        seats_each=seats,
        seats_are_confirmed=seats > 1,
        image_url=f"http://example/{product_id}.jpg",
        product_url=f"http://example/{product_id}",
    )


def _bundle(shape: SeatingShape, *lines: SeatingBundleLine) -> SeatingBundle:
    return SeatingBundle(
        shape=shape,
        lines=lines,
        total_seats=sum(line.line_seats for line in lines),
        total_price=sum((line.line_total for line in lines), Decimal(0)),
        currency="SAR",
    )


def _solution(*shapes: SeatingShape, target: int = 8) -> SeatingSolution:
    """Bundles and options for the given shapes, cheapest first."""
    bundles = {
        SEPARATE: _bundle(SEPARATE, _line(1, 6), _line(2, 2)),
        EXTRA: _bundle(EXTRA, _line(3, 6), _line(4, 1, quantity=2)),
    }
    return SeatingSolution(
        target_seats=target,
        budget_amount=Decimal("5000"),
        currency="SAR",
        outcome=SeatingSolutionOutcome.BUNDLES,
        bundles=tuple(bundles[s] for s in shapes),
        options=tuple(
            SeatingShapeOption(shape=s, from_price=bundles[s].total_price, combination_count=1)
            for s in shapes
        ),
    )


SEARCH = CustomerAgentDecision(action=AgentAction.SEARCH)


async def _run(
    state: AgentStateV1,
    decision: CustomerAgentDecision = SEARCH,
    solution: SeatingSolution | None = None,
    request: ProductSearchRequest = SEATS_EIGHT,
) -> tuple[Any, FakeSeatingPlanner]:
    planner = FakeSeatingPlanner(solution or _solution(SEPARATE, EXTRA))
    coordinator, _ = _coordinator(
        decision,
        interpretation=_resolved(request),
        pipeline=FakePipeline(ids=()),
        seating_planner=planner,
    )
    return await coordinator.run(_turn(state, "a sofa for 8 people")), planner


# ── ask first, once ─────────────────────────────────────────────────────────


async def test_a_real_choice_of_shape_is_asked_before_anything_is_shown() -> None:
    result, _ = await _run(_state(request=None))

    solution = result.seating_solution
    assert solution is not None
    assert solution.outcome is SeatingSolutionOutcome.CHOOSE_SHAPE
    assert solution.bundles == ()
    assert [o.shape for o in solution.options] == [SEPARATE, EXTRA]
    offer = result.state.seating_offer
    assert offer is not None and offer.shape_asked and offer.shown == ()
    assert offer.offered_shapes == (SEPARATE, EXTRA)


async def test_one_shape_and_a_known_colour_is_shown_without_asking() -> None:
    beige = SEATS_EIGHT.model_copy(update={"colors_any_of": ("Beige",)})

    result, _ = await _run(_state(request=None), solution=_solution(EXTRA), request=beige)

    assert result.seating_solution.outcome is SeatingSolutionOutcome.BUNDLES
    assert len(result.state.seating_offer.shown) == 1


async def test_one_shape_with_no_colour_still_asks_the_colour() -> None:
    result, _ = await _run(_state(request=None), solution=_solution(EXTRA))

    solution = result.seating_solution
    assert solution.outcome is SeatingSolutionOutcome.CHOOSE_SHAPE
    assert solution.ask_colour is True


async def test_the_question_is_never_asked_twice_for_the_same_seat_count() -> None:
    asked = _state(request=None).model_copy(
        update={
            "seating_offer": SeatingOfferState(
                target_seats=8, offered_shapes=(SEPARATE, EXTRA), shape_asked=True
            )
        }
    )

    result, _ = await _run(asked)

    assert result.seating_solution.outcome is SeatingSolutionOutcome.BUNDLES


async def test_a_new_seat_count_is_a_new_question() -> None:
    asked_for_nine = _state(request=None).model_copy(
        update={
            "seating_offer": SeatingOfferState(
                target_seats=9, offered_shapes=(SEPARATE,), shape_asked=True
            )
        }
    )

    result, _ = await _run(asked_for_nine)

    assert result.seating_solution.outcome is SeatingSolutionOutcome.CHOOSE_SHAPE
    assert result.state.seating_offer.target_seats == 8


# ── their answer ────────────────────────────────────────────────────────────


def _asked() -> AgentStateV1:
    from app.schemas.agent_state import ActiveSearchState

    return _state(request=None).model_copy(
        update={
            "active_search": ActiveSearchState(request=SEATS_EIGHT, revision=0),
            "seating_offer": SeatingOfferState(
                target_seats=8, offered_shapes=(SEPARATE, EXTRA), shape_asked=True
            ),
        }
    )


def _answer(answer: SeatingAnswer) -> CustomerAgentDecision:
    return CustomerAgentDecision(action=AgentAction.REFINE_SEARCH, seating_answer=answer)


async def test_the_chosen_shape_is_what_the_planner_builds() -> None:
    result, planner = await _run(_asked(), _answer(SeatingAnswer.SEPARATE_SOFAS))

    assert planner.calls[-1]["shape"] is SEPARATE
    assert result.state.seating_offer.chosen_shape is SEPARATE
    assert result.seating_solution.outcome is SeatingSolutionOutcome.BUNDLES


async def test_a_shape_that_was_never_offered_is_not_taken() -> None:
    only_separate = _asked().model_copy(
        update={
            "seating_offer": SeatingOfferState(
                target_seats=8, offered_shapes=(SEPARATE,), shape_asked=True
            )
        }
    )

    _, planner = await _run(only_separate, _answer(SeatingAnswer.SOFA_WITH_EXTRA_SEATS))

    assert planner.calls[-1]["shape"] is None


async def test_either_shows_the_best_of_every_shape() -> None:
    result, planner = await _run(_asked(), _answer(SeatingAnswer.ANY))

    assert planner.calls[-1]["shape"] is None
    assert result.seating_solution.outcome is SeatingSolutionOutcome.BUNDLES
    assert len(result.state.seating_offer.shown) == 2


# ── choosing a combination on screen ────────────────────────────────────────


def _shown() -> AgentStateV1:
    return _state(request=None).model_copy(
        update={
            "seating_offer": SeatingOfferState(
                target_seats=8,
                offered_shapes=(SEPARATE, EXTRA),
                shape_asked=True,
                shown=(
                    OfferedCombination(
                        shape=SEPARATE,
                        lines=(
                            OfferedCombinationLine(product_id=1, quantity=1),
                            OfferedCombinationLine(product_id=2, quantity=1),
                        ),
                    ),
                    OfferedCombination(
                        shape=EXTRA,
                        lines=(
                            OfferedCombinationLine(product_id=3, quantity=1),
                            OfferedCombinationLine(product_id=4, quantity=2),
                        ),
                    ),
                ),
            )
        }
    )


def _choose(position: int) -> CustomerAgentDecision:
    return CustomerAgentDecision(action=AgentAction.SHOW_SELECTION, combination_choice=position)


async def test_the_second_option_is_the_second_combination_shown() -> None:
    before = _shown()
    coordinator, _ = _coordinator(_choose(2))

    result = await coordinator.run(_turn(before, "I'll take the second option"))

    picks = result.state.product_interaction.selected_product_ids
    assert set(picks) >= {3, 4}
    assert 1 not in picks
    offer = result.state.seating_offer
    assert offer is not None
    chosen = offer.chosen
    assert chosen is not None
    assert [(line.product_id, line.quantity) for line in chosen.lines] == [(3, 1), (4, 2)]


async def test_the_first_option_is_the_first_combination_shown() -> None:
    coordinator, _ = _coordinator(_choose(1))

    result = await coordinator.run(_turn(_shown(), "the first one please"))

    offer = result.state.seating_offer
    assert offer is not None and offer.chosen is not None
    assert [line.product_id for line in offer.chosen.lines] == [1, 2]


async def test_a_choice_past_the_end_changes_nothing() -> None:
    before = _shown()
    coordinator, _ = _coordinator(_choose(3))

    result = await coordinator.run(_turn(before, "the third one"))

    assert result.state.product_interaction.selected_product_ids == (
        before.product_interaction.selected_product_ids
    )
    offer = result.state.seating_offer
    assert offer is not None and offer.chosen is None


def test_an_answer_alone_is_a_refinement() -> None:
    decision = CustomerAgentDecision.model_validate(
        {"action": "refine_search", "seating_answer": "separate_sofas"}
    )

    assert decision.seating_answer is SeatingAnswer.SEPARATE_SOFAS


def test_a_combination_is_chosen_only_by_showing_what_was_chosen() -> None:
    with pytest.raises(ValueError, match="shows what they have chosen"):
        CustomerAgentDecision(action=AgentAction.SEARCH, combination_choice=1)


# ── the offer survives the turn and the session ─────────────────────────────


def test_the_offer_survives_every_state_rebuild() -> None:
    state = _shown()

    updated = apply_update(state, AgentStateUpdate(customer_preferences=CustomerPreferenceUpdate()))

    assert updated.seating_offer == state.seating_offer
    from app.schemas.agent_state import ActiveSearchState

    searching = state.model_copy(
        update={"active_search": ActiveSearchState(request=SEATS_EIGHT, revision=1)}
    )
    assert commit_search_results(searching, (7,)).seating_offer == state.seating_offer


def test_a_session_saved_before_the_offer_existed_still_loads() -> None:
    session = new_session()
    document = json.loads(session.model_dump_json())
    document["state"].pop("seating_offer", None)

    loaded = SessionEnvelope.model_validate_json(json.dumps(document))

    assert loaded.state.seating_offer is None


# ── what the reply is given ─────────────────────────────────────────────────


def test_a_question_carries_its_real_options() -> None:
    options = _solution(SEPARATE, EXTRA).options

    view = SeatingSolutionGroundingView(
        outcome=SeatingSolutionOutcome.CHOOSE_SHAPE,
        target_seats=8,
        shape_options=options,
        currency="SAR",
        ask_colour=True,
    )

    assert seating_figures(view) == tuple(o.from_price for o in options)
    assert 2 in seating_counts(view)
    with pytest.raises(ValueError, match="carries its options"):
        SeatingSolutionGroundingView(outcome=SeatingSolutionOutcome.CHOOSE_SHAPE, target_seats=8)


def test_the_question_may_quote_its_from_prices_and_nothing_else() -> None:
    view = SeatingSolutionGroundingView(
        outcome=SeatingSolutionOutcome.CHOOSE_SHAPE,
        target_seats=8,
        shape_options=_solution(SEPARATE, EXTRA).options,
    )
    allowance = build_allowance(
        "a sofa for 8 people", counts=seating_counts(view), figures=seating_figures(view)
    )

    said = "No single sofa seats 8, but there are 2 ways: sofas from 2,000 or a set from 3,000."
    assert check_numeric_policy(message=said, follow_up_question=None, allowance=allowance) is None
    invented = "Or a set with chairs from 4,200."
    assert check_numeric_policy(message=invented, follow_up_question=None, allowance=allowance)


def test_a_solution_with_nothing_to_report_adds_no_zero() -> None:
    view = SeatingSolutionGroundingView(
        outcome=SeatingSolutionOutcome.BUNDLES, target_seats=8, bundle_count=2
    )

    assert seating_counts(view) == (8, 2)


def test_the_chips_offer_only_real_shapes_with_their_prices() -> None:
    question = _solution(SEPARATE, EXTRA).model_copy(
        update={"outcome": SeatingSolutionOutcome.CHOOSE_SHAPE, "bundles": ()}
    )

    labels = [choice.label for choice in seating_choices(question)]

    assert labels[0].startswith("Separate sofas") and "2,000" in labels[0]
    assert labels[1].startswith("Sofa + armchairs")
    assert labels[-1].startswith("Either")
    assert seating_choices(_solution(SEPARATE, EXTRA)) == ()


def test_a_lifted_colour_reaches_the_question() -> None:
    solution = _solution(SEPARATE).model_copy(update={"lifted": (AttributeFamily.COLOR,)})
    assert solution.lifted == (AttributeFamily.COLOR,)


# ── the reviewed list of extra seats ────────────────────────────────────────


def test_only_one_seat_types_may_be_extra_seats(tmp_path: Path) -> None:
    seating = load_seating_semantics(taxonomy=load_taxonomy())
    assert seating.is_combination_extra("chair")
    assert not seating.is_combination_extra("office-chair")

    path = tmp_path / "seating.yaml"
    path.write_text("version: v1\nimplied_capacity: {chair: 1}\ncombination_extras: [sofa]\n")
    with pytest.raises(TaxonomyConfigurationError) as raised:
        load_seating_semantics(path, taxonomy=load_taxonomy())
    assert "must each seat one" in str(raised.value.context)


# ── more, and not this one ──────────────────────────────────────────────────


def _on_screen() -> AgentStateV1:
    from app.schemas.agent_state import ActiveSearchState

    return _shown().model_copy(
        update={"active_search": ActiveSearchState(request=SEATS_EIGHT, revision=0)}
    )


def _excluded_ids(planner: FakeSeatingPlanner) -> set[tuple[tuple[int, int], ...]]:
    return set(planner.calls[-1]["exclude"])


async def test_show_more_leaves_out_every_combination_on_screen() -> None:
    more = CustomerAgentDecision(action=AgentAction.SEARCH, show_more=True)

    result, planner = await _run(_on_screen(), more)

    assert _excluded_ids(planner) == {((1, 1), (2, 1)), ((3, 1), (4, 2))}
    offer = result.state.seating_offer
    assert offer is not None and len(offer.excluded) == 2


async def test_not_the_second_one_leaves_out_only_that_one() -> None:
    dismiss = CustomerAgentDecision(action=AgentAction.SEARCH, combination_dismiss=2)

    _, planner = await _run(_on_screen(), dismiss)

    assert _excluded_ids(planner) == {((3, 1), (4, 2))}


async def test_show_more_with_product_cards_still_pages_the_products() -> None:
    """No combinations on screen: the ordinary product paging runs."""
    more = CustomerAgentDecision(action=AgentAction.SEARCH, show_more=True)
    coordinator, parts = _coordinator(more)

    await coordinator.run(_turn(_state(), "show more options"))

    assert parts["pipeline"].calls[-1].request.exclude_product_ids


async def test_the_oldest_remembered_combinations_are_forgotten_first() -> None:
    from app.services.turn_coordinator import MAX_EXCLUDED_COMBINATIONS

    old = tuple(
        OfferedCombination(
            shape=SEPARATE, lines=(OfferedCombinationLine(product_id=100 + n, quantity=1),)
        )
        for n in range(MAX_EXCLUDED_COMBINATIONS)
    )
    full = _on_screen().model_copy(
        update={
            "seating_offer": _on_screen().seating_offer.model_copy(update={"excluded": old})  # type: ignore[union-attr]
        }
    )
    more = CustomerAgentDecision(action=AgentAction.SEARCH, show_more=True)

    _, planner = await _run(full, more)

    excluded = _excluded_ids(planner)
    assert len(excluded) == MAX_EXCLUDED_COMBINATIONS
    assert ((1, 1), (2, 1)) in excluded
    assert ((100, 1),) not in excluded


async def test_no_more_keeps_what_they_last_saw() -> None:
    no_more = SeatingSolution(
        target_seats=8,
        currency="SAR",
        outcome=SeatingSolutionOutcome.NO_MORE,
        requested_shape=SEPARATE,
        options=_solution(EXTRA).options,
    )
    more = CustomerAgentDecision(action=AgentAction.SEARCH, show_more=True)

    result, _ = await _run(_on_screen(), more, solution=no_more)

    assert result.seating_solution.outcome is SeatingSolutionOutcome.NO_MORE
    offer = result.state.seating_offer
    assert offer is not None and len(offer.shown) == 2


def test_turning_a_combination_down_is_a_search() -> None:
    with pytest.raises(ValueError, match="searches for another"):
        CustomerAgentDecision(action=AgentAction.SHOW_SELECTION, combination_dismiss=1)


def test_no_more_offers_only_the_shapes_left() -> None:
    no_more = SeatingSolution(
        target_seats=8,
        currency="SAR",
        outcome=SeatingSolutionOutcome.NO_MORE,
        requested_shape=SEPARATE,
        options=_solution(EXTRA).options,
    )

    labels = [c.label for c in seating_choices(no_more)]
    view = SeatingSolutionGroundingView(
        outcome=SeatingSolutionOutcome.NO_MORE,
        target_seats=8,
        shape_options=no_more.options,
        exhausted_shape=SEPARATE,
    )

    assert len(labels) == 1 and labels[0].startswith("Sofa + armchairs")
    assert view.exhausted_shape is SEPARATE
    with pytest.raises(ValueError, match="offers shapes"):
        SeatingSolutionGroundingView(
            outcome=SeatingSolutionOutcome.BUNDLES,
            target_seats=8,
            bundle_count=1,
            shape_options=no_more.options,
        )
