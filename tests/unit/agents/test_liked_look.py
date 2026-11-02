"""A like leans the next search to its look (known issue 34, CLAUDE.md 10.2).

The colours of liked pieces of the kind searched and the styles of any liked
piece order a search the customer asks for - after everything they said,
before a designer's direction - and never filter it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.agents.work import TurnWork
from app.core.config import SizeSettings
from app.schemas.agent_state import AgentStateV1, ProductInteractionState
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import ProductSearchRequest
from app.schemas.product import CommerceClassification, EligibleProduct, ProductCandidate
from app.schemas.query import RankingLean, ResolvedSearch
from app.schemas.relaxation import RelaxedCandidate
from app.services.agent_state import remember_taste
from app.services.liked_look import with_liked_look
from app.services.semantic_ranking import _preference_match
from app.taxonomy.seating import load_seating_semantics

from tests.unit.agents.fakes import (
    OFF_SCREEN,
    TAXONOMY,
    FakePipeline,
    agent_services,
    real_loop,
    tool_context,
)

LOOP = real_loop()
SOFAS = {"commerce_category": "seating", "commerce_subcategory": "sofa"}
SOFA_REQUEST = ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa")


def _liked(
    product_id: int, colour: str | None, styles: tuple[str, ...], subcategory: str = "sofa"
) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Piece {product_id}",
        name_arabic="قطعة",
        price_amount=Decimal(1000),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(
            category="lighting" if subcategory == "floor-lamp" else "seating",
            subcategory=subcategory,
        ),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color=colour,
        styles=styles,
    )


BEIGE_SOFA = _liked(201, "Beige", ("Modern",))
GREY_LAMP = _liked(202, "Grey", ("Industrial",), subcategory="floor-lamp")


class LikedHydration:
    def __init__(self, products: tuple[ProductCandidate, ...], error: Exception | None = None):
        self.products = {p.product_id: p for p in products}
        self.error = error

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[Any, ...]:
        if self.error is not None:
            raise self.error
        return tuple(self.products[p] for p in product_ids if p in self.products)


def _likes(*ids: int) -> AgentStateV1:
    return AgentStateV1(product_interaction=ProductInteractionState(liked_product_ids=ids))


async def _call(name: str, args: dict[str, Any], state: AgentStateV1, services: Any) -> Any:
    tool = LOOP._registry.get(name)
    assert tool is not None and tool.handler is not None
    return await tool.handler(
        tool.args.model_validate(args),
        tool_context(services, customer_said="show me sofas"),
        TurnWork.starting(state),
    )


def _services(*liked: ProductCandidate, error: Exception | None = None) -> tuple[Any, FakePipeline]:
    pipeline = FakePipeline(ids=(10, 11))
    hydration = LikedHydration(liked, error)
    return agent_services(pipeline=pipeline, hydration=hydration), pipeline


# ── the look ────────────────────────────────────────────────────────────────


def test_a_colour_leans_only_its_own_kind_a_style_leans_any() -> None:
    lean = with_liked_look(None, (BEIGE_SOFA, GREY_LAMP), SOFA_REQUEST)

    assert lean.learned_colours == ("Beige",), "a grey lamp says nothing about sofas"
    assert lean.learned_styles == ("Modern", "Industrial")
    assert lean.learned and lean.learned_for == "sofa"


def test_a_colour_leans_from_the_kinds_reviewed_to_go_with_the_one_asked() -> None:
    """A light-grey sofa set they liked leans a sofa search, even of sofas alone."""
    grey_set = _liked(203, "Light Grey", ("Modern",), subcategory="sofa-set")

    lean = with_liked_look(None, (grey_set,), SOFA_REQUEST, ("sofa-set", "sectional-sofa"))
    alone = with_liked_look(None, (grey_set,), SOFA_REQUEST)

    assert lean.learned_colours == ("Light Grey",)
    assert alone.learned_colours == (), "with no related kinds, only the kind itself"


def test_a_search_by_family_takes_colours_from_the_whole_family() -> None:
    request = ProductSearchRequest(commerce_category="seating")

    lean = with_liked_look(None, (BEIGE_SOFA, GREY_LAMP), request)

    assert lean.learned_colours == ("Beige",)
    assert lean.learned_for == "seating"


def test_the_lean_it_joins_keeps_everything_else() -> None:
    lean = with_liked_look(RankingLean(said_avoid_colours=("Red",)), (BEIGE_SOFA,), SOFA_REQUEST)

    assert lean.said_avoid_colours == ("Red",)
    assert lean.learned_colours == ("Beige",)


# ── the order ───────────────────────────────────────────────────────────────


def _candidate(product_id: int, colour: str, styles: tuple[str, ...] = ()) -> RelaxedCandidate:
    return RelaxedCandidate(
        product=EligibleProduct(
            product_id=product_id, price_amount=Decimal(1000), main_color=colour, styles=styles
        ),
        relaxation_depth=0,
    )


def _ordered(lean: RankingLean, *candidates: RelaxedCandidate) -> list[int]:
    resolved = ResolvedSearch(request=SOFA_REQUEST, lean=lean)
    key = _preference_match(resolved, SizeSettings())
    return [c.product.product_id for c in sorted(candidates, key=key)]


def test_a_liked_colour_comes_first_and_nothing_is_hidden() -> None:
    lean = RankingLean(learned=True, learned_colours=("Beige",))

    assert _ordered(lean, _candidate(1, "Grey"), _candidate(2, "Beige")) == [2, 1]


def test_what_they_said_to_avoid_outranks_what_they_liked() -> None:
    lean = RankingLean(learned_colours=("Beige",), said_avoid_styles=("Rustic",))

    assert _ordered(
        lean, _candidate(1, "Beige", ("Rustic",)), _candidate(2, "Grey", ("Modern",))
    ) == [2, 1]


def test_what_they_liked_outranks_a_designers_direction() -> None:
    lean = RankingLean(learned_colours=("Beige",), colours=("Grey",))

    assert _ordered(lean, _candidate(1, "Grey"), _candidate(2, "Beige")) == [2, 1]


# ── the search ──────────────────────────────────────────────────────────────


async def test_a_search_they_ask_for_leans_to_what_they_liked_and_says_so() -> None:
    services, pipeline = _services(BEIGE_SOFA, GREY_LAMP)

    outcome = await _call("search_products", {"search": SOFAS}, _likes(201, 202), services)

    lean = pipeline.calls[-1].lean
    assert lean.learned_colours == ("Beige",)
    assert lean.learned_styles == ("Modern", "Industrial")
    assert outcome.facts["leaning_to_liked"] == {
        "colours": ["Beige"],
        "styles": ["Modern", "Industrial"],
    }


async def test_its_next_page_keeps_the_same_lean() -> None:
    services, pipeline = _services(BEIGE_SOFA)
    first = await _call("search_products", {"search": SOFAS}, _likes(201), services)

    await _call("more_results", {}, first.state, services)

    assert pipeline.calls[-1].lean.learned_colours == ("Beige",)


async def test_unliked_it_stops_leaning_the_next_search() -> None:
    services, pipeline = _services(BEIGE_SOFA)

    outcome = await _call("search_products", {"search": SOFAS}, _likes(), services)

    lean = pipeline.calls[-1].lean
    assert lean is None or not lean.learned_colours
    assert "leaning_to_liked" not in outcome.facts


async def test_what_they_said_stays_ahead_of_what_they_liked() -> None:
    services, pipeline = _services(BEIGE_SOFA)
    search = {**SOFAS, "colours_wanted": ["Grey"]}

    await _call("search_products", {"search": search}, _likes(201), services)

    resolved = pipeline.calls[-1]
    assert [p.canonical_value for p in resolved.semantic_preferences] == ["Grey"]
    assert resolved.lean.learned_colours == ("Beige",)


async def test_a_matched_search_follows_the_designer_not_the_likes() -> None:
    services, pipeline = _services(BEIGE_SOFA)
    state = _likes(201)

    await _call(
        "search_products",
        {"search": SOFAS, "match_with": [{"kind": "liked_product", "position": 1}]},
        state,
        services,
    )

    lean = pipeline.calls[-1].lean
    assert lean is None or not lean.learned_colours


async def test_more_like_this_keeps_its_own_look_not_the_likes() -> None:
    services, pipeline = _services(BEIGE_SOFA, _liked(OFF_SCREEN, "Grey", ("Classic",)))

    await _call(
        "more_like_this",
        {"which": {"kind": "presented_ordinal", "position": 1}},
        _likes(201),
        services,
    )

    assert len(pipeline.calls) == 1, "the similar search ran"
    lean = pipeline.calls[-1].lean
    assert lean is None or not lean.learned_colours


async def test_likes_that_cannot_be_read_lean_nothing() -> None:
    from app.core.exceptions import IntegrationUnavailableError

    services, pipeline = _services(error=IntegrationUnavailableError(dependency="postgres"))

    outcome = await _call("search_products", {"search": SOFAS}, _likes(201), services)

    assert pipeline.calls, "the search still ran"
    lean = pipeline.calls[-1].lean
    assert lean is None or not lean.learned
    assert "leaning_to_liked" not in outcome.facts


async def test_a_liked_sofa_set_leans_a_search_for_sofas_alone() -> None:
    """Seen live: the colour of a liked set was lost when the search was sofas only."""
    grey_set = _liked(203, "Light Grey", ("Modern",), subcategory="sofa-set")
    pipeline = FakePipeline(ids=(10, 11))
    services = agent_services(
        pipeline=pipeline,
        hydration=LikedHydration((grey_set,)),
        seating=load_seating_semantics(taxonomy=TAXONOMY),
    )

    await _call(
        "search_products", {"search": SOFAS, "with_related_types": False}, _likes(203), services
    )

    assert pipeline.calls[-1].request.single_type
    assert pipeline.calls[-1].lean.learned_colours == ("Light Grey",)


async def test_a_sort_they_ask_for_is_never_leaned() -> None:
    """Seen in review: a liked powder-blue sofa led "the cheapest" over a cheaper beige one."""
    services, pipeline = _services(BEIGE_SOFA)

    outcome = await _call(
        "search_products", {"search": {**SOFAS, "sort": "price_asc"}}, _likes(201), services
    )

    lean = pipeline.calls[-1].lean
    assert lean is None or not (lean.learned_colours or lean.learned_styles)
    assert "leaning_to_liked" not in outcome.facts


def test_what_they_said_to_avoid_is_never_leaned_to() -> None:
    lean = with_liked_look(
        None, (BEIGE_SOFA,), SOFA_REQUEST, avoid_colours=("Beige",), avoid_styles=("Modern",)
    )

    assert (lean.learned_colours, lean.learned_styles) == ((), ())


async def test_the_facts_never_name_an_avoided_colour() -> None:
    services, _ = _services(BEIGE_SOFA)
    state = remember_taste(_likes(201), avoid_colours=("Beige",))

    outcome = await _call("search_products", {"search": SOFAS}, state, services)

    assert outcome.facts["leaning_to_liked"] == {"styles": ["Modern"]}
