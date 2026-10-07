"""Chips, the question card and the room piece picker in the session's language.

The rule they all follow (docs/arabic-replies-plan.md): which chips are offered,
the keys a tap sends back and the action a chip performs never depend on the
language - only the words do. English is exactly what it was.
"""

from __future__ import annotations

import re
import shutil
import string
from decimal import Decimal
from pathlib import Path

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.prompts.customer_commerce import v1 as decision_prompt
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CustomerAgentDecision,
    FollowUpPolicy,
)
from app.schemas.agent_state import AgentStateV1, SwapBudgetOfferStage
from app.schemas.agent_turn import (
    CustomerTurnInput,
    CustomerTurnResult,
    SwapBudgetOffer,
    TurnGrounding,
)
from app.schemas.language import ReplyLanguage
from app.schemas.product_action import CompanionAction, CompanionOffer
from app.schemas.product_brief import BriefMode
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerCatalogCapability
from app.schemas.room_opener import RoomPieceOffer, RoomQuestion, RoomQuestionKind
from app.schemas.seating_solution import (
    SeatingShape,
    SeatingShapeOption,
    SeatingSolution,
    SeatingSolutionOutcome,
)
from app.services.chip_wording import CHIPS, Chip
from app.services.cross_sell import companion_choices
from app.services.next_step import next_step
from app.services.room_presentation import piece_picker, room_answer_choices, swap_offer_choices
from app.services.seating_presentation import seating_choices
from app.taxonomy import attributes as attributes_module
from app.taxonomy import briefs as briefs_module
from app.taxonomy import complements as complements_module
from app.taxonomy import rooms as rooms_module
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.briefs import load_briefs
from app.taxonomy.complements import load_complements
from app.taxonomy.rooms import PieceTier, load_room_pieces
from app.taxonomy.seating import load_seating_semantics

from tests.unit.test_product_brief import CONTEXT, ROOMS, TAXONOMY, _builder, _need, _search
from tests.unit.test_product_brief import _coordinator as _card_coordinator

AR, EN = ReplyLanguage.AR, ReplyLanguage.EN
LATIN = re.compile(r"[A-Za-z]")


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


# ── the chip table ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", list(Chip))
def test_every_chip_is_worded_in_both_languages(key: Chip) -> None:
    assert set(CHIPS[key]) == {EN, AR}


@pytest.mark.parametrize("key", list(Chip))
def test_the_arabic_never_invents_a_figure_or_leaves_english(key: Chip) -> None:
    """It may leave out a name the English uses - never one the chip is not
    given - and outside its placeholders it is Arabic."""
    english, arabic = CHIPS[key][EN], CHIPS[key][AR]
    given = _placeholders(english.label) | _placeholders(english.value)
    for text in (arabic.label, arabic.value):
        assert _placeholders(text) <= given
        assert not LATIN.search(re.sub(r"\{\w+\}", "", text)), text


# ── the next step ───────────────────────────────────────────────────────────


def _result(reply_language: ReplyLanguage | None) -> CustomerTurnResult:
    return CustomerTurnResult(
        state=AgentStateV1(),
        decision=CustomerAgentDecision(action=AgentAction.ANSWER),
        grounding=TurnGrounding(),
        reply_language=reply_language,
    )


def test_the_next_step_chips_are_english_exactly_as_before() -> None:
    step = next_step(_result(None), None)

    assert step is not None
    assert [(c.label, c.value) for c in step.chips] == [
        ("Find a piece", "I'm looking for a piece of furniture"),
        ("Design a room", "I'd like to design a room"),
    ]


def test_the_next_step_chips_are_arabic_in_an_arabic_turn() -> None:
    step = next_step(_result(AR), None)

    assert step is not None
    assert [(c.label, c.value) for c in step.chips] == [
        ("ابحث عن قطعة", "أبحث عن قطعة أثاث"),
        ("صمّم غرفة", "أودّ تصميم غرفة"),
    ]


def test_arabic_piece_choices_cover_every_approved_catalog_type() -> None:
    result = _result(AR).model_copy(
        update={
            "grounding": TurnGrounding(
                clarification=BlockingClarification(
                    reason=BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE,
                    question="ما نوع الأثاث الذي تبحث عنه؟",
                )
            )
        }
    )
    keys = TAXONOMY.categories | set().union(
        *(TAXONOMY.subcategories(category) for category in TAXONOMY.categories)
    )
    for key in keys:
        category = (
            key
            if TAXONOMY.is_category(key)
            else next(
                category for category in TAXONOMY.categories if TAXONOMY.is_pair(category, key)
            )
        )
        capability = RetailerCatalogCapability(
            commerce_category=category,
            commerce_subcategory=None if TAXONOMY.is_category(key) else key,
            active_product_count=1,
        )
        step = next_step(
            result,
            None,
            capabilities=RetailerCatalogCapabilities(capabilities=(capability,)),
            taxonomy=TAXONOMY,
        )
        assert step is not None and len(step.chips) == 1, key
        assert not LATIN.search(step.chips[0].label + step.chips[0].value), key
        assert step.chips[0].label == TAXONOMY.arabic(key)


@pytest.mark.parametrize("language", [AR, EN])
def test_the_room_type_question_has_supported_localized_choices(language: ReplyLanguage) -> None:
    result = _result(language).model_copy(
        update={
            "grounding": TurnGrounding(
                clarification=BlockingClarification(
                    reason=BlockingClarificationReason.MISSING_ROOM_REQUIREMENTS,
                    question="أي غرفة تودّ تصميمها؟",
                )
            )
        }
    )
    step = next_step(result, None, rooms=ROOMS)

    assert step is not None
    expected = ["غرفة المعيشة", "غرفة النوم"] if language is AR else ["Living room", "Bedroom"]
    assert [choice.label for choice in step.chips] == expected
    if language is AR:
        assert all(not LATIN.search(choice.value) for choice in step.chips)


@pytest.mark.parametrize("message", ["أودّ تصميم غرفة", "أبحث عن قطعة أثاث"])
async def test_arabic_entry_messages_produce_arabic_options_in_the_chat(message: str) -> None:
    from app.services.chat_runtime import ChatRuntime

    from tests.unit.test_turn_coordinator import _coordinator

    reason = (
        BlockingClarificationReason.MISSING_ROOM_REQUIREMENTS
        if "غرفة" in message
        else BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE
    )
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.CLARIFY,
            follow_up_policy=FollowUpPolicy.NONE,
            clarification=BlockingClarification(reason=reason, question="ماذا تفضّل؟"),
        ),
        rooms=ROOMS,
        arabic_replies=True,
    )
    result = await coordinator.run(
        CustomerTurnInput(message=message, state=AgentStateV1(), context=CONTEXT)
    )
    presentation = ChatRuntime.presentation(result)
    assert result.reply_language is AR
    assert presentation is not None and presentation.choices
    assert all(not LATIN.search(choice.label + choice.value) for choice in presentation.choices)


async def test_narrow_product_brief_nouns_are_also_arabic() -> None:
    from dataclasses import replace

    from app.schemas.discovery import ProductSearchRequest
    from app.schemas.query import ResolvedSearch

    from tests.unit.test_product_brief import SOFA_FACTS

    builder, _ = _builder(replace(SOFA_FACTS, kinds=(("bed", None, 20),)))
    built = await builder.build(
        ResolvedSearch(
            request=ProductSearchRequest(commerce_category="bedroom", commerce_subcategory="bed")
        ),
        AgentStateV1(),
        CONTEXT,
        mode=BriefMode.ASK,
        language=AR,
    )
    assert built is not None
    assert built.card.noun == "سرير"
    assert not LATIN.search(built.card.submit_label)


# ── a room's questions ──────────────────────────────────────────────────────


def _question(kind: RoomQuestionKind, **fields: object) -> RoomQuestion:
    return RoomQuestion(room_kind="living_room", kind=kind, **fields)  # type: ignore[arg-type]


def test_room_budget_chips_keep_their_figures_in_arabic() -> None:
    english = room_answer_choices(_question(RoomQuestionKind.BUDGET))
    arabic = room_answer_choices(_question(RoomQuestionKind.BUDGET), AR)

    assert [c.label for c in english] == [
        "Under 10,000 SAR",
        "10,000-25,000 SAR",
        "25,000-50,000 SAR",
        "No strict limit",
    ]
    assert [c.label for c in arabic] == [
        "أقل من 10,000 ريال",
        "10,000-25,000 ريال",
        "25,000-50,000 ريال",
        "بدون حد معيّن",
    ]
    assert arabic[1].value == "ميزانية بين 10000 و25000 ريال"


def test_seat_chips_confirm_an_earlier_count_in_arabic() -> None:
    chips = room_answer_choices(_question(RoomQuestionKind.SEATS, earlier_seat_count=9), AR)

    assert chips[0].label == "نعم، 9"
    assert chips[0].value == "عدد الأشخاص: 9"
    assert chips[-1].label == "لـ 6 أو أكثر"


def test_colour_chips_name_the_stores_colours_in_arabic() -> None:
    question = _question(
        RoomQuestionKind.COLOUR, colours=("Grey", "Beige"), colours_ar=("رمادي", "بيج")
    )

    english = room_answer_choices(question)
    arabic = room_answer_choices(question, AR)

    assert [(c.label, c.value) for c in english[:2]] == [
        ("Grey", "I'd like grey tones"),
        ("Beige", "I'd like beige tones"),
    ]
    assert [(c.label, c.value) for c in arabic[:2]] == [
        ("رمادي", "أفضّل لون رمادي"),
        ("بيج", "أفضّل لون بيج"),
    ]
    assert arabic[-1].label == "اختر أنت"


def test_an_arabic_name_is_given_for_every_colour_or_none() -> None:
    with pytest.raises(ValueError):
        _question(RoomQuestionKind.COLOUR, colours=("Grey", "Beige"), colours_ar=("رمادي",))


def test_the_piece_picker_names_the_pieces_in_arabic_and_marks_a_pick() -> None:
    pieces = (
        RoomPieceOffer(
            key="sofa",
            label="Sofa · your pick",
            label_ar="كنبة",
            tier=PieceTier.ESSENTIAL,
            selected=True,
            picked=True,
        ),
        RoomPieceOffer(
            key="rug", label="Rug", label_ar="سجادة", tier=PieceTier.ESSENTIAL, selected=True
        ),
    )
    question = _question(RoomQuestionKind.PIECES, pieces=pieces)

    english = piece_picker(question)
    arabic = piece_picker(question, AR)

    assert english is not None and arabic is not None
    assert [p.label for p in english.pieces] == ["Sofa · your pick", "Rug"]
    assert english.submit_label == "Design my room"
    assert [p.label for p in arabic.pieces] == ["كنبة · من اختياراتك", "سجادة"]
    assert arabic.submit_label == "صمّم غرفتي"
    assert arabic.choose_for_me.label == "اختر لي"


def test_every_registry_piece_has_an_arabic_label() -> None:
    for kind in ROOMS.kinds:
        template = ROOMS.template(kind)
        assert template is not None
        assert all(piece.label_ar for piece in template.pieces), kind


@pytest.mark.parametrize("stage", list(SwapBudgetOfferStage))
def test_a_swap_answer_does_the_same_in_either_language(stage: SwapBudgetOfferStage) -> None:
    offer = SwapBudgetOffer.model_construct(stage=stage)

    english = swap_offer_choices(offer)
    arabic = swap_offer_choices(offer, AR)

    assert [c.bundle_action for c in arabic] == [c.bundle_action for c in english]
    assert all(not LATIN.search(c.label) for c in arabic)


# ── seating shapes and companions ───────────────────────────────────────────


def _shapes() -> SeatingSolution:
    return SeatingSolution.model_construct(
        target_seats=8,
        currency="SAR",
        outcome=SeatingSolutionOutcome.CHOOSE_SHAPE,
        options=(
            SeatingShapeOption.model_construct(
                shape=SeatingShape.SEPARATE_SOFAS, from_price=Decimal("3700")
            ),
            SeatingShapeOption.model_construct(
                shape=SeatingShape.SOFA_WITH_EXTRA_SEATS, from_price=Decimal("3710")
            ),
        ),
        bundles=(),
    )


def test_seating_shapes_quote_their_real_totals_in_either_language() -> None:
    english = seating_choices(_shapes())
    arabic = seating_choices(_shapes(), AR)

    assert [c.label for c in english] == [
        "Separate sofas · from 3,700 SAR",
        "Sofa + armchairs · from 3,710 SAR",
        "Either - show me both",
    ]
    assert [c.label for c in arabic] == [
        "كنبات منفصلة · من 3,700 ريال",
        "كنبة + كراسي منفردة · من 3,710 ريال",
        "أيٌّ منهما - أرني الاثنين",
    ]


def test_companion_chips_run_the_same_search_in_either_language() -> None:
    offers = (
        CompanionOffer(category="decor", subcategory="carpet", label="rugs", label_ar="سجاد"),
    )

    english = companion_choices(offers, offering=True)
    arabic = companion_choices(offers, offering=True, language=AR)

    assert [(c.label, c.value) for c in english] == [
        ("Rugs", "Show me rugs to go with it"),
        ("No thanks", "No thanks"),
    ]
    assert [(c.label, c.value) for c in arabic] == [
        ("سجاد", "أرني ما يناسبه من سجاد"),
        ("لا، شكرًا", "لا، شكرًا"),
    ]
    assert arabic[0].product_action == CompanionAction(category="decor", subcategory="carpet")


# ── the card of questions ───────────────────────────────────────────────────


async def test_the_arabic_card_asks_the_same_with_the_same_keys() -> None:
    builder, _ = _builder()

    english = await builder.build(_need(), AgentStateV1(), CONTEXT, mode=BriefMode.ASK)
    arabic = await builder.build(_need(), AgentStateV1(), CONTEXT, mode=BriefMode.ASK, language=AR)

    assert english is not None and arabic is not None
    assert arabic.pending == english.pending, "taps read back identically"
    assert [q.kind for q in arabic.card.questions] == [q.kind for q in english.card.questions]
    for en_question, ar_question in zip(english.card.questions, arabic.card.questions, strict=True):
        assert [c.key for c in ar_question.choices] == [c.key for c in en_question.choices]
    titles = {q.kind.value: q.label for q in arabic.card.questions}
    assert titles["type"] == "أي نوع؟"
    assert titles["feel"] == "ملمس القماش"
    kinds = next(q for q in arabic.card.questions if q.kind.value == "type")
    assert "زاوية (L)" in [c.label for c in kinds.choices]
    budget = next(q for q in arabic.card.questions if q.kind.value == "budget")
    assert budget.choices[0].label.endswith("ريال")
    colours = next(q for q in arabic.card.questions if q.kind.value == "colour")
    assert colours.choices[0].label == "بيج"
    assert arabic.card.submit_label == "أرني الكنب"


async def test_the_english_card_is_unchanged() -> None:
    builder, _ = _builder()

    built = await builder.build(_need(), AgentStateV1(), CONTEXT, mode=BriefMode.ASK)

    assert built is not None
    assert built.card.submit_label == "Show me sofas"
    assert built.card.questions[0].label == "What kind?"


# ── the registries ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("module", "filename", "loader"),
    [
        (attributes_module, "catalog_attributes_v1.yaml", lambda p: load_catalog_attributes(p)),
        (complements_module, "complements_v1.yaml", lambda p: load_complements(p, TAXONOMY)),
        (briefs_module, "briefs_v1.yaml", lambda p: load_briefs(p, TAXONOMY)),
        (
            rooms_module,
            "room_pieces_v1.yaml",
            lambda p: load_room_pieces(
                p, taxonomy=TAXONOMY, seating=load_seating_semantics(taxonomy=TAXONOMY)
            ),
        ),
    ],
    ids=["attributes", "complements", "briefs", "rooms"],
)
def test_a_registry_missing_one_arabic_name_refuses_to_load(
    tmp_path: Path, module: object, filename: str, loader: object
) -> None:
    """A gap would show an Arabic customer an English chip."""
    source = Path(module.__file__).parent / filename  # type: ignore[attr-defined]
    copy = tmp_path / filename
    shutil.copy(source, copy)
    lines = copy.read_text(encoding="utf-8").splitlines()
    last_arabic_line = max(i for i, line in enumerate(lines) if re.search(r":\s*\S*[؀-ۿ]", line))
    del lines[last_arabic_line]
    copy.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(TaxonomyConfigurationError):
        loader(copy)  # type: ignore[operator]


# ── the decision model reads Arabic piece names ─────────────────────────────


def test_the_room_list_shows_arabic_names_only_when_on() -> None:
    off = decision_prompt.build_instructions(rooms=ROOMS)
    on = decision_prompt.build_instructions(rooms=ROOMS, reply_language=True)

    assert "sofa (Sofa)" in off and "كنبة" not in off
    assert "sofa (Sofa / كنبة)" in on


# ── the card is built in the language of the reply beside it ──────────────


@pytest.mark.parametrize(
    ("message", "stored", "decision", "on", "submit"),
    [
        ("أبحث عن كنبة", None, {}, True, "أرني الكنب"),
        # Arabic in Latin letters, read by the decision: the card is Arabic.
        ("abi kanaba", None, {"writes_arabizi": True}, True, "أرني الكنب"),
        # "English please" in an Arabic session: the card is English.
        ("English please, show me sofas", AR, {"switch_reply_language": EN}, True, "Show me sofas"),
        # Off, whatever was written: exactly today's card.
        ("أبحث عن كنبة", None, {}, False, "Show me sofas"),
    ],
    ids=["arabic", "arabizi", "switch_to_english", "off"],
)
async def test_the_card_matches_the_turns_language(
    message: str,
    stored: ReplyLanguage | None,
    decision: dict[str, object],
    on: bool,
    submit: str,
) -> None:
    coordinator, _ = _card_coordinator(_search().model_copy(update=decision), arabic_replies=on)
    state = AgentStateV1().model_copy(update={"reply_language": stored})
    turn = CustomerTurnInput(message=message, state=state, context=CONTEXT)

    result = await coordinator.run(turn)

    assert result.product_brief is not None
    assert result.product_brief.submit_label == submit


def test_every_arabic_piece_name_fits_its_chip_when_marked_as_a_pick() -> None:
    """The picker marks a pick in Arabic; the marked label must still fit."""
    from app.schemas.chat import PieceChoice
    from app.services.chip_wording import PIECE_PICKED

    for kind in ROOMS.kinds:
        template = ROOMS.template(kind)
        assert template is not None
        for piece in template.pieces:
            assert piece.label_ar is not None
            PieceChoice(label=PIECE_PICKED[AR].format(label=piece.label_ar), selected=True)
