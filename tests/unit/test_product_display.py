"""A product's text ready to show, and the words around it, in the reply's language.

The backend - not a client - words what a card shows: the name (in Arabic, the
store's `name_arabic` as stored), the kind, colour and styles in the
registries' reviewed words, the seats, price and listed size, and the labels a
widget draws around them. A store's widget shows them as they are. Display
only: every English value stays beside them.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, cast

import pytest
from app.core.config import CatalogSettings, SessionSettings
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_state import AgentStateV1, ProductInteractionState
from app.schemas.agent_turn import CustomerResponse
from app.schemas.bundle import BundleStatus
from app.schemas.bundle_presentation import (
    GroundedBundleItem,
    GroundedBundlePresentation,
    GroundedBundleTotals,
)
from app.schemas.card_labels import CardLabels
from app.schemas.chat import ChatPresentation, ChatRequest, ChatResponse
from app.schemas.comparison import ComparisonField, ProductComparisonResult
from app.schemas.dimensions import DimensionStatus, DimensionUnit, NormalisedDimensions
from app.schemas.grounding import GroundedProduct
from app.schemas.language import ReplyLanguage
from app.schemas.picks import PickView
from app.schemas.product import CommerceClassification, ProductCandidate, ProductDisplay
from app.schemas.seating_solution import SeatingBundle, SeatingBundleLine, SeatingShape
from app.schemas.visualization import VisualizeRequest
from app.services.bundle_presentation import build_state_bundle_presentation
from app.services.card_labels import LABELS, card_labels
from app.services.catalog import CatalogService, catalog_item
from app.services.chat_runtime import ChatRuntime
from app.services.furniture_finder import FinderTurnRuntime
from app.services.numeric_guard import picks_figures
from app.services.product_display import (
    ProductDisplayWords,
    arabic_name,
    price_words,
    seat_words,
    size_words,
)
from app.services.product_interaction import build_picks
from app.services.refinement_composer import SearchRefinementComposer
from app.services.screen_view import screen_from_presentation
from app.services.seating_presentation import present_chosen_seating
from app.services.similar_search import SimilarSearchBuilder

from tests.unit import test_design_revision as design
from tests.unit import test_furniture_finder as finder
from tests.unit import test_room_visualization as visualization
from tests.unit.test_catalog import (
    ATTRIBUTES,
    CONTEXT,
    TAXONOMY,
    FakeBrowseRepository,
    a_query,
    a_row,
)

EN, AR = ReplyLanguage.EN, ReplyLanguage.AR
WORDS = ProductDisplayWords(TAXONOMY, ATTRIBUTES)

SIZE = NormalisedDimensions(
    length_cm=Decimal("220"),
    width_cm=Decimal("95.5"),
    height_cm=Decimal("85"),
    unit=DimensionUnit.CENTIMETRE,
    status=DimensionStatus.NORMALISED,
)
NO_SIZE = NormalisedDimensions(status=DimensionStatus.ABSENT)


def _grounded(ref: int = 1, *, name_arabic: str = "طقم أريكة زاوية 4 مقاعد") -> GroundedProduct:
    return GroundedProduct(
        grounding_ref=ref,
        presented_ordinal=ref,
        name_english=f"4 Seater Fabric Corner Sofa Set {ref}",
        name_arabic=name_arabic,
        price_amount=Decimal("6950.00"),
        price_unit="SAR",
        image_url=f"https://shop.test/{ref}.jpg",
        product_url=f"https://shop.test/p/{ref}",
        commerce=CommerceClassification(
            category="seating", subcategory="sofa-set", seating_capacity=4
        ),
        dimensions=SIZE,
        main_color="Light Grey",
        styles=("Modern", "Modern_Classic"),
        relaxation_depth=0,
    )


def _candidate(product_id: int) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english="4 Seater Fabric Sofa",
        name_arabic="أريكة 4 مقاعد",
        price_amount=Decimal("3200"),
        price_unit="SAR",
        image_url=f"https://shop.test/{product_id}.jpg",
        product_url=f"https://shop.test/p/{product_id}",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NO_SIZE,
        main_color="Beige",
        styles=("Modern",),
    )


def _runtime(display: ProductDisplayWords | None = WORDS) -> ChatRuntime:
    unused = cast(Any, None)
    return ChatRuntime(unused, unused, unused, unused, display=display)


def _reply(
    presentation: ChatPresentation,
    language: ReplyLanguage | None,
    runtime: ChatRuntime | None = None,
) -> ChatResponse:
    return (runtime or _runtime()).public_response(
        ChatRequest(session_id="s-1", store_id=50, message="كنب"),
        2,
        CustomerResponse(message="تفضل"),
        presentation,
        reply_language=language,
    )


# ── the product's text ══════════════════════════════════════════════════════


def test_a_card_reads_in_arabic_ready_to_show() -> None:
    assert WORDS.product(_grounded(), AR) == ProductDisplay(
        name="طقم أريكة زاوية 4 مقاعد",
        kind="طقم كنب",
        color="رمادي فاتح",
        styles=("عصري", "كلاسيكي عصري"),
        seats="4 مقاعد",
        price="6,950 ريال",
        size="الطول 220 · العرض 95.5 · الارتفاع 85 سم",
    )


def test_a_card_reads_in_english_as_the_console_always_showed() -> None:
    assert WORDS.product(_grounded(), EN) == ProductDisplay(
        name="4 Seater Fabric Corner Sofa Set 1",
        kind="sofa set",
        color="Light Grey",
        styles=("Modern", "Modern Classic"),
        seats="4-seat",
        price="6,950 SAR",
        size="L 220 · W 95.5 · H 85 cm",
    )


def test_the_arabic_name_is_shown_as_stored_and_english_only_where_it_is_empty() -> None:
    assert arabic_name("Ridged Ceramic Vase", "vas de céramique nervuré") == (
        "vas de céramique nervuré"
    )
    assert arabic_name("Bed", "  ") == "Bed"
    assert WORDS.product(_grounded(name_arabic=""), AR).name == "4 Seater Fabric Corner Sofa Set 1"


@pytest.mark.parametrize(
    ("count", "english", "arabic"),
    [
        (1, "1-seat", "مقعد واحد"),
        (2, "2-seat", "مقعدان"),
        (4, "4-seat", "4 مقاعد"),
        (12, "12-seat", "12 مقعدًا"),
    ],
)
def test_seats_are_worded_for_the_language(count: int, english: str, arabic: str) -> None:
    assert seat_words(count, EN) == english
    assert seat_words(count, AR) == arabic


def test_a_price_and_a_size_are_worded_for_the_language() -> None:
    assert price_words(Decimal("1250.50"), "SAR", AR) == "1,250.5 ريال"
    assert price_words(Decimal("20"), "USD", AR) == "20 USD"
    assert size_words(NO_SIZE, AR) is None
    assert size_words(SIZE.model_copy(update={"height_cm": None}), EN) == "L 220 · W 95.5 cm"


def test_a_value_with_no_reviewed_word_is_shown_as_stored() -> None:
    product = _grounded().model_copy(
        update={
            "main_color": "Moonbeam",
            "styles": ("Futurist_Wave",),
            "commerce": CommerceClassification(category="seating", subcategory=None),
        }
    )
    shown = WORDS.product(product, AR)

    assert (shown.kind, shown.color, shown.styles) == ("مقاعد", "Moonbeam", ("Futurist Wave",))


# ── the chat ════════════════════════════════════════════════════════════════


def test_the_cards_focus_and_comparison_read_in_the_replys_language() -> None:
    first, second = _grounded(1), _grounded(2)
    presentation = ChatPresentation(
        products=(first, second),
        focus=first,
        comparison=ProductComparisonResult(products=(first, second)),
    )

    reply = _reply(presentation, AR)

    shown = reply.presentation
    assert shown is not None and shown.focus is not None and shown.comparison is not None
    assert [p.display.price for p in shown.products if p.display] == ["6,950 ريال"] * 2
    assert shown.focus.display is not None and shown.focus.display.seats == "4 مقاعد"
    assert all(p.display is not None for p in shown.comparison.products)
    # Display only: the English every later turn reads is untouched.
    assert shown.products[0].name_english == "4 Seater Fabric Corner Sofa Set 1"


def test_every_reply_carries_its_labels_in_its_language() -> None:
    presentation = ChatPresentation(products=(_grounded(),))

    arabic = _reply(presentation, AR)
    english = _reply(presentation, None)

    assert arabic.labels == LABELS[AR] and arabic.labels.select == "أضف إلى السلة"
    assert english.labels == LABELS[EN] and english.labels.select == "Add to cart"
    assert english.presentation is not None and english.presentation.products[0].display is not None
    assert english.presentation.products[0].display.price == "6,950 SAR"


def test_a_reply_written_in_arabic_names_the_cards_as_the_store_does() -> None:
    products = (_grounded(1), _grounded(2, name_arabic=""))

    arabic = screen_from_presentation(products=products, arabic_names=True)

    assert [card.name for card in arabic.products] == [
        "طقم أريكة زاوية 4 مقاعد",
        "4 Seater Fabric Corner Sofa Set 2",
    ]


# ── the room package ════════════════════════════════════════════════════════


def _piece(
    ref: int = 1,
    *,
    quantity: int = 2,
    owned: bool = False,
    name_arabic: str = "سرير منجّد مفرد",
) -> GroundedBundleItem:
    return GroundedBundleItem(
        grounding_ref=ref,
        name_english=f"Single Upholstered Bed {ref}",
        name_arabic=name_arabic,
        image_url=f"https://shop.test/{ref}.jpg",
        product_url=f"https://shop.test/p/{ref}",
        commerce=CommerceClassification(category="bedroom", subcategory="bed"),
        quantity=quantity,
        acquisition=BundleAcquisition.ALREADY_OWNED if owned else BundleAcquisition.TO_BUY,
        locked=owned,
        unit_price=Decimal("1360.00"),
        price_unit="SAR",
        new_spend_line_total=None if owned else Decimal("1360.00") * quantity,
    )


def _package(*items: GroundedBundleItem) -> GroundedBundlePresentation:
    return GroundedBundlePresentation(
        status=BundleStatus.COMPLETE,
        items=items,
        totals=GroundedBundleTotals(
            new_spend_total=Decimal("10570"),
            currency="SAR",
            budget_max_amount=Decimal("12000"),
            budget_currency="SAR",
            within_budget=True,
        ),
    )


def test_a_room_piece_reads_in_arabic_ready_to_show() -> None:
    shown = WORDS.bundle_item(_piece(), AR)

    assert shown.name == "سرير منجّد مفرد"
    assert shown.kind == TAXONOMY.arabic("bed")
    assert (shown.price, shown.line_total) == ("1,360 ريال", "2,720 ريال")


def test_a_room_piece_reads_in_english_and_an_owned_one_adds_nothing() -> None:
    bought = WORDS.bundle_item(_piece(), EN)
    owned = WORDS.bundle_item(_piece(owned=True, quantity=1), AR)

    assert (bought.name, bought.kind) == ("Single Upholstered Bed 1", "bed")
    assert (bought.price, bought.line_total) == ("1,360 SAR", "2,720 SAR")
    assert owned.price == "1,360 ريال" and owned.line_total is None


def test_the_rooms_totals_are_worded_and_absent_ones_stay_absent() -> None:
    totals = _package(_piece()).totals

    assert WORDS.bundle_totals(totals, AR).model_dump() == {
        "new_spend": "10,570 ريال",
        "budget": "12,000 ريال",
    }
    bare = WORDS.bundle_totals(GroundedBundleTotals(), EN)
    assert bare.new_spend is None and bare.budget is None


def test_the_room_and_seating_read_in_the_replys_language() -> None:
    package = _package(_piece(1), _piece(2, name_arabic=""))
    presentation = ChatPresentation(
        room=package, seating_bundles=(package,), chosen_seating=package
    )

    shown = _reply(presentation, AR).presentation

    assert shown is not None and shown.room is not None and shown.chosen_seating is not None
    for bundle in (shown.room, shown.seating_bundles[0], shown.chosen_seating):
        assert [item.display.name for item in bundle.items if item.display] == [
            "سرير منجّد مفرد",
            "Single Upholstered Bed 2",
        ]
        assert bundle.totals.display is not None
        assert bundle.totals.display.new_spend == "10,570 ريال"
    # Display only: the English and the figures the arithmetic used are untouched.
    assert shown.room.items[0].name_english == "Single Upholstered Bed 1"
    assert shown.room.totals.new_spend_total == Decimal("10570")


def test_a_room_without_display_words_serialises_as_before() -> None:
    dumped = _package(_piece()).model_dump(mode="json")

    assert "display" not in dumped["items"][0] and "display" not in dumped["totals"]


def test_the_room_and_a_seating_choice_carry_the_stores_arabic_name() -> None:
    state = design.a_room(needs=(design.need(),), lines=(design.line(10),))
    product = design.product(10).model_copy(update={"name_arabic": "كنبة"})
    room = build_state_bundle_presentation(design.room(state), [product])
    seating = present_chosen_seating(
        SeatingBundle(
            shape=next(iter(SeatingShape)),
            lines=(
                SeatingBundleLine(
                    product_id=10,
                    name="Sofa",
                    name_arabic="كنبة",
                    commerce_subcategory="sofa",
                    unit_price=Decimal("1000"),
                    quantity=1,
                    seats_each=3,
                    seats_are_confirmed=True,
                    image_url="https://shop.test/10.jpg",
                    product_url="https://shop.test/p/10",
                ),
            ),
            total_seats=3,
            total_price=Decimal("1000"),
            currency="SAR",
        )
    )

    assert room is not None and room.items[0].name_arabic == "كنبة"
    assert seating.items[0].name_arabic == "كنبة"


def test_a_reply_written_in_arabic_names_the_room_pieces_as_the_cards_do() -> None:
    package = _package(_piece(1), _piece(2, name_arabic=""))

    arabic = screen_from_presentation(room=package, arabic_names=True)
    english = screen_from_presentation(room=package)

    assert arabic.room is not None and english.room is not None
    assert [card.name for card in arabic.room.cards] == [
        "سرير منجّد مفرد",
        "Single Upholstered Bed 2",
    ]
    assert english.room.cards[0].name == "Single Upholstered Bed 1"


# ── the labels ══════════════════════════════════════════════════════════════


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_every_label_reads_in_both_languages_and_arabic_adds_no_placeholder() -> None:
    """An Arabic label may leave out what its English names - the customer's
    own Arabic message names no product - but never asks for a value the
    English does not, which a client filling the English would not supply."""
    english, arabic = LABELS[EN].model_dump(), LABELS[AR].model_dump()

    assert set(english) == set(arabic) == set(CardLabels.model_fields)
    for field in english:
        assert english[field] and arabic[field], field
        assert set(_PLACEHOLDER.findall(arabic[field])) <= set(
            _PLACEHOLDER.findall(english[field])
        ), field
    assert card_labels(None) == LABELS[EN]


def test_every_comparison_row_has_a_name_in_both_languages() -> None:
    for field in ComparisonField:
        assert f"field_{field.value}" in CardLabels.model_fields, field


async def test_a_room_picture_carries_the_conversations_labels() -> None:
    sessions = visualization.FakeSessionStore()
    state = visualization.a_room_state().model_copy(update={"reply_language": AR})
    await visualization.a_stored_session(sessions, state)
    runtime, _ = visualization.a_turn_runtime(sessions, arabic_replies=True)

    reply = await runtime.visualize(
        VisualizeRequest(session_id=visualization.SESSION, store_id=visualization.STORE),
        visualization.CONTEXT,
    )

    assert reply.reply_language is AR and reply.labels == LABELS[AR]


async def test_a_photos_matches_read_like_a_chat_turns_cards() -> None:
    service, _ = finder.a_finding_service()
    runtime = FinderTurnRuntime(
        service,
        finder.FakeSessionStore(),  # type: ignore[arg-type]
        SessionSettings(max_history_messages=6),
        SimilarSearchBuilder(TAXONOMY, ATTRIBUTES),
        SearchRefinementComposer(ATTRIBUTES, finder.DIMENSIONS),
        display=WORDS,
    )

    reply = await runtime.pick(finder.a_pick(), finder.CONTEXT)

    assert reply.labels == LABELS[EN]
    assert reply.presentation is not None and reply.presentation.products
    assert all(product.display is not None for product in reply.presentation.products)


# ── the picks tray ══════════════════════════════════════════════════════════


def test_picks_read_in_the_conversations_language() -> None:
    state = AgentStateV1(product_interaction=ProductInteractionState(selected_product_ids=(7,)))

    [arabic] = build_picks(state, [_candidate(7)], WORDS, AR)
    [english] = build_picks(state, [_candidate(7)], WORDS)
    [bare] = build_picks(state, [_candidate(7)])

    assert arabic.display is not None and arabic.display.name == "أريكة 4 مقاعد"
    assert english.display is not None and english.display.price == "3,200 SAR"
    assert bare.display is None


def test_a_figure_in_a_picks_shown_name_may_be_said() -> None:
    pick = PickView(
        pick=1,
        name_english="Fabric Sofa",
        image_url="https://shop.test/1.jpg",
        price_amount=Decimal("3200"),
        price_unit="SAR",
        display=ProductDisplay(name="أريكة 6 مقاعد"),
    )

    assert Decimal("6") in picks_figures([pick])


# ── Browse Catalogue ════════════════════════════════════════════════════════


def _catalogue(*, arabic_replies: bool = True) -> CatalogService:
    return CatalogService(
        cast(Any, FakeBrowseRepository([a_row(1)])),
        TAXONOMY,
        ATTRIBUTES,
        CatalogSettings(),
        render_available=True,
        display=WORDS,
        arabic_replies=arabic_replies,
    )


def test_a_tile_reads_in_the_requested_language() -> None:
    assert catalog_item(a_row(1), WORDS, AR).display == ProductDisplay(
        name="قطعة",
        kind="كنبة",
        color="بيج",
        styles=("عصري",),
        price="2,500 ريال",
        size="الطول 220 · العرض 95 · الارتفاع 80 سم",
    )
    assert catalog_item(a_row(1)).display is None


async def test_the_catalogue_reads_in_the_requested_language() -> None:
    service = _catalogue()

    facets = await service.facets(CONTEXT, AR)
    page = await service.browse(a_query(language="ar"), CONTEXT)

    seating = next(c for c in facets.categories if c.value == "seating")
    assert seating.label == "مقاعد"
    assert {s.value: s.label for s in seating.subcategories} == {"chair": "كرسي", "sofa": "كنبة"}
    assert {c.value: c.label for c in facets.colors} == {"Beige": "بيج", "Grey": "رمادي"}
    assert (
        facets.studio.style_labels is not None
        and facets.studio.style_labels["Japandi"] == "جاباندي"
    )
    assert page.items[0].display is not None and page.items[0].display.price == "2,500 ريال"
    # The room step's choices and the currency a client totals in, in Arabic too.
    rooms = {option.value: option.label for option in facets.studio.room_types}
    assert rooms["bedroom"] == "غرفة النوم" and rooms["majlis"] == "المجلس"
    assert facets.price is not None and facets.price.currency_label == "ريال"


async def test_without_a_language_or_with_arabic_off_the_catalogue_reads_in_english() -> None:
    english = await _catalogue().facets(CONTEXT)
    switched_off = await _catalogue(arabic_replies=False).browse(a_query(language="ar"), CONTEXT)

    assert next(c for c in english.colors if c.value == "Beige").label == "Beige"
    assert next(o for o in english.studio.room_types if o.value == "bedroom").label == "Bedroom"
    assert switched_off.items[0].display is not None
    assert switched_off.items[0].display.name == "Piece 1"
