"""Each sub-category's own questions, asked on one card before its products.

"I need a recliner" gets that sub-category's questions from the reviewed sheet
(`subcategory_questions_v1.yaml`), all at once: the sheet decides what is
asked, the catalog the chips wherever it has the data, and what the catalog
cannot know - a material, a feature, a shape - only ranks (CLAUDE.md 10.4).
The catalog is faked; the registries and the builder are the real ones.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from app.core.config import CustomerAgentSettings
from app.core.exceptions import TaxonomyConfigurationError
from app.repositories.products import BriefFacts, ProductRepository
from app.schemas.agent_state import AgentStateV1
from app.schemas.language import ReplyLanguage
from app.schemas.product_brief import BriefMode
from app.schemas.search_action import BriefAnswerAction
from app.services.product_brief import ProductBriefBuilder
from app.taxonomy.briefs import BriefQuestionKind
from app.taxonomy.name_match import NameMatch
from app.taxonomy.name_match import matches as name_matches
from app.taxonomy.subcategory_questions import (
    MAX_SHEET_CHOICES,
    SheetAsk,
    load_subcategory_questions,
)

from tests.unit.test_product_brief import (
    ATTRIBUTES,
    BRIEFS,
    SOFA_FACTS,
    TAXONOMY,
    FakeFacts,
    _need,
)
from tests.unit.test_turn_coordinator import CONTEXT

SHEET = load_subcategory_questions(taxonomy=TAXONOMY)

RECLINER_FACTS = BriefFacts(
    kinds=(("recliner", None, 40),),
    currency="SAR",
    price_quartiles=(Decimal("1900"), Decimal("2800"), Decimal("4100")),
    colors=(("Brown", 20), ("Grey", 12)),
    styles=(("Modern", 30),),
)


def _stocked(*subcategories: str) -> BriefFacts:
    """A store with a dozen of each of these types, none seat-counted."""
    return BriefFacts(
        kinds=tuple((subcategory, None, 12) for subcategory in subcategories),
        currency=RECLINER_FACTS.currency,
        price_quartiles=RECLINER_FACTS.price_quartiles,
        colors=RECLINER_FACTS.colors,
        styles=RECLINER_FACTS.styles,
    )


def _builder(
    facts: BriefFacts = SOFA_FACTS, *, sheet: Any = SHEET
) -> tuple[ProductBriefBuilder, FakeFacts]:
    repository = FakeFacts(facts)
    builder = ProductBriefBuilder(
        cast(ProductRepository, repository), BRIEFS, ATTRIBUTES, TAXONOMY, sheet=sheet
    )
    return builder, repository


async def _card(builder: ProductBriefBuilder, resolved: Any = None, **options: Any) -> Any:
    return await builder.build(
        resolved or _need(), AgentStateV1(), CONTEXT, mode=BriefMode.ASK, **options
    )


def _labels(built: Any, index: int) -> list[str]:
    return [choice.label for choice in built.card.questions[index].choices]


# ── the reviewed sheet ══════════════════════════════════════════════════════


def test_every_sub_category_but_lounge_chair_has_its_questions() -> None:
    """Lounge chair keeps today's questions until its own are reviewed."""
    every = {sub for category in TAXONOMY.categories for sub in TAXONOMY.subcategories(category)}
    covered = {sub for row in SHEET.rows for sub in row.subcategories}

    assert every - covered == {"lounge-chair"}
    assert SHEET.for_search("tables", None) is not None
    assert SHEET.for_search("lighting", None) is not None


def test_no_answer_that_adds_nothing_is_offered() -> None:
    nothing = {"not sure", "no preference", "all colors", "all options", "my budget is flexible"}
    for row in SHEET.rows:
        for question in row.questions:
            labels = {option.label.casefold() for option in question.options}
            assert not labels & nothing, row.name
            assert not any(label.startswith("other ") for label in labels), row.name


def test_the_catalog_answers_colour_and_budget_and_brand_is_never_asked() -> None:
    for row in SHEET.rows:
        for question in row.questions:
            if question.ask is SheetAsk.BUDGET:
                assert question.options == ()
            if question.ask is SheetAsk.COLOUR and question.options:
                # Only the sheet's own words beside the catalog's colours.
                assert all(option.names is not None for option in question.options)
            assert "brand" not in question.text.casefold()


def test_a_card_asks_the_sheets_questions_as_the_sheet_asks_them() -> None:
    """At most the sheet's three; "which color or pattern" stays one question."""
    rug = SHEET.for_search("decor", "carpet")
    tables = SHEET.for_search("tables", None)
    sofa = SHEET.for_search("seating", "sofa")
    assert rug is not None and tables is not None and sofa is not None

    assert all(len(row.questions) <= 3 for row in SHEET.rows)
    assert [q.text for q in rug.questions] == [
        "What rug shape and size do you need?",
        "Which pile type or material do you prefer?",
        "Which color or pattern do you prefer?",
    ]
    colour = rug.questions[2]
    assert colour.ask is SheetAsk.COLOUR and colour.arabic == "أي لون أو نقشة تفضّل؟"
    assert [o.label for o in colour.options][:3] == ["Multicolor", "Solid", "Floral"]
    assert tables.questions[2].text == "Which material or color do you prefer?"
    assert tables.questions[2].options_first
    assert [q.ask for q in sofa.questions] == [SheetAsk.TYPE, SheetAsk.COLOUR, SheetAsk.DETAIL]


def test_the_sheet_fixes_are_kept() -> None:
    pendant = SHEET.for_search("lighting", "pendant-lighting")
    outdoor = SHEET.for_search("lighting", "outdoor-lighting")
    chandelier = SHEET.for_search("lighting", "chandelier")
    plate = SHEET.for_search("kitchenware", "plate")
    assert pendant is not None and outdoor is not None
    assert chandelier is not None and plate is not None

    assert pendant.questions[0].options[0].label == "Single pendant"
    assert outdoor.questions[0].options[0].label == "Wall sconce"
    assert chandelier.questions[0].text == "Which chandelier shape do you prefer?"
    assert plate.questions[1].text == "Which material do you prefer?"


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("rows: {sofa: {for: [couch], questions: []}}", "approved subcategory"),
        (
            "rows: {sofa: {for: [sofa], questions: [{ask: budget, q: {en: C, ar: ل},"
            " options: [{en: A, ar: د}, {en: B, ar: ب}]}]}}",
            "catalog's own",
        ),
        (
            "rows: {sofa: {for: [sofa], questions: [{ask: detail, q: {en: C, ar: ل},"
            " options_first: true, options: [{en: Velvet, ar: د}, {en: Linen, ar: ب}]}]}}",
            "only a colour question",
        ),
        (
            "rows: {sofa: {for: [sofa], questions: [{ask: colour, q: {en: C, ar: ل},"
            " options: [{en: Striped, ar: د}]}]}}",
            "at least two options",
        ),
        (
            "rows: {sofa: {for: [sofa], questions: [{ask: shade, q: {en: C, ar: ل}}]}}",
            "unknown 'ask'",
        ),
        (
            "rows: {t: {for: [], for_category: [tables], questions:"
            " [{ask: budget, q: {en: B, ar: م}}]}}",
            "must ask the type",
        ),
        (
            "rows: {a: {for: [sofa], questions: [{ask: budget, q: {en: B, ar: م}}]},"
            " b: {for: [sofa], questions: [{ask: budget, q: {en: B, ar: م}}]}}",
            "already has a row",
        ),
        (
            "rows: {sofa: {for: [sofa], questions: [{ask: type, q: {en: T, ar: ن},"
            " options: [{en: A, ar: د, seats: 2}, {en: B, ar: ب}]}]}}",
            "needs a subcategory",
        ),
        (
            "rows: {sofa: {for: [sofa], questions: [{ask: budget, multi: true,"
            " q: {en: B, ar: م}}]}}",
            "only a detail",
        ),
        (
            "rows: {sofa: {for: [sofa], questions: [{ask: detail, q: {en: D, ar: ت},"
            " options: [{en: A}, {en: B, ar: ب}]}]}}",
            "'ar' must be",
        ),
        (
            # A type it switches to is offered by its stock, never by names.
            "rows: {sofa: {for: [sofa], questions: [{ask: type, q: {en: T, ar: ن},"
            " options: [{en: Sets, ar: د, subcategory: sofa-set, match: [set]},"
            " {en: Curved, ar: ب}]}]}}",
            "unknown field match",
        ),
        (
            "rows: {sofa: {for: [sofa], questions: [{ask: detail, q: {en: D, ar: ت},"
            " options: [{en: Sofa, ar: د}, {en: Velvet, ar: ب}]}]}}",
            "give it 'match'",
        ),
        (
            "rows: {sofa: {for: [sofa], questions: [{ask: detail, q: {en: D, ar: ت},"
            " options: [{en: Velvet, ar: د, match: ['velv.*']}, {en: Linen, ar: ب}]}]}}",
            "'match'",
        ),
    ],
)
def test_a_malformed_sheet_fails_startup(tmp_path: Path, body: str, problem: str) -> None:
    source = tmp_path / "questions.yaml"
    source.write_text(f"version: v1\n{body}\n", encoding="utf-8")

    with pytest.raises(TaxonomyConfigurationError, match=problem):
        load_subcategory_questions(source, taxonomy=TAXONOMY)


def test_the_questions_are_on_by_default() -> None:
    assert CustomerAgentSettings().subcategory_questions is True


def test_chips_are_checked_against_names_by_default() -> None:
    settings = CustomerAgentSettings()

    assert settings.subcategory_chips_from_names is True
    assert settings.subcategory_chip_min_products == 1


def test_every_chip_that_ranks_knows_what_names_to_look_for() -> None:
    """A type it switches to is counted by stock and a head count by seats;
    every other chip has the words its names must carry."""
    for row in SHEET.rows:
        for question in row.questions:
            for option in question.options:
                ranks = option.subcategory is None and question.ask is not SheetAsk.PEOPLE
                assert (option.names is not None) is ranks, (row.name, option.label)


def test_reviewed_words_find_what_the_store_names_it() -> None:
    bed = SHEET.for_search("bedroom", "bed")
    rug = SHEET.for_search("decor", "carpet")
    assert bed is not None and rug is not None
    lift = next(o for o in bed.questions[1].options if o.label.startswith("Bed with lift-up"))
    flat = next(o for o in rug.questions[1].options if o.label == "Flatweave (no pile)")
    assert lift.names is not None and flat.names is not None

    assert name_matches(lift.names, "Queen Fabric Storage Bed with Gas-Lift Base", None)
    assert name_matches(flat.names, "Handwoven Cotton Dhurry Rug", None)
    assert not name_matches(lift.names, "Queen Upholstered Bed with Panel Headboard", None)


# ── the card ════════════════════════════════════════════════════════════════


async def test_a_sofa_is_asked_its_own_questions_all_at_once() -> None:
    builder, repository = _builder()

    built = await _card(builder, opening=True)

    assert built is not None
    assert built.pending.sheet and not built.pending.opening
    # The sheet's three, as it asks them: its colours and patterns are one question.
    assert [q.label for q in built.card.questions] == [
        "Which sofa type or shape do you prefer?",
        "Which color do you prefer?",
        "Which material or upholstery type do you prefer?",
    ]
    assert [q.kind for q in built.card.questions] == [
        BriefQuestionKind.TYPE,
        BriefQuestionKind.COLOUR,
        BriefQuestionKind.FEEL,
    ]
    assert [q.id for q in built.card.questions] == ["q1", "q2", "q3"]
    assert built.card.submit_label == "Show me sofas"
    # The kinds offered are counted together, as on the family card.
    assert repository.calls[-1] == ("sofa", "sectional-sofa", "single-seater-sofa", "sofa-set")


async def test_kinds_the_store_stocks_come_first_then_the_sheets_in_its_order() -> None:
    builder, _ = _builder()

    built = await _card(builder)

    labels = _labels(built, 0)
    assert len(labels) == MAX_SHEET_CHOICES
    # Most stocked first; no single-seater sofas in stock, so none offered.
    assert labels[:4] == [
        "3-seater sofa",
        "2-seater sofa",
        "Large 4\u20136 seater sofa",
        "Separate sofa set",
    ]
    assert "Single-seat sofa" not in labels
    assert labels[-2:] == ["Straight sofa", "Curved / Round sofa"]


async def test_colours_are_the_catalogs_and_details_the_sheets() -> None:
    builder, _ = _builder()

    built = await _card(builder)

    colour = built.card.questions[1]
    assert _labels(built, 1) == [
        "Beige",
        "Light Grey",
        "Grey",
        "Multicolor",
        "Solid",
        "Striped",
        "Floral",
        "Geometric",
    ]
    # The catalog's colours are colours; the sheet's patterns beside them, feels.
    assert [c.kind for c in colour.choices[:3]] == [None, None, None]
    assert {c.kind for c in colour.choices[3:]} == {BriefQuestionKind.FEEL}
    assert colour.max_choices == 3
    assert _labels(built, 2)[:3] == ["Linen", "Cotton", "Velvet"]
    assert len(_labels(built, 2)) == MAX_SHEET_CHOICES
    assert built.card.questions[2].max_choices == 3


async def test_a_single_answer_question_takes_one_tick() -> None:
    builder, _ = _builder(RECLINER_FACTS)

    built = await _card(builder, _need("recliner"))

    first, features, budget = built.card.questions
    assert first.max_choices == 1 and features.max_choices == 3
    assert budget.kind is BriefQuestionKind.BUDGET
    assert budget.choices[0].label == "Under 1,900 SAR"


async def test_what_they_already_said_is_left_off() -> None:
    builder, _ = _builder()

    built = await _card(
        builder,
        _need(semantic_text="a velvet sofa", colors_any_of=("Grey",)),
    )

    # Grey answers "which color" and velvet the material: only the kind is left.
    assert [q.label for q in built.card.questions] == [
        "Which sofa type or shape do you prefer?",
    ]


async def test_a_card_with_nothing_left_to_ask_is_skipped() -> None:
    builder, _ = _builder(RECLINER_FACTS)
    price = {"currency": "SAR", "max_amount": Decimal("3000")}

    built = await _card(
        builder,
        _need("recliner", semantic_text="swivel recliner with massage", price=price),
    )

    assert built is None


async def test_a_need_that_names_no_kind_asks_the_kind_on_the_rows_card() -> None:
    builder, repository = _builder(_stocked("center-table", "dining-table"))

    built = await _card(builder, _need(cast(str, None), "tables"))

    assert built.card.noun == "tables"
    assert built.card.questions[0].kind is BriefQuestionKind.TYPE
    assert "dining-table" in repository.calls[-1] and "office-table" in repository.calls[-1]


async def test_a_head_count_ranks_where_the_store_records_no_seats() -> None:
    builder, _ = _builder(_stocked("dining-table"))

    built = await _card(builder, _need("dining-table", "dining"))

    people = built.card.questions[0]
    assert people.kind is BriefQuestionKind.FEEL
    assert [c.label for c in people.choices][:2] == ["2 people", "4 people"]
    assert built.pending.feels[1].words == "4 seater"


async def test_a_head_count_orders_by_seats_where_they_are_reviewed() -> None:
    builder, _ = _builder()

    built = await _card(builder, _need("sofa-set"))

    people = built.card.questions[0]
    assert people.kind is BriefQuestionKind.PEOPLE
    options = {p.people: p for p in built.pending.people}
    # The store's sets seat six at most: nine is a combination.
    assert not options[6].combine and options[9].combine and options[9].or_more


async def test_mattresses_are_asked_their_size_and_firmness() -> None:
    builder, _ = _builder(_stocked("mattresses"))

    built = await _card(builder, _need("mattresses", "bedding"), opening=True)

    assert [q.label for q in built.card.questions][:2] == [
        "What mattress size do you need?",
        "Which firmness level do you prefer?",
    ]


async def test_the_card_reads_in_the_sheets_arabic() -> None:
    builder, _ = _builder()

    built = await _card(builder, language=ReplyLanguage.AR)

    assert built.card.questions[0].label == "أي نوع أو شكل كنب تفضّل؟"
    assert "كنب 3 مقاعد" in _labels(built, 0)
    assert _labels(built, 1)[0] == "بيج"
    assert built.card.submit_label.endswith("الكنب")


async def test_a_type_the_store_does_not_stock_is_searched_not_asked() -> None:
    """No answer would lead to a product: the search says it is not stocked."""
    builder, _ = _builder(SOFA_FACTS)

    assert await _card(builder, _need("recliner")) is None


async def test_switched_off_the_family_card_is_asked_as_before() -> None:
    builder, _ = _builder(sheet=None)

    built = await _card(builder, opening=True)

    assert built is not None and not built.pending.sheet
    assert all(question.id is None for question in built.card.questions)


async def test_lounge_chair_keeps_its_family_questions() -> None:
    builder, _ = _builder()

    built = await _card(builder, _need("lounge-chair"), opening=True)

    assert built is not None and not built.pending.sheet


# ── the answers ═════════════════════════════════════════════════════════════


async def _sofa_pending() -> Any:
    builder, _ = _builder()
    built = await _card(builder)
    return builder, built.pending


async def test_a_kind_that_names_a_type_switches_to_it() -> None:
    builder, pending = await _sofa_pending()
    three = next(k for k in pending.kinds if k.seats == 3)

    answer = builder.answer(BriefAnswerAction(card=pending.card, piece=three.key), pending)

    assert answer is not None
    request = answer.search.request
    assert request.commerce_subcategory == "sofa" and request.single_type
    assert request.seating_capacity is not None and request.seating_capacity.min_capacity == 3


async def test_a_kind_that_names_no_type_only_ranks() -> None:
    builder, pending = await _sofa_pending()
    straight = next(k for k in pending.kinds if k.words == "Straight sofa")

    answer = builder.answer(BriefAnswerAction(card=pending.card, piece=straight.key), pending)

    assert answer is not None
    assert answer.search.request.commerce_subcategory == "sofa"
    assert answer.search.request.seating_capacity is None
    assert answer.search.semantic_text == "Straight sofa"


async def test_every_feel_ticked_ranks_and_none_filters() -> None:
    builder, pending = await _sofa_pending()
    keys = (pending.feels[0].key, pending.feels[-1].key)

    answer = builder.answer(
        BriefAnswerAction(card=pending.card, colours=("Beige",), feels=keys), pending
    )

    assert answer is not None
    search = answer.search
    assert search.semantic_text == f"{pending.feels[0].words} {pending.feels[-1].words}"
    assert search.request.colors_any_of == ()
    assert [p.canonical_value for p in search.semantic_preferences] == ["Beige"]


async def test_a_feel_the_card_never_offered_searches_nothing() -> None:
    builder, pending = await _sofa_pending()

    answer = builder.answer(BriefAnswerAction(card=pending.card, feels=("q9-1",)), pending)

    assert answer is None


async def test_a_typed_answer_to_the_card_is_recognised() -> None:
    builder, pending = await _sofa_pending()
    state = AgentStateV1().model_copy(
        update={
            "product_brief": AgentStateV1().product_brief.model_copy(
                update={"pending": pending, "cards": pending.card}
            )
        }
    )

    assert builder.answers_card(_need(semantic_text="velvet"), state)
    assert not builder.answers_card(_need(), state)


# ── Narrow down ═════════════════════════════════════════════════════════════


async def test_narrow_down_asks_the_same_questions_and_the_budget() -> None:
    builder, _ = _builder()

    built = await builder.build(
        _need(semantic_text="velvet", seating_capacity={"min_capacity": 3, "max_capacity": 3}),
        AgentStateV1(),
        CONTEXT,
        mode=BriefMode.NARROW,
        prefilled=True,
    )

    assert built is not None and built.pending.narrowing
    questions = built.card.questions
    assert [q.kind for q in questions][-1] is BriefQuestionKind.BUDGET
    three = next(k.key for k in built.pending.kinds if k.seats == 3)
    assert questions[0].selected == (three,)
    velvet = next(f.key for f in built.pending.feels if f.words == "Velvet")
    assert velvet in questions[2].selected


# ── chips the store's names back ════════════════════════════════════════════

BED_FACTS = BriefFacts(
    kinds=(("bed", None, 6),),
    currency="SAR",
    price_quartiles=(Decimal("950"), Decimal("1100"), Decimal("1400")),
    colors=(("Grey", 3), ("Beige", 3)),
    styles=(("Modern", 6),),
)

BED_NAMES = (
    "Queen Upholstered Bed with Panel Headboard - Warm Grey",
    "King Upholstered Bed with Curved Headboard - Light Beige",
    "Single Fabric Storage Bed with Gas-Lift Base - Sage Green",
    "Queen Fabric Storage Bed with Gas-Lift Base - Sage Green",
    "Single Upholstered Wingback Bed with Reading Light - Warm Beige",
    "Queen Upholstered Bed with Wingback Headboard - Warm Beige",
)


class FakeNamed(FakeFacts):
    """The catalog's counts, and the names a chip's words are looked for in -
    with the real patterns, so what is offered is what the names say."""

    def __init__(self, facts: BriefFacts, names: tuple[str, ...]) -> None:
        super().__init__(facts)
        self.names = names
        self.named_calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    async def name_match_counts(
        self, subcategories: Any, matches: dict[str, NameMatch], context: Any
    ) -> dict[str, int]:
        self.named_calls.append((tuple(subcategories), tuple(matches)))
        return {
            key: sum(1 for name in self.names if name_matches(match, name, None))
            for key, match in matches.items()
        }


def _named(
    facts: BriefFacts = BED_FACTS, names: tuple[str, ...] = BED_NAMES, *, minimum: int | None = 1
) -> tuple[ProductBriefBuilder, FakeNamed]:
    repository = FakeNamed(facts, names)
    builder = ProductBriefBuilder(
        cast(ProductRepository, repository),
        BRIEFS,
        ATTRIBUTES,
        TAXONOMY,
        sheet=SHEET,
        named_chip_min=minimum,
    )
    return builder, repository


def _bed() -> Any:
    return _need("bed", "bedroom")


async def test_a_chip_is_offered_only_where_the_stores_names_back_it() -> None:
    builder, _ = _named()

    built = await _card(builder, _bed())

    assert built is not None
    # Most-named first; no bed is named Double, so it is not offered.
    assert _labels(built, 0) == ["Queen 160\u00d7200", "Single 90\u00d7200", "King 180\u00d7200"]
    assert _labels(built, 1) == [
        "Bed with upholstered headboard",
        "Standard bed with headboard",
        "Bed with lift-up storage (ottoman)",
        "Bed with built-in lighting",
    ]
    assert built.card.questions[2].kind is BriefQuestionKind.BUDGET


async def test_a_chip_keeps_its_place_in_the_sheet_as_its_key() -> None:
    builder, _ = _named()

    built = await _card(builder, _bed())

    keys = [choice.key for choice in built.card.questions[1].choices]
    assert keys == ["q2-4", "q2-1", "q2-7", "q2-8"]
    assert {feel.key for feel in built.pending.feels} >= set(keys)


async def test_a_chip_the_names_do_not_back_searches_nothing() -> None:
    builder, _ = _named()
    built = await _card(builder, _bed())
    pending = built.pending

    drawers = builder.answer(BriefAnswerAction(card=pending.card, feels=("q2-6",)), pending)
    lift = builder.answer(BriefAnswerAction(card=pending.card, feels=("q2-7",)), pending)

    assert drawers is None
    assert lift is not None
    assert lift.search.semantic_text == "Bed with lift-up storage (ottoman)"
    assert lift.search.request.commerce_subcategory == "bed"


async def test_a_question_its_names_back_too_little_is_asked_in_the_sheets_words() -> None:
    """Every sheet question is asked: one chip is no choice, so the backed one
    comes first and the sheet's others follow, in its order."""
    builder, _ = _named(names=("King Upholstered Bed with Curved Headboard",))

    built = await _card(builder, _bed())

    assert built is not None
    assert [q.label for q in built.card.questions] == [
        "What bed size do you need?",
        "Which type of bed do you prefer?",
        "What's your approximate budget?",
    ]
    assert _labels(built, 0) == [
        "King 180\u00d7200",
        "Single 90\u00d7200",
        "Double 140\u00d7200",
        "Queen 160\u00d7200",
    ]
    # Two backed are a choice: only they are offered, the sheet's order between equals.
    assert _labels(built, 1) == ["Standard bed with headboard", "Bed with upholstered headboard"]


async def test_every_sheet_question_is_asked_though_its_names_back_none() -> None:
    mattresses = BriefFacts(
        kinds=(("mattresses", None, 7),),
        currency=None,
        price_quartiles=None,
        colors=(),
        styles=(),
    )
    builder, _ = _named(mattresses, ("Premier Silver Rest Bonnell Spring Mattress",))

    built = await _card(builder, _need("mattresses", "bedroom"))

    assert built is not None
    assert [q.label for q in built.card.questions] == [
        "What mattress size do you need?",
        "Which firmness level do you prefer?",
    ]
    assert _labels(built, 0) == ["Single", "Double", "Queen", "King", "Custom size"]
    assert _labels(built, 1) == ["Soft", "Medium", "Firm"]


async def test_a_type_it_switches_to_is_counted_by_stock_and_the_rest_by_names() -> None:
    builder, repository = _named(SOFA_FACTS, ("Curved Boucle Sofa", "3 Seater Chenille Sofa"))

    built = await _card(builder)

    assert built is not None
    kinds = _labels(built, 0)
    assert kinds[:4] == [
        "3-seater sofa",
        "2-seater sofa",
        "Large 4\u20136 seater sofa",
        "Separate sofa set",
    ]
    # Beside the kinds it switches to, the question is a choice: only the
    # ranking kinds the names back join them.
    assert "Curved / Round sofa" in kinds and "Straight sofa" not in kinds
    assert [q.label for q in built.card.questions] == [
        "Which sofa type or shape do you prefer?",
        "Which color do you prefer?",
        "Which material or upholstery type do you prefer?",
    ]
    # No pattern is named: beside the store's colours, none is offered.
    assert _labels(built, 1) == ["Beige", "Light Grey", "Grey"]
    assert sorted(_labels(built, 2)) == ["Bouclé", "Chenille"]
    # One read for every chip on the card, across the kinds it offers.
    [(types, keys)] = repository.named_calls
    assert types == ("sofa", "sectional-sofa", "single-seater-sofa", "sofa-set")
    assert "q1-1" in keys and not any(key == "q1-2" for key in keys)


async def test_a_higher_minimum_asks_more_of_the_names() -> None:
    builder, _ = _named(minimum=3)

    built = await _card(builder, _bed())

    assert built is not None
    # Only Queen is named on three beds: asked in the sheet's words, Queen first.
    assert _labels(built, 0) == [
        "Queen 160\u00d7200",
        "Single 90\u00d7200",
        "Double 140\u00d7200",
        "King 180\u00d7200",
    ]
    assert _labels(built, 1) == ["Bed with upholstered headboard", "Standard bed with headboard"]


async def test_switched_off_every_chip_is_offered_in_the_sheets_order() -> None:
    builder, repository = _named(minimum=None)

    built = await _card(builder, _bed())

    assert built is not None
    assert _labels(built, 0)[:2] == ["Single 90\u00d7200", "Double 140\u00d7200"]
    assert len(_labels(built, 1)) == MAX_SHEET_CHOICES
    assert repository.named_calls == []


# ── "which color or pattern": one question ══════════════════════════════════

RUG_FACTS = BriefFacts(
    kinds=(("carpet", None, 4),),
    currency="SAR",
    price_quartiles=(Decimal("400"), Decimal("700"), Decimal("1100")),
    colors=(("Beige", 2), ("Grey", 1), ("Navy", 1)),
    styles=(("Modern", 4),),
)

RUG_NAMES = (
    "Abstract Handwoven Wool Rug",
    "Striped Cotton Dhurry Rug",
    "Abstract Hand Tufted Rug",
    "Plain Jute Rug",
)


def _rug(**search: Any) -> Any:
    return _need("carpet", "decor", **search)


async def test_the_sheets_colour_or_pattern_question_asks_both_at_once() -> None:
    builder, _ = _named(RUG_FACTS, RUG_NAMES)

    built = await _card(builder, _rug())

    assert built is not None
    question = built.card.questions[2]
    assert question.label == "Which color or pattern do you prefer?"
    assert question.kind is BriefQuestionKind.COLOUR and question.max_choices == 3
    # The store's colours, then the patterns its rugs are named with.
    assert _labels(built, 2) == ["Beige", "Grey", "Navy", "Abstract", "Striped"]
    assert [c.kind for c in question.choices] == [None] * 3 + [BriefQuestionKind.FEEL] * 2


async def test_a_colour_and_a_pattern_ticked_together_are_each_read_as_their_own() -> None:
    builder, _ = _named(RUG_FACTS, RUG_NAMES)
    built = await _card(builder, _rug())
    pending = built.pending
    striped = next(c.key for c in built.card.questions[2].choices if c.label == "Striped")

    answer = builder.answer(
        BriefAnswerAction(card=pending.card, colours=("Grey",), feels=(striped,)), pending
    )

    assert answer is not None
    assert answer.search.semantic_text == "Striped"
    assert answer.search.request.colors_any_of == ()
    assert [p.canonical_value for p in answer.search.semantic_preferences] == ["Grey"]


async def test_a_pattern_they_named_answers_the_colour_or_pattern_question() -> None:
    builder, _ = _named(RUG_FACTS, RUG_NAMES)

    built = await _card(builder, _rug(semantic_text="a striped rug"))

    assert built is not None
    assert "Which color or pattern do you prefer?" not in [q.label for q in built.card.questions]


async def test_narrow_down_opens_a_pattern_in_use_ticked() -> None:
    builder, _ = _named(RUG_FACTS, RUG_NAMES)

    built = await builder.build(
        _rug(semantic_text="Abstract"),
        AgentStateV1(),
        CONTEXT,
        mode=BriefMode.NARROW,
        prefilled=True,
    )

    assert built is not None
    question = next(q for q in built.card.questions if q.kind is BriefQuestionKind.COLOUR)
    abstract = next(c.key for c in question.choices if c.label == "Abstract")
    assert question.selected == (abstract,)


async def test_where_the_sheet_lists_materials_first_they_come_first() -> None:
    builder, _ = _named(
        _stocked("center-table", "service-table"),
        ("Marble Coffee Table", "Glass Side Table", "Marble Look Side Table"),
    )

    built = await _card(builder, _need(cast(str, None), "tables"))

    assert built is not None
    question = built.card.questions[2]
    assert question.label == "Which material or color do you prefer?"
    # Materials first, as the sheet lists them, then the approved colours.
    assert _labels(built, 2) == ["Marble", "Glass", "Grey"]
