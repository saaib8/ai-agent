"""Taste after products (docs/designer-led-shopping-plan.md, phase 5).

Taste is caught quietly first - from what they liked, picked and asked more
like of - and only what nothing has told us is asked, softly, one question a
reply and each once a session: which of two feels more like them, which of
the store's styles, anything to avoid. An answer leans the cards and is never
a pick; what to avoid is pushed down, never hidden.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision, FollowUpGoal
from app.schemas.agent_state import ActiveSearchState, AgentStateV1, ProductInteractionState
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.catalog_overview import CatalogOverview, SubcategoryShelf
from app.schemas.grounding import (
    SearchExecutionGrounding,
    SearchOutcome,
    TurnFailureCode,
)
from app.schemas.language import ReplyLanguage
from app.schemas.next_step import NextStepKind
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.query import RankingLean
from app.schemas.relaxation import StopReason
from app.schemas.resolution import ProductSearchExecutionResult
from app.schemas.search_action import TasteAnswerAction
from app.schemas.taste import PendingTaste, TasteOption, TasteQuestionKind, TasteState
from app.services.grounding_builder import to_grounded_product
from app.services.product_brief import without_facet
from app.services.taste import learned_taste
from app.services.taste_question import NEITHER, answer, choose_question, on_screen
from app.taxonomy.attributes import AttributeFamily

from tests.unit.test_cross_sell_shows_products import _liking
from tests.unit.test_product_brief import _builder
from tests.unit.test_turn_coordinator import (
    CONTEXT,
    SOFAS,
    FakeCapabilities,
    FakeHydration,
    _coordinator,
    _product,
)

LOOKS: dict[int, tuple[str, tuple[str, ...]]] = {
    10: ("Beige", ("Modern",)),
    11: ("Beige", ("Modern",)),
    12: ("Walnut", ("Boho",)),
    13: ("Grey", ("Industrial",)),
}


def _card(pid: int, subcategory: str = "sofa") -> ProductCandidate:
    colour, styles = LOOKS[pid]
    return _product(pid).model_copy(
        update={
            "main_color": colour,
            "styles": styles,
            "commerce": CommerceClassification(category="seating", subcategory=subcategory),
        }
    )


class LookingPipeline:
    """Cards that differ in colour and style, recording what was asked."""

    def __init__(self, ids: tuple[int, ...] = (10, 11, 12, 13)) -> None:
        self.ids = ids
        self.calls: list[Any] = []

    async def execute(self, resolved: Any, context: Any, **_: Any) -> Any:
        self.calls.append(resolved)
        ids = tuple(i for i in self.ids if i not in resolved.request.exclude_product_ids)
        return ProductSearchExecutionResult(
            presented_product_ids=ids,
            grounding=SearchExecutionGrounding(
                outcome=SearchOutcome.RESULTS,
                products=tuple(
                    to_grounded_product(
                        _card(pid), grounding_ref=n, presented_ordinal=n, relaxation_depth=0
                    )
                    for n, pid in enumerate(ids, start=1)
                ),
                eligible_count=len(ids),
                ranked_count=len(ids),
                selected_count=len(ids),
                presented_count=len(ids),
                exact_candidate_count=len(ids),
                stop_reason=StopReason.EXACT_SUFFICIENT,
            ),
        )


class StyledShelves(FakeCapabilities):
    async def overview(self, context: Any) -> CatalogOverview:
        return CatalogOverview(
            store_id=50,
            shelves=(
                SubcategoryShelf(
                    commerce_category="seating",
                    commerce_subcategory="sofa",
                    active_count=40,
                    price_minimum=Decimal("900"),
                    price_maximum=Decimal("9000"),
                    styles=("Boho", "Industrial", "Modern"),
                ),
            ),
        )


class CardHydration(FakeHydration):
    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[Any, ...]:
        return tuple(_card(p) for p in product_ids if p in LOOKS)


def _engine(decision: CustomerAgentDecision | None = None, *, on: bool = True) -> Any:
    from app.schemas.query import ConstraintSemantics, ResolvedSearch

    return _coordinator(
        decision or CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=ResolvedSearch(request=SOFAS, semantics=ConstraintSemantics()),
        pipeline=LookingPipeline(),  # type: ignore[arg-type]
        capabilities=StyledShelves(),
        hydration=CardHydration(),
        designer_taste=on,
    )


def _fresh(**interaction: Any) -> AgentStateV1:
    return AgentStateV1(product_interaction=ProductInteractionState(**interaction))


def _turn(state: AgentStateV1, message: str = "show me sofas", **action: Any) -> CustomerTurnInput:
    return CustomerTurnInput(message=message, state=state, context=CONTEXT, **action)


# ── learned quietly ═════════════════════════════════════════════════════════


def test_learned_colour_is_from_the_same_kind_and_style_from_any() -> None:
    products = [_card(12, "carpet"), _card(13), _card(10)]

    colours, styles = learned_taste(products, "sofa")

    assert colours == ("Beige", "Grey")
    assert len(styles) == 2 and set(styles) <= {"Modern", "Industrial", "Boho"}


def test_learned_taste_favours_the_newest_on_a_tie() -> None:
    colours, _ = learned_taste([_card(13), _card(10)], "sofa")

    assert colours[0] == "Beige"


async def test_a_like_leans_the_next_search_and_shows_as_suggested() -> None:
    coordinator, _ = _engine()

    result = await coordinator.run(_turn(_fresh(liked_product_ids=(12,))))

    active = result.state.active_search
    assert active is not None and active.lean is not None and active.lean.learned
    assert (active.lean.learned_colours, active.lean.learned_styles) == (("Walnut",), ("Boho",))
    builder, _ = _builder()
    chips = builder.chips(active, ReplyLanguage.EN)
    assert {c.facet for c in chips} >= {"learned:colour:Walnut", "learned:style:Boho"}
    taken_off = without_facet(active, "learned:colour:Walnut")
    assert taken_off is not None and taken_off.lean is not None
    assert taken_off.lean.learned_colours == () and taken_off.lean.learned


async def test_more_like_this_is_remembered_as_a_taste_signal() -> None:
    from app.schemas.card_comparison import CardRef
    from app.schemas.product_action import MoreLikeThisAction
    from app.schemas.resolution import ResolvedProductReference

    from tests.unit.test_designer_led_buttons import ListReferences

    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        references=ListReferences(default=ResolvedProductReference(product_id=13)),
        hydration=CardHydration(),
        pipeline=LookingPipeline(),  # type: ignore[arg-type]
        capabilities=StyledShelves(),
        designer_led_buttons=True,
        designer_taste=True,
    )
    state = AgentStateV1(
        active_search=ActiveSearchState(request=SOFAS, revision=1),
        product_interaction=ProductInteractionState(
            presented_product_ids=(13,), presented_search_revision=1
        ),
    )

    result = await coordinator.run(
        _turn(
            state,
            "More like this",
            product_action=MoreLikeThisAction(card=CardRef(list_revision=1, ordinal=1)),
        )
    )

    assert result.state.product_interaction.explored_product_ids == (13,)


# ── which question, and when ═════════════════════════════════════════════════


def _search(**lean: Any) -> ActiveSearchState:
    return ActiveSearchState(
        request=SOFAS, revision=1, lean=RankingLean(learned=True, **lean) if lean else None
    )


def _cards() -> tuple[Any, ...]:
    return tuple(
        to_grounded_product(_card(pid), grounding_ref=n, presented_ordinal=n, relaxation_depth=0)
        for n, pid in enumerate((10, 11, 12, 13), start=1)
    )


STYLES = ("Boho", "Industrial", "Modern")


def test_the_first_question_is_which_of_the_two_most_different() -> None:
    pending = choose_question(_cards(), _search(), (), STYLES, TasteState(), list_revision=1)

    assert pending is not None and pending.kind is TasteQuestionKind.WHICH
    first, second = (o for o in pending.options if o.position is not None)
    assert first.colour != second.colour and not set(first.styles) & set(second.styles)
    assert pending.options[-1].key == NEITHER


def test_nothing_is_asked_that_likes_already_told_us() -> None:
    known = _search(learned_colours=("Beige",), learned_styles=("Modern",))

    pending = choose_question(_cards(), known, (), STYLES, TasteState(), list_revision=1)

    assert pending is not None and pending.kind is TasteQuestionKind.AVOID
    assert not {o.colour for o in pending.options} & {"Beige"}


def test_a_said_style_settles_the_style_question() -> None:
    said = _liking((AttributeFamily.STYLE, "Boho")).semantic_preferences
    asked = TasteState(asked=(TasteQuestionKind.WHICH,), asked_count=1)

    pending = choose_question(_cards(), _search(), said, STYLES, asked, list_revision=1)

    assert pending is not None and pending.kind is TasteQuestionKind.AVOID


def test_the_style_question_offers_the_stores_styles_on_screen_first() -> None:
    asked = TasteState(asked=(TasteQuestionKind.WHICH,), asked_count=1)

    pending = choose_question(_cards(), _search(), (), STYLES, asked, list_revision=1)

    assert pending is not None and pending.kind is TasteQuestionKind.STYLE
    assert [o.styles[0] for o in pending.options] == ["Modern", "Boho", "Industrial"]


def test_each_question_is_asked_once_a_session() -> None:
    every = TasteState(asked=tuple(TasteQuestionKind), asked_count=3)

    assert choose_question(_cards(), _search(), (), STYLES, every, list_revision=1) is None


# ── what an answer means ═════════════════════════════════════════════════════

WHICH = PendingTaste(
    question=1,
    kind=TasteQuestionKind.WHICH,
    commerce_subcategory="sofa",
    options=(
        TasteOption(key="card:1", position=1, colour="Beige", styles=("Modern",)),
        TasteOption(key="card:3", position=3, colour="Walnut", styles=("Boho",)),
        TasteOption(key=NEITHER),
    ),
)


@pytest.mark.parametrize(
    ("key", "styles", "colours", "leave_out"),
    [
        ("card:3", ("Boho",), ("Walnut",), ()),
        (NEITHER, (), (), (1, 3)),
    ],
)
def test_a_which_answer_teaches_taste_or_leaves_both_out(
    key: str, styles: tuple[str, ...], colours: tuple[str, ...], leave_out: tuple[int, ...]
) -> None:
    meaning = answer(WHICH, key)

    assert meaning is not None
    assert (meaning.styles, meaning.colours, meaning.leave_out) == (styles, colours, leave_out)


def test_a_key_the_question_never_offered_means_nothing() -> None:
    assert answer(WHICH, "card:2") is None


# ── end to end ══════════════════════════════════════════════════════════════


async def test_results_close_on_a_taste_question_in_place_of_the_old_follow_up() -> None:
    decision = CustomerAgentDecision(action=AgentAction.SEARCH, follow_up_goal=FollowUpGoal.COLOR)
    coordinator, _ = _engine(decision)

    result = await coordinator.run(_turn(_fresh()))

    assert result.next_step is not None and result.next_step.kind is NextStepKind.TASTE_WHICH
    actions = [c.search_action for c in result.next_step.chips]
    assert all(isinstance(a, TasteAnswerAction) for a in actions)
    assert result.state.taste.asked == (TasteQuestionKind.WHICH,)
    assert result.grounding.follow_up_policy.value == "none"


async def test_a_tapped_answer_leans_the_cards_and_is_never_a_pick() -> None:
    coordinator, parts = _engine()
    asked = await coordinator.run(_turn(_fresh()))
    pending = asked.state.taste.pending
    assert pending is not None
    card = next(o for o in pending.options if o.position is not None)

    answered = await coordinator.run(
        _turn(
            asked.state,
            "The first one feels more like me",
            search_action=TasteAnswerAction(question=pending.question, answer=card.key),
        )
    )

    said = {p.canonical_value for p in answered.state.customer_preferences.semantic_preferences}
    assert set(card.styles) <= said
    leaning = {p.canonical_value for p in parts["pipeline"].calls[-1].semantic_preferences}
    assert {card.colour, *card.styles} <= leaning
    assert answered.state.product_interaction.selected_product_ids == ()
    assert answered.state.taste.pending is None
    assert answered.taste_answered is not None
    # One answer is not an invitation to the next question.
    assert answered.next_step is None or not answered.next_step.kind.startswith("taste_")


async def test_neither_leaves_those_two_cards_out() -> None:
    coordinator, parts = _engine()
    asked = await coordinator.run(_turn(_fresh()))
    pending = asked.state.taste.pending
    assert pending is not None

    await coordinator.run(
        _turn(
            asked.state,
            "Neither",
            search_action=TasteAnswerAction(question=pending.question, answer=NEITHER),
        )
    )

    assert len(parts["pipeline"].calls[-1].request.exclude_product_ids) == 2


async def test_an_avoid_answer_pushes_down_and_hides_nothing() -> None:
    coordinator, parts = _engine()
    answered = await coordinator.run(
        _turn(
            _avoid_asked(),
            "Grey",
            search_action=TasteAnswerAction(question=3, answer="colour:Grey"),
        )
    )

    executed = parts["pipeline"].calls[-1]
    assert executed.lean.said_avoid_colours == ("Grey",)
    assert executed.request.colors_any_of == ()
    assert answered.state.customer_preferences.avoid_colours == ("Grey",)


def _avoid_asked() -> AgentStateV1:
    return AgentStateV1(
        active_search=ActiveSearchState(request=SOFAS, revision=1),
        product_interaction=ProductInteractionState(
            presented_product_ids=(10, 11, 12, 13), presented_search_revision=1
        ),
        taste=TasteState(
            asked=tuple(TasteQuestionKind),
            pending=PendingTaste(
                question=3,
                kind=TasteQuestionKind.AVOID,
                commerce_subcategory="sofa",
                list_revision=1,
                options=(TasteOption(key="colour:Grey", colour="Grey"),),
            ),
            asked_count=3,
        ),
    )


async def test_what_they_avoid_is_pushed_down_in_their_next_search_too() -> None:
    coordinator, parts = _engine()
    answered = await coordinator.run(
        _turn(
            _avoid_asked(),
            "Grey",
            search_action=TasteAnswerAction(question=3, answer="colour:Grey"),
        )
    )

    await coordinator.run(_turn(answered.state, "show me sofas"))

    assert parts["pipeline"].calls[-1].lean.said_avoid_colours == ("Grey",)


def test_customer_avoids_rank_ahead_of_learned_and_designer_leanings() -> None:
    from app.schemas.query import ResolvedSearch

    from tests.unit.test_designer_direction import _candidate, _order

    resolved = ResolvedSearch(
        request=SOFAS,
        lean=RankingLean(
            said_avoid_colours=("Grey",), learned_colours=("Grey",), colours=("Grey",)
        ),
    )

    assert _order(resolved, [_candidate(1, "Grey"), _candidate(2, "Beige")]) == [2, 1]


async def test_an_answer_to_an_old_question_changes_nothing() -> None:
    coordinator, _ = _engine()
    asked = await coordinator.run(_turn(_fresh()))

    stale = await coordinator.run(
        _turn(asked.state, "first", search_action=TasteAnswerAction(question=9, answer="card:1"))
    )

    assert stale.grounding.failure is not None
    assert stale.grounding.failure.code is TurnFailureCode.QUESTIONS_EXPIRED


async def test_a_typed_answer_reads_as_the_tap() -> None:
    coordinator, _ = _engine()
    asked = await coordinator.run(_turn(_fresh()))
    pending = asked.state.taste.pending
    assert pending is not None
    card = next(o for o in pending.options if o.position is not None)
    typed_engine, _ = _engine(
        CustomerAgentDecision(action=AgentAction.ANSWER, taste_answer=card.key)
    )

    answered = await typed_engine.run(_turn(asked.state, "that one feels more me"))

    assert answered.taste_answered is not None
    assert answered.state.product_interaction.selected_product_ids == ()


async def test_switched_off_nothing_is_learned_or_asked() -> None:
    coordinator, _ = _engine(on=False)

    result = await coordinator.run(_turn(_fresh(liked_product_ids=(12,))))

    assert result.state.active_search.lean is None
    assert result.next_step is None or not result.next_step.kind.startswith("taste_")


# ── after QA ════════════════════════════════════════════════════════════════


async def test_no_taste_question_when_they_declined_questions() -> None:
    coordinator, _ = _engine(CustomerAgentDecision(action=AgentAction.SEARCH, skip_questions=True))

    result = await coordinator.run(_turn(_fresh()))

    assert result.state.taste.pending is None
    assert result.next_step is None or not result.next_step.kind.startswith("taste_")


async def test_a_question_whose_list_was_replaced_answers_nothing() -> None:
    coordinator, _ = _engine()
    asked = await coordinator.run(_turn(_fresh()))
    pending = asked.state.taste.pending
    assert pending is not None and on_screen(asked.state) == pending
    assert asked.state.active_search is not None and pending.list_revision is not None
    moved_on = asked.state.model_copy(
        update={
            "active_search": asked.state.active_search.model_copy(
                update={"revision": pending.list_revision + 1}
            ),
            "product_interaction": asked.state.product_interaction.model_copy(
                update={"presented_search_revision": pending.list_revision + 1}
            ),
        }
    )
    card = next(o for o in pending.options if o.position is not None)
    typed, _ = _engine(CustomerAgentDecision(action=AgentAction.ANSWER, taste_answer=card.key))

    later = await typed.run(_turn(moved_on, "I like the first one"))

    assert on_screen(moved_on) is None
    assert later.taste_answered is None


async def test_a_turn_about_a_card_is_never_a_taste_answer() -> None:
    coordinator, _ = _engine()
    asked = await coordinator.run(_turn(_fresh()))
    pending = asked.state.taste.pending
    assert pending is not None
    card = next(o for o in pending.options if o.position is not None)
    misread, _ = _engine(
        CustomerAgentDecision(action=AgentAction.SHOW_SELECTION, taste_answer=card.key)
    )

    result = await misread.run(_turn(asked.state, "show me my picks"))

    assert result.taste_answered is None


async def test_a_learned_colour_is_worked_out_again_for_another_kind() -> None:
    from app.schemas.composition import ComposedSearch
    from app.schemas.query import ResolvedSearch

    coordinator, _ = _engine()
    for_sofas = RankingLean(learned=True, learned_for="sofa", learned_colours=("Beige",))
    sectionals = SOFAS.model_copy(update={"commerce_subcategory": "sectional-sofa"})
    composed = ComposedSearch(
        candidate=ActiveSearchState(request=sectionals, revision=1, lean=for_sofas),
        resolved=ResolvedSearch(request=sectionals, lean=for_sofas),
    )

    leaning = await coordinator._with_learned_taste(
        composed, _fresh(liked_product_ids=(10,)), CONTEXT
    )

    lean = leaning.candidate.lean
    assert lean is not None and lean.learned_for == "sectional-sofa"
    assert lean.learned_colours == () and lean.learned_styles == ("Modern",)


def test_twins_with_no_styles_are_never_offered_as_a_choice() -> None:
    plain = tuple(
        to_grounded_product(
            _card(pid).model_copy(update={"styles": ()}),
            grounding_ref=n,
            presented_ordinal=n,
            relaxation_depth=0,
        )
        for n, pid in enumerate((10, 11), start=1)
    )

    pending = choose_question(plain, _search(), (), STYLES, TasteState(), list_revision=1)

    assert pending is None or pending.kind is not TasteQuestionKind.WHICH


def test_the_avoid_question_never_offers_what_they_required() -> None:
    required = _search(learned_styles=("Modern",)).model_copy(
        update={"request": SOFAS.model_copy(update={"colors_any_of": ("Beige",)})}
    )
    asked = TasteState(asked=(TasteQuestionKind.WHICH, TasteQuestionKind.STYLE), asked_count=2)

    pending = choose_question(_cards(), required, (), STYLES, asked, list_revision=1)

    assert pending is not None and pending.kind is TasteQuestionKind.AVOID
    assert "Beige" not in {o.colour for o in pending.options}
