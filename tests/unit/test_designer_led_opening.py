"""The opening: two writer-chosen questions before a new search's products.

docs/designer-led-shopping-plan.md, phase 1. With `designer_led_opening` on, a
new search for a kind of product is answered with the questions of its family
that are still open - not known, never asked before, never the budget - and
the reply writer chooses the two to ask. The chips of those two are drawn; the
rest wait for another turn.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.repositories.products import BriefFacts
from app.schemas.agent_decision import CustomerStateProposal
from app.schemas.agent_state import AgentStateV1, CustomerPreferenceState
from app.schemas.agent_turn import CustomerResponse
from app.schemas.discovery import SeatingCapacityConstraint
from app.schemas.product import EligibleProduct
from app.schemas.product_brief import BriefMode
from app.schemas.query import ConstraintStrength
from app.schemas.relaxation import RelaxedCandidate
from app.schemas.response import ProductBriefGroundingView, ResponseViolationKind
from app.schemas.search_action import BriefAnswerAction
from app.services.agent_state import apply_update
from app.services.numeric_guard import build_allowance
from app.services.product_brief import opening_asked, record_asked
from app.services.proposal_mapping import map_proposals
from app.services.response_validation import validate_response
from app.services.semantic_ranking import SemanticRankingService
from app.taxonomy.briefs import BriefQuestionKind, load_briefs

from tests.unit.test_product_brief import (
    SOFA_FACTS,
    TAXONOMY,
    _builder,
    _need,
)
from tests.unit.test_turn_coordinator import CONTEXT

ROOM, PEOPLE, SPACE, TYPE = (
    BriefQuestionKind.ROOM,
    BriefQuestionKind.PEOPLE,
    BriefQuestionKind.SPACE,
    BriefQuestionKind.TYPE,
)


async def _opening(state: AgentStateV1 | None = None, **need: Any) -> Any:
    builder, _ = _builder()
    built = await builder.build(
        _need(**need), state or AgentStateV1(), CONTEXT, mode=BriefMode.ASK, opening=True
    )
    return builder, built


def _kinds(built: Any) -> list[BriefQuestionKind]:
    return [question.kind for question in built.card.questions]


# ── the registry ────────────────────────────────────────────────────────────


def test_each_family_lists_its_opening_and_a_new_type_gets_the_default() -> None:
    briefs = load_briefs(taxonomy=TAXONOMY)

    sofas = briefs.for_opening("seating", "sofa")
    # The room first; how wide the spot is waits until products are shown.
    assert sofas.opening[:3] == (ROOM, PEOPLE, BriefQuestionKind.TYPE)
    assert SPACE not in sofas.opening
    assert sofas.must_ask == (ROOM,)
    vase = briefs.for_opening("decor", "vase")
    assert vase.opening == (ROOM, BriefQuestionKind.COLOUR, BriefQuestionKind.STYLE)
    assert [room.label for room in briefs.rooms] == [
        "Living room",
        "Bedroom",
        "Office",
        "Dining room",
        "Other",
    ]


@pytest.mark.parametrize(
    ("opening", "default"),
    [
        ("[budget, colour]", "[room, colour]"),
        ("[room, colour]", "[room, feel]"),
    ],
    ids=["budget-in-an-opening", "card-only-question-by-default"],
)
def test_an_opening_never_asks_the_budget_and_the_default_asks_only_generic_questions(
    tmp_path: Path, opening: str, default: str
) -> None:
    source = tmp_path / "briefs.yaml"
    source.write_text(
        f"""
version: v1
opening_default: {default}
rooms:
  - {{key: a, label: Living room, room: living room}}
  - {{key: b, label: Other}}
briefs:
  rugs:
    for: [carpet]
    ask: [budget, colour]
    opening: {opening}
arabic: {{Living room: غرفة المعيشة, Other: غير ذلك}}
"""
    )

    with pytest.raises(TaxonomyConfigurationError):
        load_briefs(source, taxonomy=TAXONOMY)


# ── what is offered ─────────────────────────────────────────────────────────


async def test_the_opening_offers_its_open_questions_and_never_the_budget() -> None:
    _, built = await _opening()

    assert built is not None
    assert _kinds(built)[:3] == [ROOM, PEOPLE, BriefQuestionKind.TYPE]
    assert SPACE not in _kinds(built)
    assert BriefQuestionKind.BUDGET not in _kinds(built)
    assert built.pending.opening is True


async def test_what_they_said_is_not_offered_again() -> None:
    """A room on record, and a seat count in their words."""
    state = AgentStateV1(customer_preferences=CustomerPreferenceState(room="living room"))

    _, built = await _opening(state, seating_capacity=SeatingCapacityConstraint.exactly(3))

    assert ROOM not in _kinds(built) and PEOPLE not in _kinds(built)


async def test_a_question_asked_once_is_never_offered_again() -> None:
    """Per family - and the room once for the whole visit."""
    _, first = await _opening()
    asked = record_asked(
        AgentStateV1().model_copy(
            update={
                "product_brief": AgentStateV1().product_brief.model_copy(
                    update={"pending": first.pending, "cards": 1}
                )
            }
        ),
        (ROOM, PEOPLE),
    )

    _, second = await _opening(asked)

    _, rug = await _opening(asked, subcategory="carpet", category="decor")

    assert asked.product_brief.asked == ("room", "sofas:people")
    assert ROOM not in _kinds(second) and PEOPLE not in _kinds(second)
    assert ROOM not in _kinds(rug)


async def test_head_counts_order_only_up_to_the_largest_piece_then_combine() -> None:
    """The sofas seat up to three. A set seating five is another type: four
    sofa seats is a requirement, which offers the set or a combination."""
    facts = BriefFacts(
        kinds=(("sofa", 3, 10), ("sofa-set", 5, 4)),
        currency="SAR",
        price_quartiles=SOFA_FACTS.price_quartiles,
        colors=SOFA_FACTS.colors,
        styles=SOFA_FACTS.styles,
    )
    builder, _ = _builder(facts)
    built = await builder.build(_need(), AgentStateV1(), CONTEXT, mode=BriefMode.ASK, opening=True)

    assert built is not None
    people = {option.people: option for option in built.pending.people}
    assert [o.people for o in built.pending.people] == [2, 3, 4, 5, 6]
    assert people[3].combine is False and people[4].combine is True
    assert people[6].combine is True and people[6].or_more


# ── the answers ─────────────────────────────────────────────────────────────


async def test_a_tapped_room_is_remembered_and_filters_nothing() -> None:
    builder, built = await _opening()

    answer = builder.answer(BriefAnswerAction(card=1, room="office"), built.pending)

    assert answer is not None and answer.room == "office"
    assert answer.search.request == _need().request


async def test_other_room_remembers_nothing() -> None:
    builder, built = await _opening()

    answer = builder.answer(BriefAnswerAction(card=1, room="other"), built.pending)

    assert answer is not None and answer.room is None


async def test_a_head_count_a_sofa_seats_orders_and_one_past_it_is_required() -> None:
    builder, built = await _opening()
    pending = built.pending

    four = builder.answer(BriefAnswerAction(card=1, people="people-4"), pending)
    six = builder.answer(BriefAnswerAction(card=1, people="people-6"), pending)

    assert four is not None and four.search.seat_preference == 4
    assert four.search.request.seating_capacity is None
    # No sofa seats six, so "6+" is the seat requirement 27.1 starts from.
    assert six is not None and six.search.seat_preference is None
    assert six.search.request.seating_capacity == SeatingCapacityConstraint.at_least(6)


async def test_a_key_the_opening_never_showed_searches_nothing() -> None:
    builder, built = await _opening()

    assert builder.answer(BriefAnswerAction(card=1, room="majlis"), built.pending) is None
    assert builder.answer(BriefAnswerAction(card=1, people="people-9"), built.pending) is None


async def test_any_reply_while_the_opening_is_on_screen_answers_it() -> None:
    """ "Living room, and we're a family" changes no search - yet it answers."""
    builder, built = await _opening()
    state = AgentStateV1().model_copy(
        update={
            "product_brief": AgentStateV1().product_brief.model_copy(
                update={"pending": built.pending, "cards": 1}
            )
        }
    )

    assert builder.answers_card(_need(), state) is True
    assert builder.answers_card(_need("carpet", "decor"), state) is False


# ── ordering by head count ──────────────────────────────────────────────────


def _candidate(product_id: int, seats: int | None) -> RelaxedCandidate:
    return RelaxedCandidate(
        product=EligibleProduct(
            product_id=product_id, price_amount=Decimal("1000"), seating_capacity=seats
        ),
        relaxation_depth=0,
    )


async def test_a_head_count_puts_pieces_that_seat_them_first_and_hides_none() -> None:
    ranking = SemanticRankingService(None, None)
    search = _need().model_copy(update={"seat_preference": 4})
    candidates = [_candidate(1, 2), _candidate(2, None), _candidate(3, 4), _candidate(4, 5)]

    result = await ranking.rank(search, candidates, CONTEXT, namespace="test")

    assert [c.product_id for c in result.candidates] == [3, 4, 2, 1]


# ── the reply's choice ──────────────────────────────────────────────────────


def _validate(asked: tuple[BriefQuestionKind, ...], choose: int) -> Any:
    response = CustomerResponse(message="Tell me a little about the space?", asked=asked)
    brief = ProductBriefGroundingView(
        looking_for="sofa", asks_about=(ROOM, PEOPLE, SPACE), choose=choose
    )
    return validate_response(
        response,
        valid_grounding_refs=frozenset(),
        follow_up_allowed=False,
        allowance=build_allowance("I need a sofa"),
        brief=brief,
    )


def test_the_reply_asks_as_many_offered_questions_as_it_must_choose() -> None:
    assert _validate((ROOM, PEOPLE), choose=2) is None


@pytest.mark.parametrize(
    ("asked", "choose"),
    [((ROOM,), 2), ((ROOM, TYPE), 2)],
    ids=["too-few", "not-offered"],
)
def test_a_choice_outside_the_opening_is_refused(
    asked: tuple[BriefQuestionKind, ...], choose: int
) -> None:
    violation = _validate(asked, choose)

    assert violation is not None and violation.kind is ResponseViolationKind.OPENING_NOT_OFFERED


def test_a_choice_where_nothing_was_offered_is_ignored_not_refused() -> None:
    """With no opening on screen `asked` draws nothing, so it never costs the
    reply."""
    assert _validate((ROOM,), choose=0) is None


async def test_the_screen_asks_the_chosen_questions_or_the_room_first_on_a_fallback() -> None:
    _, built = await _opening()

    assert opening_asked(built.card, built.pending, (PEOPLE, ROOM)) == (PEOPLE, ROOM)
    assert opening_asked(built.card, built.pending, ()) == (ROOM, PEOPLE)
    # A sofa's opening always asks the room where it is offered.
    assert opening_asked(built.card, built.pending, (PEOPLE, BriefQuestionKind.TYPE)) == (
        ROOM,
        PEOPLE,
    )
    assert opening_asked(built.card, None, (ROOM,)) == ()


# ── the room they named ─────────────────────────────────────────────────────


def test_a_room_named_in_words_is_remembered_beside_their_preferences() -> None:
    mapped = map_proposals(CustomerStateProposal(shopping_room="majlis"), None)

    state = apply_update(AgentStateV1(), mapped.update)

    assert state.customer_preferences.room == "majlis"
    assert state.room_project is None


def test_a_session_that_never_named_a_room_saves_as_before() -> None:
    saved = AgentStateV1().model_dump(mode="json")

    assert "room" not in saved["customer_preferences"]
    assert "asked" not in saved["product_brief"]


def test_seat_semantics_of_a_combination_are_locked() -> None:
    """Kept beside the ordering test: a head count past the largest piece is a
    requirement, exactly as a typed one is."""
    from app.schemas.product_brief import BriefPeopleOption
    from app.services.product_brief import _with_people

    search = _with_people(
        _need(), BriefPeopleOption(key="people-7", people=7, or_more=True, combine=True)
    )

    assert search.request.seating_capacity == SeatingCapacityConstraint.at_least(7)
    assert search.semantics.seating_min is ConstraintStrength.LOCKED
    assert search.seat_preference is None


async def test_answering_a_card_keeps_what_was_asked() -> None:
    """Recording the next card, or clearing the answered one, never forgets
    which questions were asked."""
    from app.services.agent_state import record_brief

    _, built = await _opening()
    shown = record_brief(AgentStateV1(), built.pending, shown=built.pending.name)
    asked = record_asked(shown, (ROOM, PEOPLE))

    answered = record_brief(asked, None)

    assert answered.product_brief.asked == ("room", "sofas:people")


async def test_a_room_named_in_the_same_message_is_not_asked() -> None:
    """The turn's proposals land after its card is built; the card must still
    know the room this message named."""
    from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
    from app.services.turn_coordinator import _with_room_said

    decision = CustomerAgentDecision(
        action=AgentAction.SEARCH, state_proposal=CustomerStateProposal(shopping_room="bedroom")
    )

    _, built = await _opening(_with_room_said(AgentStateV1(), decision), subcategory="carpet")

    assert ROOM not in _kinds(built)


async def test_a_head_count_typed_in_reply_reads_as_the_tap() -> None:
    """ "There are four of us" finds what tapping 4 finds (CLAUDE.md 17.1)."""
    builder, built = await _opening()
    typed = _need(seating_capacity=SeatingCapacityConstraint.exactly(4)).model_copy(
        update={
            "semantics": _need().semantics.model_copy(
                update={
                    "seating_min": ConstraintStrength.LOCKED,
                    "seating_max": ConstraintStrength.LOCKED,
                }
            )
        }
    )

    read = builder.typed_head_count(typed, built.pending, 4)
    tapped = builder.answer(BriefAnswerAction(card=1, people="people-4"), built.pending)

    assert tapped is not None
    assert read.request == tapped.search.request
    assert read.seat_preference == tapped.search.seat_preference == 4
    assert (read.semantics.seating_min, read.semantics.seating_max) == (None, None)


async def test_a_typed_head_count_no_sofa_seats_stays_the_requirement() -> None:
    builder, built = await _opening()
    typed = _need(seating_capacity=SeatingCapacityConstraint.at_least(6))

    assert builder.typed_head_count(typed, built.pending, 6) == typed


async def test_a_named_seater_is_the_piece_not_a_head_count() -> None:
    """ "A grey 3 seater" names the piece: it stays a requirement."""
    builder, built = await _opening()
    typed = _need(seating_capacity=SeatingCapacityConstraint.exactly(3))

    assert builder.typed_head_count(typed, built.pending, None) == typed


async def test_a_typed_head_count_without_an_opening_is_untouched() -> None:
    builder, built = await _opening()
    typed = _need(seating_capacity=SeatingCapacityConstraint.exactly(4))
    card = built.pending.model_copy(update={"opening": False})

    assert builder.typed_head_count(typed, card, 4) == typed


@pytest.mark.parametrize(
    ("always", "refusal"),
    [("[space]", "what the opening lacks"), ("[room, colour, style]", "at most 2")],
    ids=["not-in-the-opening", "more-than-two"],
)
def test_what_every_opening_asks_is_among_its_questions(
    tmp_path: Path, always: str, refusal: str
) -> None:
    source = tmp_path / "briefs.yaml"
    source.write_text(
        f"""
version: v1
opening_default: [room, colour]
rooms:
  - {{key: a, label: Living room, room: living room}}
  - {{key: b, label: Bedroom, room: bedroom}}
briefs:
  rugs:
    for: [carpet]
    ask: [budget, colour, style]
    opening: [room, colour, style]
    always: {always}
""",
        encoding="utf-8",
    )

    with pytest.raises(TaxonomyConfigurationError, match=refusal):
        load_briefs(path=source, taxonomy=TAXONOMY)


# ── a kind not chosen by its look ───────────────────────────────────────────


def test_a_mattress_is_shown_at_once_and_never_asked_about_its_look() -> None:
    """Under the bedding, its colour and style decide nothing."""
    briefs = load_briefs(taxonomy=TAXONOMY)

    assert briefs.for_opening("bedding", "mattresses") is None
    assert not briefs.by_look("mattresses")
    assert briefs.by_look("vase")
    assert briefs.for_narrowing("bedding", "mattresses").ask == (BriefQuestionKind.BUDGET,)


def test_a_kind_not_chosen_by_its_look_has_no_card(tmp_path: Path) -> None:
    """A card would ask about exactly what it is not chosen by."""
    source = tmp_path / "briefs.yaml"
    source.write_text(
        """
version: v1
opening_default: [room, colour, style]
not_by_look: [carpet]
rooms:
  - {key: a, label: Living room, room: living room}
  - {key: b, label: Other}
briefs:
  rugs:
    for: [carpet]
    ask: [budget, colour]
arabic: {Living room: غرفة المعيشة, Other: غير ذلك}
"""
    )

    with pytest.raises(TaxonomyConfigurationError):
        load_briefs(source, taxonomy=TAXONOMY)
