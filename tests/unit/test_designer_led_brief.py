"""Results with their brief: removable chips, and Narrow down pre-filled.

docs/designer-led-shopping-plan.md, phase 2. Beside a list of results they
searched for, what the search uses is shown as chips - ✕ runs it again without
that one thing - and Narrow down opens with every option, showing what is
already used, its answers replacing those values.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.schemas.agent_decision import AgentAction, CommercialReason, CustomerAgentDecision
from app.schemas.agent_state import ActiveSearchState, AgentStateV1
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PlanarDimensionConstraint,
    PriceConstraint,
    ProductSearchRequest,
    SeatingCapacityConstraint,
)
from app.schemas.grounding import TurnFailureCode
from app.schemas.language import ReplyLanguage
from app.schemas.product_brief import BriefMode
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    PlanarDimensionSemantics,
    SemanticPreference,
)
from app.schemas.search_action import BriefAnswerAction, DropFacetAction
from app.services.product_brief import without_facet
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.briefs import BriefQuestionKind, load_briefs
from app.taxonomy.dimensions import DimensionRole

from tests.unit.test_product_brief import TAXONOMY, _builder, _coordinator, _need
from tests.unit.test_turn_coordinator import CONTEXT

BUDGET, COLOUR, PEOPLE, TYPE = (
    BriefQuestionKind.BUDGET,
    BriefQuestionKind.COLOUR,
    BriefQuestionKind.PEOPLE,
    BriefQuestionKind.TYPE,
)


def _liked(family: AttributeFamily, value: str) -> SemanticPreference:
    return SemanticPreference(
        family=family,
        raw_value=value,
        canonical_value=value,
        strength=ConstraintStrength.PREFERRED,
    )


def _beige_sofas_under_2000() -> Any:
    return _need(price=PriceConstraint(currency="SAR", max_amount=Decimal("1900"))).model_copy(
        update={
            "semantic_preferences": (_liked(AttributeFamily.COLOR, "Beige"),),
            "seat_preference": 4,
        }
    )


async def _narrow(search: Any) -> Any:
    builder, _ = _builder()
    built = await builder.build(
        search, AgentStateV1(), CONTEXT, mode=BriefMode.NARROW, prefilled=True
    )
    assert built is not None
    return builder, built


def _question(built: Any, kind: BriefQuestionKind) -> Any:
    return next(q for q in built.card.questions if q.kind is kind)


# ── what Narrow down offers ─────────────────────────────────────────────────


def test_a_sofa_is_narrowed_by_its_card_and_its_head_count_a_new_type_by_default() -> None:
    briefs = load_briefs(taxonomy=TAXONOMY)

    assert briefs.for_narrowing("seating", "sofa").ask[:2] == (TYPE, PEOPLE)
    assert briefs.for_narrowing("decor", "vase").ask == (
        BUDGET,
        COLOUR,
        BriefQuestionKind.STYLE,
    )


async def test_narrow_down_offers_every_question_showing_what_is_used() -> None:
    _, built = await _narrow(_beige_sofas_under_2000())

    asked = [q.kind for q in built.card.questions]
    assert BUDGET in asked and PEOPLE in asked and COLOUR in asked
    assert _question(built, COLOUR).selected == ("Beige",)
    assert _question(built, PEOPLE).selected == ("people-4",)
    # 1,900 SAR is the first band's ceiling, so that band shows ticked.
    assert _question(built, BUDGET).selected == ("budget-1",)
    assert built.pending.replaces == (PEOPLE, BUDGET, COLOUR)


async def test_a_budget_on_a_band_edge_is_shown_selected() -> None:
    _, built = await _narrow(_need())
    band = built.pending.budgets[1]
    search = _need(
        price=PriceConstraint(
            currency=band.currency, min_amount=band.min_amount, max_amount=band.max_amount
        )
    )

    _, again = await _narrow(search)

    assert _question(again, BUDGET).selected == (band.key,)


async def test_an_untick_takes_the_value_away_and_the_kind_stays() -> None:
    """They untick Beige and the head count, and pick a new budget."""
    builder, built = await _narrow(_beige_sofas_under_2000())
    band = built.pending.budgets[-1].key

    answer = builder.answer(BriefAnswerAction(card=1, budget=band), built.pending)

    assert answer is not None
    search = answer.search
    assert search.semantic_preferences == ()
    assert search.seat_preference is None
    assert search.request.commerce_subcategory == "sofa"
    assert search.request.price is not None and search.request.price.max_amount is None


async def test_ticks_sent_back_unchanged_keep_the_search() -> None:
    builder, built = await _narrow(_beige_sofas_under_2000())

    answer = builder.answer(
        BriefAnswerAction(card=1, colours=("Beige",), people="people-4"), built.pending
    )

    assert answer is not None
    assert [p.canonical_value for p in answer.search.semantic_preferences] == ["Beige"]
    assert answer.search.seat_preference == 4


# ── the chips ───────────────────────────────────────────────────────────────


def _active(**request: Any) -> ActiveSearchState:
    semantics = (
        ConstraintSemantics(
            subcategory=ConstraintStrength.LOCKED,
            dimensions=(
                DimensionConstraintSemantics(
                    role=DimensionRole.OVERALL_WIDTH, strength=ConstraintStrength.LOCKED
                ),
            ),
        )
        if request.get("dimensions")
        else ConstraintSemantics(subcategory=ConstraintStrength.LOCKED)
    )
    return ActiveSearchState(
        request=ProductSearchRequest(
            commerce_category="seating", commerce_subcategory="sofa", **request
        ),
        semantics=semantics,
        semantic_preferences=(_liked(AttributeFamily.COLOR, "Beige"),),
        seat_preference=4,
        semantic_intent="velvet",
        revision=1,
    )


def _wide(cm: int) -> dict[str, Any]:
    return {
        "dimensions": (
            DimensionConstraint(
                role=DimensionRole.OVERALL_WIDTH,
                kind=DimensionConstraintKind.MAX,
                max_cm=Decimal(cm),
            ),
        )
    }


def test_the_chips_name_what_the_search_uses_in_the_reply_language() -> None:
    builder, _ = _builder()
    search = _active(
        price=PriceConstraint(currency="SAR", max_amount=Decimal("3000")), **_wide(220)
    )

    english = builder.chips(search, ReplyLanguage.EN)
    arabic = builder.chips(search, ReplyLanguage.AR)

    assert [(c.facet, c.label) for c in english] == [
        ("seats", "For 4"),
        ("price", "Under 3,000 SAR"),
        ("size", "Up to 220 cm wide"),
        ("colour:Beige", "Beige"),
    ]
    assert arabic[0].label == "لـ 4 أشخاص"
    assert arabic[3].label != "Beige"


def test_a_size_is_named_by_its_own_figures() -> None:
    """Never "your size": a rug's pair, a depth floor, a target."""
    builder, _ = _builder()
    deep = DimensionConstraint(
        role=DimensionRole.DEPTH, kind=DimensionConstraintKind.MIN, min_cm=Decimal(90)
    )
    around = DimensionConstraint(
        role=DimensionRole.OVERALL_WIDTH,
        kind=DimensionConstraintKind.TARGET,
        target_cm=Decimal("220.5"),
    )
    rug = ActiveSearchState(
        request=ProductSearchRequest(
            commerce_category="decor",
            commerce_subcategory="carpet",
            planar_dimensions=PlanarDimensionConstraint(
                first_cm=Decimal(300), second_cm=Decimal(200)
            ),
        ),
        semantics=ConstraintSemantics(
            planar_dimension=PlanarDimensionSemantics(strength=ConstraintStrength.LOCKED)
        ),
        revision=1,
    )
    sofa = ActiveSearchState(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sofa",
            dimensions=(around, deep),
        ),
        semantics=ConstraintSemantics(
            dimensions=tuple(
                DimensionConstraintSemantics(role=d.role, strength=ConstraintStrength.LOCKED)
                for d in (around, deep)
            )
        ),
        revision=1,
    )

    def size(search: ActiveSearchState, language: ReplyLanguage) -> str:
        return next(c.label for c in builder.chips(search, language) if c.facet == "size")

    assert size(rug, ReplyLanguage.EN) == "200 x 300 cm"
    assert size(sofa, ReplyLanguage.EN) == "Around 220.5 cm wide · At least 90 cm deep"
    assert size(sofa, ReplyLanguage.AR) == "العرض حوالي 220.5 سم · العمق 90 سم على الأقل"


def test_a_cross_removes_only_that_thing_and_starts_a_fresh_set() -> None:
    search = _active(
        price=PriceConstraint(currency="SAR", max_amount=Decimal("3000")),
        seating_capacity=SeatingCapacityConstraint.at_least(4),
        exclude_product_ids=(5, 6),
    )

    no_price = without_facet(search, "price")
    no_seats = without_facet(search, "seats")
    no_beige = without_facet(search, "colour:Beige")

    assert no_price is not None and no_price.request.price is None
    assert no_price.request.exclude_product_ids == ()
    assert no_price.seat_preference == 4
    assert no_seats is not None and no_seats.request.seating_capacity is None
    assert no_seats.seat_preference is None
    assert no_beige is not None and no_beige.semantic_preferences == ()
    # Their own wording is not a chip: it keeps ranking.
    assert no_beige.semantic_intent == "velvet"


def test_a_facet_the_search_does_not_use_removes_nothing() -> None:
    search = _active()

    assert without_facet(search, "size") is None
    assert without_facet(search, "colour:Black") is None
    assert without_facet(search, "words") is None
    assert without_facet(search, "made-up") is None


# ── beside results ──────────────────────────────────────────────────────────


def _search_decision() -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.SEARCH,
        commercial_reason=CommercialReason.CUSTOMER_REQUEST,
        skip_questions=True,
    )


async def test_results_carry_their_chips_and_narrow_down_beside_them() -> None:
    coordinator, pipeline = _coordinator(
        _search_decision(), _beige_sofas_under_2000(), designer_led_brief=True
    )

    result = await coordinator.run(
        CustomerTurnInput(message="just show me sofas", state=AgentStateV1(), context=CONTEXT)
    )

    assert pipeline.requests
    assert result.product_brief is None
    assert result.narrow_down is not None and result.narrow_down.mode is BriefMode.NARROW
    assert {chip.facet for chip in result.brief_chips} >= {"price", "colour:Beige"}
    assert result.state.product_brief.pending is not None
    assert result.state.product_brief.pending.replaces


async def test_switched_off_results_get_the_folded_card_once() -> None:
    coordinator, _ = _coordinator(_search_decision(), designer_led_brief=False)

    result = await coordinator.run(
        CustomerTurnInput(message="just show me sofas", state=AgentStateV1(), context=CONTEXT)
    )

    assert result.narrow_down is None and result.brief_chips == ()
    assert result.product_brief is not None and result.product_brief.mode is BriefMode.NARROW


async def test_a_cross_on_a_chip_runs_the_search_again_without_it() -> None:
    coordinator, pipeline = _coordinator(
        _search_decision(), _beige_sofas_under_2000(), designer_led_brief=True
    )
    shown = await coordinator.run(
        CustomerTurnInput(message="just show me sofas", state=AgentStateV1(), context=CONTEXT)
    )

    again = await coordinator.run(
        CustomerTurnInput(
            message="Without under 1,900 SAR",
            state=shown.state,
            context=CONTEXT,
            search_action=DropFacetAction(facet="price"),
        )
    )

    assert pipeline.requests[-1].request.price is None
    assert again.state.active_search is not None
    assert again.state.active_search.request.price is None
    assert again.narrow_down is not None


async def test_a_cross_on_something_no_longer_used_searches_nothing() -> None:
    coordinator, pipeline = _coordinator(_search_decision(), designer_led_brief=True)
    shown = await coordinator.run(
        CustomerTurnInput(message="just show me sofas", state=AgentStateV1(), context=CONTEXT)
    )
    ran = len(pipeline.requests)

    again = await coordinator.run(
        CustomerTurnInput(
            message="Without it",
            state=shown.state,
            context=CONTEXT,
            search_action=DropFacetAction(facet="price"),
        )
    )

    assert len(pipeline.requests) == ran
    assert again.grounding.failure is not None
    assert again.grounding.failure.code is TurnFailureCode.QUESTIONS_EXPIRED


async def test_a_budget_no_band_names_survives_an_unchanged_submit() -> None:
    """ "Under 3,000" matches no band, so nothing shows it ticked - and sending
    the card as it opened must not take it away."""
    search = _need(price=PriceConstraint(currency="SAR", max_amount=Decimal("3000")))
    builder, built = await _narrow(search)

    answer = builder.answer(BriefAnswerAction(card=1), built.pending)

    assert BUDGET not in built.pending.replaces
    assert answer is not None and answer.search.request.price == search.request.price


async def test_a_required_colour_re_ticked_stays_required() -> None:
    search = _need(colors_any_of=("Beige",))
    builder, built = await _narrow(search)

    answer = builder.answer(BriefAnswerAction(card=1, colours=("Beige",)), built.pending)

    assert answer is not None
    assert answer.search.request.colors_any_of == ("Beige",)


async def test_narrow_down_is_never_answered_by_typing() -> None:
    """A new need typed beside it still gets its opening."""
    builder, built = await _narrow(_need())
    state = AgentStateV1().model_copy(
        update={
            "product_brief": AgentStateV1().product_brief.model_copy(
                update={"pending": built.pending, "cards": 1}
            )
        }
    )

    assert builder.answers_card(_need(colors_any_of=("White",)), state) is False


def test_a_head_count_that_only_orders_is_said_as_who_it_is_for() -> None:
    builder, _ = _builder()

    chips = builder.chips(_active(), ReplyLanguage.EN)

    assert chips[0].label == "For 4"


async def test_an_unticked_colour_is_no_longer_remembered_as_theirs() -> None:
    """Unticked on Narrow down, a liked colour must not seed the search back
    in, and the reply is told the search was just narrowed."""
    coordinator, pipeline = _coordinator(
        _search_decision(), _beige_sofas_under_2000(), designer_led_brief=True
    )
    liked = AgentStateV1().model_copy(
        update={
            "customer_preferences": AgentStateV1().customer_preferences.model_copy(
                update={"semantic_preferences": (_liked(AttributeFamily.COLOR, "Beige"),)}
            )
        }
    )
    shown = await coordinator.run(
        CustomerTurnInput(message="just show me sofas", state=liked, context=CONTEXT)
    )
    assert shown.narrow_down is not None

    narrowed = await coordinator.run(
        CustomerTurnInput(
            message="Narrow down",
            state=shown.state,
            context=CONTEXT,
            search_action=BriefAnswerAction(card=shown.narrow_down.card),
        )
    )

    assert narrowed.narrowed
    assert narrowed.state.customer_preferences.semantic_preferences == ()
    assert not any(
        p.family is AttributeFamily.COLOR for p in pipeline.requests[-1].semantic_preferences
    )


async def test_a_required_colour_survives_a_card_that_never_asked_it() -> None:
    """ "Only dark grey sofas" answered on the opening, which asks the room and
    head count: the strict colour is still required (CLAUDE.md 12.4)."""
    builder, _ = _builder()
    built = await builder.build(
        _need(colors_any_of=("Grey", "Charcoal")),
        AgentStateV1(),
        CONTEXT,
        mode=BriefMode.ASK,
        opening=True,
    )
    assert built is not None and not built.pending.replaces

    answer = builder.answer(BriefAnswerAction(card=built.pending.card), built.pending)

    assert answer is not None
    assert answer.search.request.colors_any_of == ("Grey", "Charcoal")
