"""The card of questions for a stated need, and what its answers search.

"I need a sofa" is answered with one card - the kind, the budget, colours, the
feel, the style - built from reviewed data and the live catalog, and read back
through the card the session remembers (CLAUDE.md 10.4). The catalog is faked
here; the registries, the builder, the composer and the routing are the real
ones, because they are pure and faking them would only test the fake.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from app.core.config import CustomerAgentSettings
from app.core.exceptions import PickUnavailableError, TaxonomyConfigurationError
from app.repositories.products import BriefFacts, ProductRepository
from app.schemas.agent_decision import AgentAction, CommercialReason, CustomerAgentDecision
from app.schemas.agent_state import (
    MAX_EARLIER_LISTS,
    ActiveSearchState,
    AgentStateV1,
    CustomerPreferenceState,
    PresentedList,
    ProductInteractionState,
)
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.catalog_overview import CatalogOverview, SeatingSpread, SubcategoryShelf
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import ProductSearchRequest, ProductSort, SeatingCapacityConstraint
from app.schemas.grounding import SearchExecutionGrounding, SearchOutcome, TurnFailureCode
from app.schemas.picks import PicksRequest, SelectPickAction
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.product_brief import BriefMode
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    ResolvedSearch,
    SemanticPreference,
)
from app.schemas.relaxation import StopReason
from app.schemas.resolution import ProductSearchExecutionResult
from app.schemas.response import ResponseGroundingView, ResponseOutcomeKind
from app.schemas.search_action import BriefAnswerAction
from app.services.agent_state import commit_search_results, record_brief
from app.services.agent_view import project_state
from app.services.bundle_reference import BundleReferenceResolver
from app.services.cross_sell import CompanionSearchBuilder
from app.services.grounding_builder import to_grounded_product
from app.services.picks import PicksRuntime
from app.services.product_brief import ProductBriefBuilder
from app.services.product_interaction import build_picks
from app.services.reference_resolver import ProductReferenceResolver
from app.services.refinement_composer import SearchRefinementComposer
from app.services.response_view import best_match_first, route_response
from app.services.similar_search import SimilarSearchBuilder
from app.services.turn_coordinator import CustomerTurnCoordinator, _earlier_seat_count
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes
from app.taxonomy.briefs import BriefQuestionKind, load_briefs
from app.taxonomy.complements import load_complements
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics

from tests.unit.test_chat_api import FakeSessionStore
from tests.unit.test_picks import SESSION, STORE, _stored
from tests.unit.test_reference_resolver import FakeRepository, _row
from tests.unit.test_turn_coordinator import (
    CONTEXT,
    FakeCapabilities,
    FakeComparison,
    FakeDecisions,
    FakeDesign,
    FakeDesignDiscovery,
    FakeOptimizer,
    FakeQueryUnderstanding,
    FakeRelativePrice,
    FakeSeatingPlanner,
)

TAXONOMY = load_taxonomy()
ATTRIBUTES = load_catalog_attributes()
DIMENSIONS = load_dimension_semantics(taxonomy=TAXONOMY)
BRIEFS = load_briefs(taxonomy=TAXONOMY)
COMPLEMENTS = load_complements(taxonomy=TAXONOMY)
ROOMS = load_room_pieces(taxonomy=TAXONOMY, seating=load_seating_semantics(taxonomy=TAXONOMY))

SOFA_FACTS = BriefFacts(
    kinds=(
        ("sofa", 2, 69),
        ("sofa", 3, 77),
        ("sofa", 4, 19),
        ("sofa", None, 7),
        ("sectional-sofa", 3, 7),
        ("sofa-set", 6, 9),
        # No sofa beds: that kind is not offered.
    ),
    currency="SAR",
    price_quartiles=(Decimal("1850"), Decimal("2740"), Decimal("3950")),
    colors=(("Beige", 80), ("Light Grey", 68), ("Grey", 24), ("Not A Colour", 9)),
    styles=(("Minimalist", 201), ("Modern", 189), ("Scandinavian", 19)),
)


class FakeFacts:
    """The catalog's counts for a card, recording which types were asked."""

    def __init__(self, facts: BriefFacts = SOFA_FACTS) -> None:
        self.facts = facts
        self.calls: list[tuple[str, ...]] = []

    async def brief_facts(self, subcategories: Any, context: Any) -> BriefFacts:
        self.calls.append(tuple(subcategories))
        return self.facts


def _builder(facts: BriefFacts = SOFA_FACTS) -> tuple[ProductBriefBuilder, FakeFacts]:
    repository = FakeFacts(facts)
    return (
        ProductBriefBuilder(cast(ProductRepository, repository), BRIEFS, ATTRIBUTES, TAXONOMY),
        repository,
    )


def _need(
    subcategory: str = "sofa",
    category: str = "seating",
    *,
    semantic_text: str | None = None,
    preferences: tuple[SemanticPreference, ...] = (),
    **request: Any,
) -> ResolvedSearch:
    return ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category=category, commerce_subcategory=subcategory, **request
        ),
        semantics=ConstraintSemantics(subcategory=ConstraintStrength.LOCKED),
        semantic_preferences=preferences,
        semantic_text=semantic_text,
    )


def _asked(built: Any) -> list[BriefQuestionKind]:
    return [question.kind for question in built.card.questions]


def _labels(built: Any, kind: BriefQuestionKind) -> list[str]:
    question = next(q for q in built.card.questions if q.kind is kind)
    return [choice.label for choice in question.choices]


# ── the reviewed cards ══════════════════════════════════════════════════════


def test_every_card_loads_against_the_taxonomy() -> None:
    assert BRIEFS.for_type("sofa") is BRIEFS.for_type("sectional-sofa")
    assert BRIEFS.for_type("carpet") is not None
    assert BRIEFS.for_type("vase") is None
    assert BRIEFS.for_type(None) is None


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("sofas: {for: [not-a-type], ask: [budget]}", "not an approved subcategory"),
        (
            "sofas: {for: [sofa], ask: [type], type: [{label: A, subcategory: bed}, "
            "{label: B, subcategory: sofa}]}",
            "one of the card's subcategories",
        ),
        (
            "beds: {for: [bed], ask: [type], type: [{label: A, subcategory: bed, seats: 2}, "
            "{label: B, subcategory: bed}]}",
            "only seating has seats",
        ),
        ("sofas: {for: [sofa], ask: [type]}", "kinds are listed exactly when"),
        ("sofas: {for: [sofa], ask: [feel]}", "feels are listed exactly when"),
        ("sofas: {for: [sofa], ask: [budget, budget]}", "repeats a question"),
        ("sofas: {for: [sofa], ask: [price]}", "unknown question"),
        (
            "a: {for: [sofa], ask: [budget]}\n  b: {for: [sofa], ask: [colour]}",
            "already has a card",
        ),
        (
            "sofas: {for: [sofa], ask: [feel], feel: {label: Feel, options: "
            "[{label: A, words: a}]}}",
            "options",
        ),
        (
            "sofas: {for: [sofa], for_category: [seating], ask: [budget]}",
            "must ask the kind",
        ),
        ("sofas: {for: [sofa], for_category: [furniture], ask: [budget]}", "approved category"),
        (
            "a: {for: [sofa], for_category: [seating], ask: [type], type: "
            "[{label: A, subcategory: sofa, seats: 2}, {label: B, subcategory: sofa, seats: 3}]}"
            "\n  b: {for: [chair], for_category: [seating], ask: [type], type: "
            "[{label: A, subcategory: chair}, {label: B, subcategory: chair, seats: 1}]}",
            "already has a card",
        ),
    ],
)
def test_a_malformed_card_fails_startup(tmp_path: Path, body: str, problem: str) -> None:
    source = tmp_path / "briefs.yaml"
    source.write_text(f"version: v1\nbriefs:\n  {body}\n", encoding="utf-8")

    with pytest.raises(TaxonomyConfigurationError) as refused:
        load_briefs(source, taxonomy=TAXONOMY)

    assert problem in str(refused.value.context.get("detail", refused.value))


# ── building the card ═══════════════════════════════════════════════════════


async def test_a_bare_need_is_asked_everything_in_card_order() -> None:
    builder, facts = _builder()

    built = await builder.build(_need(), AgentStateV1(), CONTEXT, mode=BriefMode.ASK)

    assert built is not None
    assert _asked(built) == [
        BriefQuestionKind.TYPE,
        BriefQuestionKind.BUDGET,
        BriefQuestionKind.COLOUR,
        BriefQuestionKind.FEEL,
        BriefQuestionKind.STYLE,
    ]
    # The facts cover every kind the card may reach, not only the one named.
    assert set(facts.calls[0]) == {"sofa", "sectional-sofa", "sofa-set", "sofa-bed"}
    assert built.card.submit_label == "Show me sofas"
    assert built.card.card == built.pending.card == 1


async def test_only_stocked_kinds_are_offered_and_seats_count_only_when_reviewed() -> None:
    facts = BriefFacts(
        kinds=(("sofa", 3, 5), ("sofa", None, 40), ("sofa-set", 6, 2)),
        currency="SAR",
        price_quartiles=None,
        colors=(),
        styles=(),
    )
    builder, _ = _builder(facts)

    built = await builder.build(_need(), AgentStateV1(), CONTEXT, mode=BriefMode.ASK)

    assert built is not None
    # 40 sofas with no reviewed capacity make no 2-seater and no 4+ seater.
    assert _labels(built, BriefQuestionKind.TYPE) == ["3-seater", "Sofa set"]


async def test_the_kind_the_store_has_most_of_comes_first() -> None:
    builder, _ = _builder()

    built = await builder.build(_need(), AgentStateV1(), CONTEXT, mode=BriefMode.ASK)

    assert built is not None
    assert _labels(built, BriefQuestionKind.TYPE) == [
        "3-seater",
        "2-seater",
        "4+ seater",
        "Sofa set",
        "L-shape",
    ]


async def test_colours_are_counted_after_spelling_is_normalised() -> None:
    """Two merchants' "beige" and "Beige" are one colour, and together they
    outnumber the grey."""
    facts = BriefFacts(
        kinds=(("sofa", 3, 5),),
        currency=None,
        price_quartiles=None,
        colors=(("Grey", 30), ("Beige", 20), ("beige", 15)),
        styles=(),
    )
    builder, _ = _builder(facts)

    built = await builder.build(
        _need(seating_capacity=SeatingCapacityConstraint.exactly(3)),
        AgentStateV1(),
        CONTEXT,
        mode=BriefMode.ASK,
    )

    assert built is not None
    assert _labels(built, BriefQuestionKind.COLOUR) == ["Beige", "Grey"]


async def test_budget_bands_follow_the_quartiles_rounded_as_a_person_says_them() -> None:
    builder, _ = _builder()

    built = await builder.build(_need(), AgentStateV1(), CONTEXT, mode=BriefMode.ASK)

    assert built is not None
    assert _labels(built, BriefQuestionKind.BUDGET) == [
        "Under 1,900 SAR",
        "1,900-2,700 SAR",
        "2,700-4,000 SAR",
        "Over 4,000 SAR",
    ]


async def test_colours_and_styles_are_approved_values_most_common_first() -> None:
    builder, _ = _builder()

    built = await builder.build(_need(), AgentStateV1(), CONTEXT, mode=BriefMode.ASK)

    assert built is not None
    assert _labels(built, BriefQuestionKind.COLOUR) == ["Beige", "Light Grey", "Grey"]
    assert _labels(built, BriefQuestionKind.STYLE) == ["Minimalist", "Modern", "Scandinavian"]
    colour = next(q for q in built.card.questions if q.kind is BriefQuestionKind.COLOUR)
    assert colour.max_choices > 1


async def test_what_they_already_said_is_not_asked_again() -> None:
    builder, _ = _builder()
    grey = SemanticPreference(
        family=AttributeFamily.COLOR,
        raw_value="grey",
        canonical_value="Grey",
        strength=ConstraintStrength.PREFERRED,
    )
    need = _need(
        preferences=(grey,),
        semantic_text="soft boucle",
        seating_capacity=SeatingCapacityConstraint.exactly(3),
    )

    built = await builder.build(need, AgentStateV1(), CONTEXT, mode=BriefMode.ASK)

    assert built is not None
    assert _asked(built) == [BriefQuestionKind.BUDGET, BriefQuestionKind.STYLE]


async def test_a_colour_on_record_from_earlier_counts_as_said() -> None:
    builder, _ = _builder()
    remembered = AgentStateV1(
        customer_preferences=CustomerPreferenceState(
            semantic_preferences=(
                SemanticPreference(
                    family=AttributeFamily.STYLE,
                    raw_value="modern",
                    canonical_value="Modern",
                    strength=ConstraintStrength.PREFERRED,
                ),
            )
        )
    )

    built = await builder.build(_need(), remembered, CONTEXT, mode=BriefMode.ASK)

    assert built is not None
    assert BriefQuestionKind.STYLE not in _asked(built)


async def test_words_that_name_no_feel_still_ask_the_feel() -> None:
    builder, _ = _builder()

    built = await builder.build(
        _need(semantic_text="for my living room"), AgentStateV1(), CONTEXT, mode=BriefMode.ASK
    )

    assert built is not None
    assert BriefQuestionKind.FEEL in _asked(built)


async def test_a_need_stated_again_gets_its_card_again() -> None:
    """ "I need a bed" an hour later is a new need - even if an earlier bed card
    was left unanswered."""
    builder, _ = _builder()
    shown = record_brief(AgentStateV1(), None, shown="sofas")

    again = await builder.build(_need("sectional-sofa"), shown, CONTEXT, mode=BriefMode.ASK)

    assert again is not None


async def test_the_folded_card_is_offered_once_per_family() -> None:
    builder, _ = _builder()
    shown = record_brief(AgentStateV1(), None, shown="sofas")

    folded = await builder.build(_need("sectional-sofa"), shown, CONTEXT, mode=BriefMode.NARROW)

    assert folded is None


async def test_a_type_with_no_card_or_nothing_left_to_ask_is_just_searched() -> None:
    builder, _ = _builder()
    everything = _need(
        "sofa-set",
        semantic_text="velvet",
        price={"currency": "SAR", "max_amount": Decimal("5000")},
        colors_any_of=("Beige",),
        styles_all_of=("Modern",),
    )

    assert (
        await builder.build(_need("vase", "decor"), AgentStateV1(), CONTEXT, mode=BriefMode.ASK)
        is None
    )
    assert await builder.build(everything, AgentStateV1(), CONTEXT, mode=BriefMode.ASK) is None


async def test_the_card_names_what_they_asked_for() -> None:
    builder, _ = _builder()

    sectional = await builder.build(
        _need("sectional-sofa"), AgentStateV1(), CONTEXT, mode=BriefMode.ASK
    )
    rug = await builder.build(_need("carpet", "decor"), AgentStateV1(), CONTEXT, mode=BriefMode.ASK)

    assert sectional is not None and sectional.card.submit_label == "Show me sectional sofas"
    assert rug is not None and rug.card.submit_label == "Show me rugs"


async def test_cards_are_numbered_through_the_session() -> None:
    builder, _ = _builder()
    first = await builder.build(_need(), AgentStateV1(), CONTEXT, mode=BriefMode.ASK)
    assert first is not None
    after = record_brief(AgentStateV1(), first.pending, shown=first.pending.name)

    rug = await builder.build(_need("carpet", "decor"), after, CONTEXT, mode=BriefMode.NARROW)

    assert rug is not None and rug.card.card == 2 and rug.card.mode is BriefMode.NARROW


TABLE_FACTS = BriefFacts(
    kinds=(("center-table", None, 41), ("service-table", None, 63), ("dining-table", None, 4)),
    currency="SAR",
    price_quartiles=(Decimal("630"), Decimal("990"), Decimal("1500")),
    colors=(("Walnut", 30), ("Black", 20)),
    styles=(("Modern", 90), ("Minimalist", 70)),
)


async def test_a_need_that_names_no_kind_is_asked_the_kind_first() -> None:
    """ "I need a table" names a category, not a kind: the card settles it by
    a tap rather than a guess (CLAUDE.md 14.5)."""
    builder, facts = _builder(TABLE_FACTS)

    built = await builder.build(
        ResolvedSearch(request=ProductSearchRequest(commerce_category="tables")),
        AgentStateV1(),
        CONTEXT,
        mode=BriefMode.ASK,
    )

    assert built is not None
    assert _asked(built)[0] is BriefQuestionKind.TYPE
    # Only kinds the store stocks: no TV units, consoles or nightstands here.
    # Most-stocked first: side tables, then coffee tables, then dining tables.
    assert _labels(built, BriefQuestionKind.TYPE) == ["Side table", "Coffee table", "Dining table"]
    assert "dining-table" in facts.calls[0]
    assert built.card.submit_label == "Show me tables"


async def test_a_kind_from_a_whole_category_card_can_move_to_its_own_category() -> None:
    builder, _ = _builder(TABLE_FACTS)
    built = await builder.build(
        ResolvedSearch(request=ProductSearchRequest(commerce_category="tables")),
        AgentStateV1(),
        CONTEXT,
        mode=BriefMode.ASK,
    )
    assert built is not None

    search = builder.answer(BriefAnswerAction(card=1, piece="dining-table"), built.pending)

    assert search is not None
    assert (search.request.commerce_category, search.request.commerce_subcategory) == (
        "dining",
        "dining-table",
    )
    assert search.semantics.subcategory is ConstraintStrength.LOCKED


async def test_a_category_with_no_card_is_just_searched() -> None:
    builder, _ = _builder()

    built = await builder.build(
        ResolvedSearch(request=ProductSearchRequest(commerce_category="seating")),
        AgentStateV1(),
        CONTEXT,
        mode=BriefMode.ASK,
    )

    assert built is None


# ── reading the answers ═════════════════════════════════════════════════════


async def _pending() -> Any:
    builder, _ = _builder()
    built = await builder.build(_need(), AgentStateV1(), CONTEXT, mode=BriefMode.ASK)
    assert built is not None
    return builder, built.pending


def _key(pending: Any, field: str, index: int) -> str:
    return str(getattr(pending, field)[index].key)


async def test_a_kind_and_a_budget_become_requirements() -> None:
    builder, pending = await _pending()

    search = builder.answer(
        BriefAnswerAction(card=1, piece="sofa:3", budget=_key(pending, "budgets", 1)), pending
    )

    assert search is not None
    request, semantics = search.request, search.semantics
    assert request.seating_capacity == SeatingCapacityConstraint.exactly(3)
    assert (semantics.seating_min, semantics.seating_max) == (ConstraintStrength.LOCKED,) * 2
    assert request.price is not None
    assert (request.price.min_amount, request.price.max_amount) == (Decimal(1900), Decimal(2700))
    # The ceiling is theirs; the floor is only where their money is.
    assert semantics.price_max is ConstraintStrength.LOCKED
    assert semantics.price_min is ConstraintStrength.PREFERRED


async def test_a_different_kind_changes_the_type_and_a_minimum_seats() -> None:
    builder, pending = await _pending()

    four = builder.answer(BriefAnswerAction(card=1, piece="sofa:4+"), pending)
    corner = builder.answer(BriefAnswerAction(card=1, piece="sectional-sofa"), pending)

    assert four is not None and four.request.seating_capacity == SeatingCapacityConstraint(
        min_capacity=4
    )
    assert corner is not None
    assert corner.request.commerce_subcategory == "sectional-sofa"
    assert corner.request.seating_capacity is None


async def test_colours_styles_and_a_feel_rank_and_never_filter() -> None:
    builder, pending = await _pending()

    search = builder.answer(
        BriefAnswerAction(
            card=1,
            colours=("Beige", "Grey"),
            styles=("Modern",),
            feel=_key(pending, "feels", 0),
        ),
        pending,
    )

    assert search is not None
    assert search.request.colors_any_of == () and search.request.styles_all_of == ()
    assert [p.canonical_value for p in search.semantic_preferences] == ["Beige", "Grey", "Modern"]
    assert all(p.strength is ConstraintStrength.PREFERRED for p in search.semantic_preferences)
    assert search.semantic_text == "textured boucle fabric"


@pytest.mark.parametrize(
    "answer",
    [
        BriefAnswerAction(card=2),
        BriefAnswerAction(card=1, piece="sofa:9"),
        BriefAnswerAction(card=1, budget="budget-99"),
        BriefAnswerAction(card=1, colours=("Purple",)),
        BriefAnswerAction(card=1, feel="feel-99"),
    ],
)
async def test_a_choice_the_card_never_offered_searches_nothing(answer: BriefAnswerAction) -> None:
    builder, pending = await _pending()

    assert builder.answer(answer, pending) is None


# ── what the session remembers ══════════════════════════════════════════════


async def test_a_card_is_recorded_shown_counted_and_answered() -> None:
    _, pending = await _pending()

    on_screen = record_brief(AgentStateV1(), pending, shown=pending.name)
    answered = record_brief(on_screen, None)

    assert on_screen.product_brief.pending == pending
    assert on_screen.product_brief.cards == 1
    assert answered.product_brief.pending is None
    # Answered, the card stays shown and counted: it is never asked again.
    assert answered.product_brief.shown == ("sofas",)
    assert answered.product_brief.cards == 1
    assert record_brief(answered, None, shown="sofas").product_brief.shown == ("sofas",)


def _presented(revision: int, ids: tuple[int, ...], earlier: Any = ()) -> AgentStateV1:
    return AgentStateV1(
        active_search=ActiveSearchState(
            request=ProductSearchRequest(commerce_category="seating"), revision=revision
        ),
        product_interaction=ProductInteractionState(
            presented_product_ids=ids, presented_search_revision=revision, earlier_lists=earlier
        ),
    )


def test_a_new_list_keeps_the_last_one_tickable() -> None:
    committed = commit_search_results(_presented(1, (1, 2, 3)), (7, 8, 9))

    interaction = committed.product_interaction
    assert interaction.presented_search_revision == 2
    assert interaction.earlier_lists == (PresentedList(revision=1, product_ids=(1, 2, 3)),)


def test_only_the_last_few_lists_stay_tickable() -> None:
    state = _presented(1, (1,))
    for revision in range(2, MAX_EARLIER_LISTS + 4):
        state = commit_search_results(state, (revision * 10,))

    earlier = state.product_interaction.earlier_lists
    current = state.product_interaction.presented_search_revision
    assert current is not None
    assert len(earlier) == MAX_EARLIER_LISTS
    assert earlier[-1].revision == current - 1


def test_with_no_card_on_screen_the_decision_model_sees_none() -> None:
    pending_state = record_brief(AgentStateV1(), None, shown="sofas")
    assert project_state(pending_state).question_card is None


async def test_a_pending_card_is_projected_in_customer_words() -> None:
    _, pending = await _pending()
    state = record_brief(AgentStateV1(), pending, shown=pending.name)

    view = project_state(state).question_card

    assert view is not None and view.looking_for == "sofa"


# ── the turn ════════════════════════════════════════════════════════════════


class Pipeline:
    """One page of three, recording what ran."""

    def __init__(self, ranked: bool = True) -> None:
        self.requests: list[ResolvedSearch] = []
        self.ranked = ranked

    async def execute(self, resolved: Any, context: Any, **_: Any) -> ProductSearchExecutionResult:
        self.requests.append(resolved)
        ids = (31, 32, 33)
        return ProductSearchExecutionResult(
            presented_product_ids=ids,
            grounding=SearchExecutionGrounding(
                outcome=SearchOutcome.RESULTS,
                products=tuple(
                    to_grounded_product(
                        _sofa(pid), grounding_ref=n, presented_ordinal=n, relaxation_depth=0
                    )
                    for n, pid in enumerate(ids, start=1)
                ),
                eligible_count=3,
                ranked_count=3,
                selected_count=3,
                presented_count=3,
                exact_candidate_count=3,
                stop_reason=StopReason.EXACT_SUFFICIENT,
                semantic_used=self.ranked,
            ),
        )


class Catalog:
    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[ProductCandidate, ...]:
        return tuple(_sofa(pid) for pid in product_ids)


def _sofa(
    product_id: int, subcategory: str = "sofa", category: str = "seating"
) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Piece {product_id}",
        name_arabic="قطعة",
        price_amount=Decimal("2000"),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/p/{product_id}",
        commerce=CommerceClassification(category=category, subcategory=subcategory),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


def _coordinator(
    decision: CustomerAgentDecision,
    need: ResolvedSearch | None = None,
    capabilities: Any = None,
    *,
    pipeline: Any = None,
    closest_type: Any = None,
    arabic_replies: bool = False,
) -> tuple[CustomerTurnCoordinator, Any]:
    pipeline = pipeline or Pipeline()
    builder, _ = _builder()
    coordinator = CustomerTurnCoordinator(
        FakeDecisions(decision),  # type: ignore[arg-type]
        FakeQueryUnderstanding(need or _need()),  # type: ignore[arg-type]
        SearchRefinementComposer(ATTRIBUTES, DIMENSIONS, None),
        None,  # type: ignore[arg-type]
        FakeRelativePrice(None),  # type: ignore[arg-type]
        FakeComparison(None),  # type: ignore[arg-type]
        pipeline,  # type: ignore[arg-type]
        Catalog(),  # type: ignore[arg-type]
        SimilarSearchBuilder(TAXONOMY, ATTRIBUTES),
        capabilities or FakeCapabilities(),  # type: ignore[arg-type]
        FakeDesign(),  # type: ignore[arg-type]
        FakeDesignDiscovery(),  # type: ignore[arg-type]
        BundleReferenceResolver(TAXONOMY),
        FakeOptimizer(),  # type: ignore[arg-type]
        FakeSeatingPlanner(),  # type: ignore[arg-type]
        DIMENSIONS,
        TAXONOMY,
        complements=COMPLEMENTS,
        companion_search=CompanionSearchBuilder(ATTRIBUTES),
        briefs=builder,
        closest_type=closest_type,
        arabic_replies=arabic_replies,
    )
    return coordinator, pipeline


def _search(*, skip_questions: bool = False) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.SEARCH,
        commercial_reason=CommercialReason.CUSTOMER_REQUEST,
        skip_questions=skip_questions,
    )


def _typed(state: AgentStateV1, message: str = "I need a sofa") -> CustomerTurnInput:
    return CustomerTurnInput(message=message, state=state, context=CONTEXT)


@pytest.mark.parametrize(
    "message",
    ["I need a sofa", "find me a sofa", "I'd like to see some sofas", "show me sofas"],
)
async def test_a_search_for_a_kind_shows_the_card_and_searches_nothing(message: str) -> None:
    """However they put it: needing one, finding one, seeing some."""
    coordinator, pipeline = _coordinator(_search())

    result = await coordinator.run(_typed(AgentStateV1(), message))

    assert pipeline.requests == []
    assert result.product_brief is not None and result.product_brief.mode is BriefMode.ASK
    assert result.state.product_brief.shown == ("sofas",)
    assert result.state.product_brief.pending is not None
    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.PRODUCT_BRIEF
    assert route.primary.brief is not None and route.primary.brief.looking_for == "sofa"
    # The card is the turn's question: the reply asks none of its own.
    assert route.follow_up_allowed is False


async def test_a_need_stated_again_later_in_the_chat_is_asked_again() -> None:
    coordinator, pipeline = _coordinator(_search())
    shown = record_brief(AgentStateV1(), None, shown="sofas")

    result = await coordinator.run(_typed(shown))

    assert pipeline.requests == []
    assert result.product_brief is not None and result.product_brief.mode is BriefMode.ASK


async def test_results_asked_for_again_do_not_fold_the_card_beside_them_twice() -> None:
    coordinator, pipeline = _coordinator(_search(skip_questions=True))
    shown = record_brief(AgentStateV1(), None, shown="sofas")

    result = await coordinator.run(_typed(shown, "just show me sofas"))

    assert len(pipeline.requests) == 1
    assert result.product_brief is None


async def test_declining_the_questions_shows_results_with_the_card_folded_beside_them() -> None:
    coordinator, pipeline = _coordinator(_search(skip_questions=True))

    result = await coordinator.run(_typed(AgentStateV1(), "just show me sofas, no questions"))

    assert len(pipeline.requests) == 1
    assert result.product_brief is not None and result.product_brief.mode is BriefMode.NARROW
    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.SEARCH_RESULTS
    assert route.primary.brief is not None
    assert route.follow_up_allowed is False


async def test_tapped_answers_run_one_search_and_close_the_card() -> None:
    coordinator, pipeline = _coordinator(_search())
    asked = await coordinator.run(_typed(AgentStateV1()))
    pending = asked.state.product_brief.pending
    assert pending is not None

    answered = await coordinator.run(
        CustomerTurnInput(
            message="3-seater · Beige",
            state=asked.state,
            context=CONTEXT,
            search_action=BriefAnswerAction(
                card=pending.card, piece="sofa:3", colours=("Beige",), feel=pending.feels[1].key
            ),
        )
    )

    (ran,) = pipeline.requests
    assert ran.request.seating_capacity == SeatingCapacityConstraint.exactly(3)
    assert "Beige" in [p.canonical_value for p in ran.semantic_preferences]
    assert ran.semantic_text == "soft chenille fabric"
    assert answered.state.product_brief.pending is None
    # The feel keeps ranking when they page or refine.
    assert answered.state.active_search is not None
    assert answered.state.active_search.semantic_intent == "soft chenille fabric"
    assert answered.grounding.search is not None


async def test_answering_the_card_in_words_is_not_asked_another_question() -> None:
    """They typed instead of tapping: the card was the question, so the
    search it runs carries no follow-up of its own."""
    asking, _ = _coordinator(_search())
    asked = await asking.run(_typed(AgentStateV1()))
    # Even unflagged: the same family, now saying something the card asked.
    typed_answer = CustomerAgentDecision(
        action=AgentAction.SEARCH,
        commercial_reason=CommercialReason.CUSTOMER_REQUEST,
        search_request="a 3-seater sofa",
    )
    searching, pipeline = _coordinator(
        typed_answer, _need(seating_capacity=SeatingCapacityConstraint.exactly(3))
    )

    result = await searching.run(_typed(asked.state, "a 3-seater"))

    assert len(pipeline.requests) == 1
    assert result.product_brief is None
    assert result.state.product_brief.pending is None
    assert route_response(result).follow_up_allowed is False


async def test_just_show_me_under_the_card_searches_what_they_first_asked() -> None:
    asking, _ = _coordinator(_search())
    asked = await asking.run(_typed(AgentStateV1()))
    searching, pipeline = _coordinator(_search(skip_questions=True))

    result = await searching.run(_typed(asked.state, "just show me"))

    assert len(pipeline.requests) == 1
    assert result.product_brief is None
    assert result.state.product_brief.pending is None


async def test_the_need_said_again_under_its_card_is_asked_again() -> None:
    """Nothing the card asked is answered, so it is no answer: asked again."""
    asking, _ = _coordinator(_search())
    asked = await asking.run(_typed(AgentStateV1()))

    again = await asking.run(_typed(asked.state, "I need a sofa"))

    assert again.product_brief is not None and again.product_brief.mode is BriefMode.ASK


async def test_another_kind_asked_for_under_a_card_gets_its_own_card() -> None:
    asking, _ = _coordinator(_search())
    asked = await asking.run(_typed(AgentStateV1()))
    beds, pipeline = _coordinator(_search(), _need("bed", "bedroom"))

    result = await beds.run(_typed(asked.state, "actually, show me beds"))

    assert pipeline.requests == []
    assert result.state.product_brief.pending is not None
    assert result.state.product_brief.pending.name == "beds"


async def test_moving_on_to_another_category_is_asked_its_card() -> None:
    """ "Now show me coffee tables" after sofas: a new task, so a new card."""
    moving_on = CustomerAgentDecision(
        action=AgentAction.REFINE_SEARCH, taxonomy_change_requested=True
    )
    coordinator, pipeline = _coordinator(moving_on, _need("center-table", "tables"))
    sofas = AgentStateV1(
        active_search=ActiveSearchState(
            request=ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa"),
            revision=1,
        )
    )

    result = await coordinator.run(_typed(sofas, "now show me coffee tables"))

    assert pipeline.requests == []
    assert result.product_brief is not None and result.product_brief.mode is BriefMode.ASK
    assert result.state.product_brief.pending is not None
    assert result.state.product_brief.pending.name == "tables"


async def test_any_size_is_fine_said_with_the_need_holds_for_its_card() -> None:
    """ "Back to sofas, any size is fine": the card comes first, and the search
    its answers run does not bring back the width they gave earlier."""
    from tests.unit.test_sizes_per_product_type import _saved, _size

    with_a_saved_width = AgentStateV1(
        customer_preferences=CustomerPreferenceState(
            measurements_by_type=(_saved("sofa", _size("200")),)
        )
    )
    letting_go = CustomerAgentDecision(
        action=AgentAction.SEARCH,
        commercial_reason=CommercialReason.CUSTOMER_REQUEST,
        drop_saved_sizes=True,
    )
    asking, _ = _coordinator(letting_go)
    asked = await asking.run(_typed(with_a_saved_width, "back to sofas, any size is fine"))
    pending = asked.state.product_brief.pending
    assert pending is not None and pending.drop_saved_sizes

    skipping, pipeline = _coordinator(_search())
    await skipping.run(
        CustomerTurnInput(
            message="Show me sofas",
            state=asked.state,
            context=CONTEXT,
            search_action=BriefAnswerAction(card=pending.card),
        )
    )

    (ran,) = pipeline.requests
    assert ran.request.dimensions == ()


async def test_a_saved_size_still_applies_when_they_did_not_let_go_of_it() -> None:
    from tests.unit.test_sizes_per_product_type import _saved, _size

    with_a_saved_width = AgentStateV1(
        customer_preferences=CustomerPreferenceState(
            measurements_by_type=(_saved("sofa", _size("200")),)
        )
    )
    asking, _ = _coordinator(_search())
    asked = await asking.run(_typed(with_a_saved_width, "back to sofas"))
    pending = asked.state.product_brief.pending
    assert pending is not None and not pending.drop_saved_sizes

    skipping, pipeline = _coordinator(_search())
    await skipping.run(
        CustomerTurnInput(
            message="Show me sofas",
            state=asked.state,
            context=CONTEXT,
            search_action=BriefAnswerAction(card=pending.card),
        )
    )

    (ran,) = pipeline.requests
    assert [d.max_cm for d in ran.request.dimensions] == [Decimal("200")]


async def test_answers_to_a_card_no_longer_on_screen_search_nothing() -> None:
    coordinator, pipeline = _coordinator(_search())

    result = await coordinator.run(
        CustomerTurnInput(
            message="3-seater",
            state=AgentStateV1(),
            context=CONTEXT,
            search_action=BriefAnswerAction(card=1, piece="sofa:3"),
        )
    )

    assert pipeline.requests == []
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.QUESTIONS_EXPIRED


class SeatCeiling(FakeCapabilities):
    """A store whose largest single seating piece seats five."""

    async def overview(self, context: Any) -> CatalogOverview:
        return CatalogOverview(
            store_id=50,
            currency="SAR",
            shelves=(
                SubcategoryShelf(
                    commerce_category="seating",
                    commerce_subcategory="sofa",
                    active_count=10,
                    price_minimum=Decimal(990),
                    price_maximum=Decimal(4990),
                    seating=SeatingSpread(known_count=10, minimum=2, maximum=5),
                ),
            ),
        )


async def test_a_need_no_single_piece_seats_asks_the_shape_not_the_card() -> None:
    """ "A sofa for 9": which shape - separate sofas, or a sofa with armchairs -
    is the question that matters, so it comes first (CLAUDE.md 10.2, 27.1)."""
    need = _need(seating_capacity=SeatingCapacityConstraint.at_least(9))
    coordinator, pipeline = _coordinator(_search(), need, SeatCeiling())

    result = await coordinator.run(_typed(AgentStateV1(), "I need a sofa for 9 people"))

    assert result.product_brief is None
    assert len(pipeline.requests) == 1


async def test_a_need_one_piece_can_seat_still_gets_its_card() -> None:
    need = _need(seating_capacity=SeatingCapacityConstraint.exactly(4))
    coordinator, pipeline = _coordinator(_search(), need, SeatCeiling())

    result = await coordinator.run(_typed(AgentStateV1(), "I need a sofa for 4 people"))

    assert result.product_brief is not None
    assert pipeline.requests == []


class _Decor(FakeCapabilities):
    """Stocks candlesticks and vases, but no candles."""

    async def overview(self, context: Any) -> CatalogOverview:
        return CatalogOverview(
            store_id=50,
            currency="SAR",
            shelves=(
                SubcategoryShelf(
                    commerce_category="decor",
                    commerce_subcategory="candlestick",
                    active_count=3,
                    price_minimum=Decimal(50),
                    price_maximum=Decimal(300),
                ),
                SubcategoryShelf(
                    commerce_category="decor",
                    commerce_subcategory="vase",
                    active_count=40,
                    price_minimum=Decimal(80),
                    price_maximum=Decimal(900),
                ),
            ),
        )


class _ByType(Pipeline):
    """Finds products only for the named subcategories, empty for the rest."""

    def __init__(self, found: set[str]) -> None:
        super().__init__()
        self.found = found

    async def execute(self, resolved: Any, context: Any, **_: Any) -> ProductSearchExecutionResult:
        self.requests.append(resolved)
        ids = (31, 32, 33) if resolved.request.commerce_subcategory in self.found else ()
        return ProductSearchExecutionResult(
            presented_product_ids=ids,
            grounding=SearchExecutionGrounding(
                outcome=SearchOutcome.RESULTS if ids else SearchOutcome.ZERO_RESULTS,
                products=tuple(
                    to_grounded_product(
                        _sofa(pid), grounding_ref=n, presented_ordinal=n, relaxation_depth=0
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


class _PickCandlestick:
    async def closest(self, **_: Any) -> str:
        return "candlestick"


async def test_a_substitution_shows_no_narrowing_card_beside_it() -> None:
    """The store carries no candles, so candlesticks are offered as the closest
    type instead. That substitution is its own answer - the narrowing card must
    not fold beside it, or the reply and the chips would ask two different
    things (the bug from composing this recovery with the brief flow)."""
    coordinator, _ = _coordinator(
        _search(skip_questions=True),
        _need("candle", "decor"),
        _Decor(),
        pipeline=_ByType({"candlestick"}),
        closest_type=_PickCandlestick(),
    )

    result = await coordinator.run(_typed(AgentStateV1(), "do you have a candle"))

    assert result.unstocked_type == "candle"
    assert result.state.active_search.request.commerce_subcategory == "candlestick"
    # The substitution stands alone: no folded brief to contradict the reply.
    assert result.product_brief is None


async def test_a_head_count_on_a_card_on_screen_is_offered_to_the_room() -> None:
    """ "A sofa for 9" shows its card; designing the room next still asks "is
    it for the 9 you mentioned?" - the head count is on the card, not a search."""
    builder, _ = _builder()
    built = await builder.build(
        _need(seating_capacity=SeatingCapacityConstraint.at_least(9)),
        AgentStateV1(),
        CONTEXT,
        mode=BriefMode.ASK,
    )
    assert built is not None
    on_screen = record_brief(AgentStateV1(), built.pending, shown=built.pending.name)
    living_room = ROOMS.template("living_room")
    assert living_room is not None

    assert _earlier_seat_count(on_screen, living_room) == 9


# ── the best match ══════════════════════════════════════════════════════════


async def test_the_first_card_is_the_best_match_only_when_their_words_ordered_them() -> None:
    coordinator, _ = _coordinator(_search(skip_questions=True))
    result = await coordinator.run(_typed(AgentStateV1(), "just show me sofas"))
    assert best_match_first(result) is True

    by_price = result.model_copy(
        update={
            "state": result.state.model_copy(
                update={
                    "active_search": result.state.active_search.model_copy(  # type: ignore[union-attr]
                        update={
                            "request": SOFAS_BY_PRICE,
                        }
                    )
                }
            )
        }
    )
    assert best_match_first(by_price) is False

    beside_a_pick = result.model_copy(update={"focus": result.grounding.search.products[0]})  # type: ignore[union-attr]
    assert best_match_first(beside_a_pick) is False


SOFAS_BY_PRICE = ProductSearchRequest(
    commerce_category="seating", commerce_subcategory="sofa", sort=ProductSort.PRICE_ASC
)


# ── ticks on earlier lists, and what a first pick sets off ══════════════════


class KindHydration:
    """Products whose kind depends on the id: 1xx sofas, 2xx sectionals,
    3xx centre tables."""

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[ProductCandidate, ...]:
        kinds = {
            1: ("seating", "sofa"),
            2: ("seating", "sectional-sofa"),
            3: ("tables", "center-table"),
        }
        return tuple(_sofa(pid, kinds[pid // 100][1], kinds[pid // 100][0]) for pid in product_ids)


def _picks_runtime(
    sessions: FakeSessionStore, *, stock: tuple[tuple[str, str | None], ...]
) -> PicksRuntime:
    ids = (101, 102, 201, 301, 302)
    repository = FakeRepository([_row(pid) for pid in ids])
    return PicksRuntime(
        ProductReferenceResolver(cast(ProductRepository, repository), ATTRIBUTES),
        KindHydration(),  # type: ignore[arg-type]
        sessions,  # type: ignore[arg-type]
        CustomerAgentSettings(),
        complements=COMPLEMENTS,
        capabilities=FakeCapabilities(pairs=stock),  # type: ignore[arg-type]
    )


def _two_lists(picks: tuple[int, ...] = ()) -> AgentStateV1:
    """Sofas shown at revision 1, then centre tables at revision 2."""
    return AgentStateV1(
        active_search=ActiveSearchState(
            request=ProductSearchRequest(commerce_category="tables"), revision=2
        ),
        product_interaction=ProductInteractionState(
            presented_product_ids=(301, 302),
            presented_search_revision=2,
            earlier_lists=(PresentedList(revision=1, product_ids=(101, 102, 201)),),
            selected_product_ids=picks,
        ),
    )


def _tick(ordinal: int, list_revision: int | None = None) -> PicksRequest:
    return PicksRequest(
        session_id=SESSION,
        store_id=STORE,
        action=SelectPickAction(ordinal=ordinal, list_revision=list_revision),
    )


async def test_a_card_on_an_earlier_list_can_still_be_picked() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _two_lists())

    reply = await _picks_runtime(sessions, stock=()).apply(_tick(2, list_revision=1), CONTEXT)

    assert sessions.saved[(STORE, SESSION)].state.product_interaction.selected_product_ids == (102,)
    (pick,) = reply.picks
    assert [(p.list_revision, p.ordinal) for p in pick.positions] == [(1, 2)]
    assert pick.presented_ordinal is None


async def test_a_list_no_longer_remembered_cannot_be_ticked() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _two_lists())

    with pytest.raises(PickUnavailableError):
        await _picks_runtime(sessions, stock=()).apply(_tick(1, list_revision=9), CONTEXT)


async def test_the_first_pick_of_its_kind_asks_for_what_goes_with_it() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _two_lists())

    reply = await _picks_runtime(sessions, stock=(("tables", "center-table"),)).apply(
        _tick(1, list_revision=1), CONTEXT
    )

    assert reply.goes_with == 1


async def test_a_second_option_of_the_same_kind_is_offered_what_goes_with_it() -> None:
    """A sectional after a sofa is still a moment to cross-sell, never a prompt
    to compare the two."""
    sessions = FakeSessionStore()
    await _stored(sessions, _two_lists(picks=(101,)))

    reply = await _picks_runtime(sessions, stock=(("tables", "center-table"),)).apply(
        _tick(3, list_revision=1), CONTEXT
    )

    assert reply.goes_with == 2
    assert len(reply.picks) == 2


async def test_nothing_the_store_sells_goes_with_it_so_the_tick_is_silent() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _two_lists())

    reply = await _picks_runtime(sessions, stock=(("lighting", "chandelier"),)).apply(
        _tick(1, list_revision=1), CONTEXT
    )

    assert reply.goes_with is None


async def test_a_companion_type_already_picked_is_not_a_reason_to_cross_sell() -> None:
    """A centre table picked after the sofa: its companions are rugs, side
    tables and sofas - the store only sells the sofa they already have."""
    sessions = FakeSessionStore()
    await _stored(sessions, _two_lists(picks=(101,)))

    reply = await _picks_runtime(sessions, stock=(("seating", "sofa"),)).apply(_tick(1), CONTEXT)

    assert reply.goes_with is None


def test_positions_cover_every_list_still_tickable() -> None:
    state = _two_lists(picks=(102,))

    (pick,) = build_picks(state, [_sofa(102)])

    assert [(p.list_revision, p.ordinal) for p in pick.positions] == [(1, 2)]
