"""The store's own colours, styles and materials are trusted data.

What the stores hold joins the reviewed vocabulary at startup, so a colour a
store sells can be offered and searched though the file does not name it. A
material the customer wants puts the pieces made of it first, never filters. A
chip Sara offers that names a colour, style or material names one the store was
shown to carry (CLAUDE.md 10.2, 12.4).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from app.agents.chip_check import chips_not_in_stock
from app.agents.context import ToolContext
from app.agents.tools.shopping import agent_search, as_interpretation
from app.agents.work import TurnWork
from app.core.exceptions import IntegrationUnavailableError
from app.integrations.embeddings import EXPECTED_DIMENSION
from app.integrations.llm import StructuredLLMClient
from app.schemas.agent_state import AgentStateV1, CustomerPreferenceState, KindAsked
from app.schemas.catalog_overview import CatalogOverview, SubcategoryShelf
from app.schemas.discovery import ProductSearchRequest, SeatingCapacityConstraint
from app.schemas.product import EligibleProduct
from app.schemas.query import (
    AttributeInterpretation,
    CommerceInterpretation,
    ConstraintStrength,
    ResolvedSearch,
    SemanticPreference,
    build_constrained_interpretation,
)
from app.schemas.relaxation import RelaxedCandidate
from app.schemas.retailer import RetailerContext
from app.schemas.text_choice import TextReplyChoice
from app.services.query_document import build_query_document
from app.services.query_understanding import QueryUnderstandingService
from app.services.semantic_ranking import SemanticRankingService
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes
from app.taxonomy.seating import load_seating_semantics

from tests.unit.agents.fakes import agent_services, real_loop, tool_context
from tests.unit.test_query_understanding import DIMENSIONS, TAXONOMY, FakeLLMClient

FILE = load_catalog_attributes()
STORED = FILE.with_stored(
    {
        AttributeFamily.COLOR: ["Red", " ", "beige"],
        AttributeFamily.STYLE: ["Vintage"],
        AttributeFamily.MATERIAL: ["Solid Wood", "Velvet", "Glass"],
    }
)


# ── the vocabulary ──────────────────────────────────────────────────────────


def test_what_the_stores_hold_joins_the_file() -> None:
    assert "Red" not in FILE.colors and "Red" in STORED.colors
    assert "Vintage" in STORED.styles
    assert STORED.materials == {"Solid Wood", "Velvet", "Glass"}
    assert not FILE.materials


def test_a_value_the_file_names_keeps_its_spelling_and_blanks_are_skipped() -> None:
    assert "beige" not in STORED.colors and "Beige" in STORED.colors
    assert all(c.strip() for c in STORED.colors)
    assert STORED.canonical(AttributeFamily.COLOR, "red") == "Red"


def test_the_files_own_values_stay_known_as_reviewed() -> None:
    assert STORED.reviewed(AttributeFamily.STYLE) == FILE.styles
    assert "Vintage" not in STORED.reviewed(AttributeFamily.STYLE)


def test_arabic_stays_the_files() -> None:
    assert STORED.arabic(AttributeFamily.COLOR, "Beige") == FILE.arabic(
        AttributeFamily.COLOR, "Beige"
    )
    assert STORED.arabic(AttributeFamily.COLOR, "Red") is None


# ── a material is a leaning ─────────────────────────────────────────────────


def _service() -> QueryUnderstandingService:
    return QueryUnderstandingService(
        cast(StructuredLLMClient, FakeLLMClient()), TAXONOMY, STORED, DIMENSIONS
    )


def test_the_search_form_offers_the_stores_materials_and_they_reach_the_search() -> None:
    # The real, constrained interpretation the tools are built with - the one
    # whose value list must name materials too.
    constrained = build_constrained_interpretation(TAXONOMY, STORED)
    form = agent_search(constrained, STORED)
    search = form.model_validate(
        {
            "commerce_category": "seating",
            "commerce_subcategory": "sofa",
            "materials_wanted": ["Velvet"],
            "colours_wanted": ["Red"],
        }
    )

    outcome = _service().resolve(as_interpretation(search, constrained))

    assert isinstance(outcome, ResolvedSearch)
    wanted = {(p.family, p.canonical_value) for p in outcome.semantic_preferences}
    assert wanted == {(AttributeFamily.MATERIAL, "Velvet"), (AttributeFamily.COLOR, "Red")}
    assert outcome.request.colors_any_of == ()  # wanted, so nothing is hidden
    assert "Material preference: Velvet" in build_query_document(outcome)


def test_a_material_said_firmly_still_never_filters() -> None:
    interpretation = CommerceInterpretation(
        commerce_category="seating",
        commerce_subcategory="sofa",
        attributes=[
            AttributeInterpretation(
                family=AttributeFamily.MATERIAL,
                raw_value="only velvet",
                canonical_value="Velvet",
                strength=ConstraintStrength.LOCKED,
            )
        ],
    )

    outcome = _service().resolve(interpretation)

    assert isinstance(outcome, ResolvedSearch)
    (preference,) = outcome.semantic_preferences
    assert preference.strength is ConstraintStrength.PREFERRED
    assert outcome.request.colors_any_of == () and outcome.request.styles_all_of == ()


class _NoIndex:
    model = "none"

    async def embed_query(self, text: str) -> tuple[float, ...]:
        return tuple(0.1 for _ in range(EXPECTED_DIMENSION))


class _Scores:
    async def score(self, **kwargs: Any) -> dict[int, float]:
        # The glass sofa is the closest by meaning; velvet must still lead.
        return {1: 0.9, 2: 0.2, 3: 0.5}


async def test_the_material_they_want_comes_first_and_nothing_is_hidden() -> None:
    candidates = [
        RelaxedCandidate(
            product=EligibleProduct(product_id=n, price_amount=Decimal(900), material=material),
            relaxation_depth=0,
        )
        for n, material in ((1, "Glass"), (2, "Velvet"), (3, None))
    ]
    resolved = ResolvedSearch(
        request=ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa"),
        semantic_preferences=(
            SemanticPreference(
                family=AttributeFamily.MATERIAL,
                raw_value="velvet",
                canonical_value="Velvet",
                strength=ConstraintStrength.PREFERRED,
            ),
        ),
    )

    result = await SemanticRankingService(_NoIndex(), _Scores()).rank(  # type: ignore[arg-type]
        resolved, candidates, RetailerContext(store_id=1), namespace="store-1"
    )

    assert result.product_ids[0] == 2
    assert sorted(result.product_ids) == [1, 2, 3]


# ── a chip offers what the store carries ────────────────────────────────────

SOFAS = SubcategoryShelf(
    commerce_category="seating",
    commerce_subcategory="sofa",
    active_count=12,
    price_minimum=Decimal(900),
    price_maximum=Decimal(5000),
    colours=("Beige", "Grey"),
    styles=("Modern",),
    materials=("Solid Wood",),
)
LAMPS = SubcategoryShelf(
    commerce_category="lighting",
    commerce_subcategory="table-lamp",
    active_count=4,
    price_minimum=Decimal(90),
    price_maximum=Decimal(500),
    colours=("Red",),
)


SEATING = load_seating_semantics(taxonomy=TAXONOMY)
CORNERS = SubcategoryShelf(
    commerce_category="seating",
    commerce_subcategory="sectional-sofa",
    active_count=3,
    price_minimum=Decimal(3000),
    price_maximum=Decimal(9000),
    colours=("Navy",),
)


class _Shelves:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def overview(self, context: Any) -> CatalogOverview:
        if self.error is not None:
            raise self.error
        return CatalogOverview(store_id=1, shelves=(SOFAS, LAMPS, CORNERS))


def _context(shelves: _Shelves | None = None, discovery: Any = None) -> ToolContext:
    services = SimpleNamespace(
        attributes=STORED,
        capabilities=shelves or _Shelves(),
        seating=SEATING,
        discovery=discovery,
    )
    return ToolContext(retailer=RetailerContext(store_id=1), services=cast(Any, services))


def _asked_about_sofas(**more: Any) -> TurnWork:
    """A later turn: the sofa was asked about before, and nothing ran yet."""
    state = AgentStateV1(
        kind_asked=KindAsked(commerce_category="seating", commerce_subcategory="sofa"), **more
    )
    return TurnWork.starting(state)


def _chips(*labels: str) -> tuple[TextReplyChoice, ...]:
    return tuple(TextReplyChoice(label=label, value=label) for label in labels)


async def test_chips_the_kinds_shelf_lists_pass_on_a_later_turn() -> None:
    chips = _chips("Beige", "grey", "Modern", "Solid Wood", "No preference")

    assert await chips_not_in_stock(chips, _asked_about_sofas(), _context()) == []


async def test_a_colour_or_material_the_kind_lacks_is_sent_back() -> None:
    # Red is on the shelf - for lamps, not for sofas.
    chips = _chips("Beige", "Red", "Velvet")

    off = await chips_not_in_stock(chips, _asked_about_sofas(), _context())

    assert off == ["color: Red", "material: Velvet"]


async def test_a_colour_the_kinds_shown_beside_it_carry_is_on_offer() -> None:
    # A sofa search shows corner sofas too, and they come in navy.
    assert SEATING.shown_with("sofa")
    assert await chips_not_in_stock(_chips("Navy"), _asked_about_sofas(), _context()) == []


async def test_a_chip_naming_two_is_checked_for_each() -> None:
    off = await chips_not_in_stock(_chips("Beige or Red"), _asked_about_sofas(), _context())

    assert off == ["color: Red"]


async def test_what_the_customer_likes_is_not_stock() -> None:
    likes = CustomerPreferenceState(
        semantic_preferences=(
            SemanticPreference(
                family=AttributeFamily.COLOR,
                raw_value="red",
                canonical_value="Red",
                strength=ConstraintStrength.PREFERRED,
            ),
        )
    )

    off = await chips_not_in_stock(
        _chips("Red"), _asked_about_sofas(customer_preferences=likes), _context()
    )

    assert off == ["color: Red"]


async def test_an_arabic_chip_is_read_by_the_files_arabic_name() -> None:
    arabic = FILE.arabic(AttributeFamily.COLOR, "Black")
    assert arabic is not None

    off = await chips_not_in_stock(_chips(arabic), _asked_about_sofas(), _context())

    assert off == ["color: Black"]


async def test_an_unreadable_shelf_sends_nothing_back() -> None:
    shelves = _Shelves(IntegrationUnavailableError(dependency="postgres"))

    assert await chips_not_in_stock(_chips("Red"), _asked_about_sofas(), _context(shelves)) == []


async def test_asking_about_a_kind_is_remembered() -> None:
    tool = real_loop()._registry.get("ask_about")
    assert tool is not None and tool.handler is not None
    services = agent_services()

    outcome = await tool.handler(
        tool.args.model_validate(
            {"search": {"commerce_category": "seating", "commerce_subcategory": "sofa"}}
        ),
        tool_context(services),
        TurnWork.starting(AgentStateV1()),
    )

    assert outcome.state is not None
    assert outcome.state.kind_asked == KindAsked(
        commerce_category="seating", commerce_subcategory="sofa"
    )


# ── what the customer already decided narrows what is offered ───────────────

# Store 50's corner sofas, as the live chat met them: Powder Blue and Light Grey
# come only as 3-seaters, so for a 4-seat corner sofa only Beige and Grey are real.
CORNER_POOL = (
    ("Beige", 4),
    ("Beige", 3),
    ("Grey", 4),
    ("Light Grey", 3),
    ("Powder Blue", 3),
)


class _Discovery:
    """Corner sofas, filtered by the seats a request asks for."""

    def __init__(self) -> None:
        self.asked: list[ProductSearchRequest] = []

    async def eligible_pool(self, request: ProductSearchRequest, context: Any) -> Any:
        self.asked.append(request)
        seats = request.seating_capacity
        return tuple(
            EligibleProduct(
                product_id=n,
                subcategory="sectional-sofa",
                price_amount=Decimal(4000),
                main_color=colour,
                seating_capacity=capacity,
            )
            for n, (colour, capacity) in enumerate(CORNER_POOL)
            if seats is None
            or (
                (seats.min_capacity is None or capacity >= seats.min_capacity)
                and (seats.max_capacity is None or capacity <= seats.max_capacity)
            )
        )


FOUR_SEAT_CORNERS = ProductSearchRequest(
    commerce_category="seating",
    commerce_subcategory="sectional-sofa",
    seating_capacity=SeatingCapacityConstraint(min_capacity=4, max_capacity=4),
)


def _asked_about_four_seat_corners() -> TurnWork:
    return TurnWork.starting(
        AgentStateV1(
            kind_asked=KindAsked(
                commerce_category="seating",
                commerce_subcategory="sectional-sofa",
                decided=FOUR_SEAT_CORNERS,
            )
        )
    )


async def test_a_colour_only_other_sizes_come_in_is_sent_back() -> None:
    off = await chips_not_in_stock(
        _chips("Beige", "Grey", "Light Grey", "Powder Blue"),
        _asked_about_four_seat_corners(),
        _context(discovery=_Discovery()),
    )

    assert off == ["color: Light Grey", "color: Powder Blue"]


async def test_nothing_meeting_what_was_decided_falls_back_to_the_kind() -> None:
    class _Nothing:
        async def eligible_pool(self, request: Any, context: Any) -> Any:
            return ()

    # Navy is on the corner-sofa shelf; with no 4-seater at all, the shelf answers.
    off = await chips_not_in_stock(
        _chips("Navy"), _asked_about_four_seat_corners(), _context(discovery=_Nothing())
    )

    assert off == []


def test_only_what_the_customer_decided_narrows() -> None:
    from app.agents.decided_stock import decided

    kind_only = ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa")
    assert decided(kind_only) is None

    strict_beige = FOUR_SEAT_CORNERS.model_copy(update={"colors_any_of": ("Beige",)})
    chosen = decided(strict_beige)
    assert chosen is not None
    assert chosen.colors_any_of == ()  # the colour is what is being asked
    assert chosen.seating_capacity == FOUR_SEAT_CORNERS.seating_capacity


async def test_ask_about_offers_what_the_decided_size_comes_in() -> None:
    tool = real_loop()._registry.get("ask_about")
    assert tool is not None and tool.handler is not None
    discovery = _Discovery()
    services = agent_services(
        capabilities=cast(Any, _Shelves()), discovery=discovery, seating=SEATING
    )

    outcome = await tool.handler(
        tool.args.model_validate(
            {
                "search": {
                    "commerce_category": "seating",
                    "commerce_subcategory": "sectional-sofa",
                    "seating_capacity_min": 4,
                    "seating_capacity_max": 4,
                }
            }
        ),
        tool_context(services),
        TurnWork.starting(AgentStateV1()),
    )

    has = outcome.facts["this_store_has"]
    assert has["for_what_they_chose"] is True
    assert has["colours"] == ["Beige", "Grey"]
    assert outcome.state is not None and outcome.state.kind_asked is not None
    assert outcome.state.kind_asked.decided is not None


async def test_ask_about_says_when_nothing_meets_what_they_chose() -> None:
    class _Nothing:
        async def eligible_pool(self, request: Any, context: Any) -> Any:
            return ()

    tool = real_loop()._registry.get("ask_about")
    assert tool is not None and tool.handler is not None
    services = agent_services(
        capabilities=cast(Any, _Shelves()), discovery=_Nothing(), seating=SEATING
    )

    outcome = await tool.handler(
        tool.args.model_validate(
            {
                "search": {
                    "commerce_category": "seating",
                    "commerce_subcategory": "sectional-sofa",
                    "seating_capacity_min": 9,
                    "seating_capacity_max": 9,
                }
            }
        ),
        tool_context(services),
        TurnWork.starting(AgentStateV1()),
    )

    assert outcome.facts["nothing_meets_what_they_chose"] is True
    assert "for_what_they_chose" not in outcome.facts["this_store_has"]


async def test_the_kinds_that_do_meet_it_are_named() -> None:
    class _OnlySofas:
        async def eligible_pool(self, request: ProductSearchRequest, context: Any) -> Any:
            if request.commerce_subcategory is not None:
                return ()  # no corner sofa under the budget
            return tuple(
                EligibleProduct(product_id=n, subcategory="sofa", price_amount=Decimal(990))
                for n in range(3)
            )

    tool = real_loop()._registry.get("ask_about")
    assert tool is not None and tool.handler is not None
    services = agent_services(
        capabilities=cast(Any, _Shelves()), discovery=_OnlySofas(), seating=SEATING
    )

    outcome = await tool.handler(
        tool.args.model_validate(
            {
                "search": {
                    "commerce_category": "seating",
                    "commerce_subcategory": "sectional-sofa",
                    "price_max": "3000",
                    "price_currency": "SAR",
                }
            }
        ),
        tool_context(services),
        TurnWork.starting(AgentStateV1()),
    )

    assert outcome.facts["nothing_meets_what_they_chose"] is True
    assert outcome.facts["kinds_that_meet_it"] == ["sofa"]
