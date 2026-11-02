"""Best rated and best selling: orders only when asked, and best sellers only
ever named positively."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from app.agents.tools.base import ToolOutcome
from app.agents.tools.shopping import sorted_card_facts
from app.agents.work import TurnWork
from app.core.config import ProductDataSettings
from app.db.tables import core_product
from app.repositories.products import _order_by
from app.schemas.discovery import ProductSearchRequest, ProductSort
from app.schemas.product import EligibleProduct, ProductListing
from app.schemas.query import ConstraintStrength, ResolvedSearch, SemanticPreference
from app.schemas.relaxation import RelaxedCandidate
from app.services.best_sellers import BestSellerService
from app.services.semantic_ranking import SemanticRankingService
from app.taxonomy.attributes import AttributeFamily

from tests.unit.agents.fakes import _state, agent_services, real_loop, tool_context
from tests.unit.test_repository_scoping import (
    CONTEXT,
    RecordingSession,
    _repository,
    _sql,
)

LOOP = real_loop()
SOFAS = ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa")


def _card(
    product_id: int,
    *,
    rating: str | None = None,
    sold: int | None = None,
    colour: str | None = None,
) -> RelaxedCandidate:
    return RelaxedCandidate(
        product=EligibleProduct(
            product_id=product_id,
            subcategory="sofa",
            price_amount=Decimal("1000"),
            main_color=colour,
            weighted_rating=Decimal(rating) if rating else None,
            sold=sold,
        ),
        relaxation_depth=0,
    )


async def _order(sort: ProductSort, cards: list[RelaxedCandidate], **resolved: Any) -> list[int]:
    request = SOFAS.model_copy(update={"sort": sort})
    ranked = await SemanticRankingService(None, None).rank(
        ResolvedSearch(request=request, **resolved), cards, CONTEXT, namespace="store-50"
    )
    return list(ranked.product_ids)


# ── the order ═══════════════════════════════════════════════════════════════


async def test_best_rated_orders_by_the_weighted_rating_unrated_last() -> None:
    cards = [_card(1), _card(2, rating="4.85"), _card(3, rating="4.94"), _card(4, rating="4.24")]

    assert await _order(ProductSort.BEST_RATED, cards) == [3, 2, 4, 1]


async def test_best_selling_orders_by_sales_unsold_last() -> None:
    cards = [_card(1), _card(2, sold=13), _card(3, sold=490), _card(4, sold=2)]

    assert await _order(ProductSort.BEST_SELLING, cards) == [3, 2, 4, 1]


async def test_a_stated_colour_still_leads_the_best_rated() -> None:
    """ "The best rated beige sofas": the beige ones by rating, then the rest."""
    beige = SemanticPreference(
        family=AttributeFamily.COLOR,
        raw_value="beige",
        canonical_value="Beige",
        strength=ConstraintStrength.PREFERRED,
    )
    cards = [
        _card(1, rating="4.9", colour="Grey"),
        _card(2, rating="4.5", colour="Beige"),
        _card(3, rating="4.8", colour="Beige"),
    ]

    order = await _order(ProductSort.BEST_RATED, cards, semantic_preferences=(beige,))

    assert order == [3, 2, 1]


async def test_a_normal_search_ignores_ratings_and_sales() -> None:
    cards = [_card(1, rating="3.0", sold=1), _card(2, rating="5.0", sold=400)]

    assert await _order(ProductSort.DEFAULT, cards) == [1, 2]


def test_best_rated_sql_weighs_by_the_stores_own_average_with_unrated_last() -> None:
    session = RecordingSession()
    statement = (
        _repository(session)
        ._eligible_select((core_product.c.id,), SOFAS, CONTEXT, (), None)
        .order_by(*_order_by(ProductSort.BEST_RATED, CONTEXT, 3))
    )
    sql = _sql(statement)

    assert "avg(core_product.rating)" in sql
    assert f"core_product.store_id = {CONTEXT.store_id}" in sql
    assert "DESC NULLS LAST" in sql


# ── best sellers: positive only ═════════════════════════════════════════════


class _Sellers:
    def __init__(self, best: set[int]) -> None:
        self.best = best

    async def among(self, ids: Any, context: Any) -> frozenset[int]:
        return frozenset(i for i in ids if i in self.best)

    async def of_kinds(self, kinds: Any, context: Any) -> tuple[int, ...]:
        return tuple(sorted(self.best))


async def _ask(among: str, best: set[int], **args: Any) -> Any:
    tool = LOOP._registry.get("best_sellers")
    assert tool is not None and tool.handler is not None
    services = agent_services(best_sellers=_Sellers(best))
    return await tool.handler(
        tool.args.model_validate({"among": among, **args}),
        tool_context(services),
        TurnWork.starting(_state(presented=(10, 11, 12), selected=(42,))),
    )


async def test_only_the_best_sellers_on_screen_are_named() -> None:
    outcome = await _ask("cards_on_screen", {11})

    assert outcome.facts == {"best_sellers": [{"card": 2}]}


async def test_none_among_them_says_nothing_about_sales() -> None:
    outcome = await _ask("cards_on_screen", set())

    assert outcome.facts["best_sellers"] == []
    assert "never that a product sells less" in outcome.facts["say"]


async def test_a_kind_that_is_not_approved_is_refused() -> None:
    outcome = await _ask("kinds", set(), kinds=["luxury-couch"])

    assert outcome.code == "not_approved_kinds"


async def test_switched_off_names_none() -> None:
    service = BestSellerService(
        _repository(RecordingSession()), ProductDataSettings(best_sellers_enabled=False)
    )

    assert await service.among([1, 2], CONTEXT) == frozenset()
    assert await service.of_kinds(["sofa"], CONTEXT) == ()


def test_the_sales_number_stays_in_the_ranking_shape_only() -> None:
    from app.schemas.agent_brief import BriefProduct
    from app.schemas.grounding import GroundedProduct
    from app.schemas.product import ProductCandidate

    for shape in (BriefProduct, GroundedProduct, ProductCandidate):
        assert not {"sold", "sold_quantity", "weighted_rating"} & set(shape.model_fields)


def _run(sort: ProductSort, listings: list[ProductListing]) -> Any:
    state = _state(presented=tuple(range(10, 10 + len(listings))))
    active = state.active_search.model_copy(  # type: ignore[union-attr]
        update={"request": SOFAS.model_copy(update={"sort": sort})}
    )
    return SimpleNamespace(
        state=state.model_copy(update={"active_search": active}),
        grounding=SimpleNamespace(products=[SimpleNamespace(listing=x) for x in listings]),
    )


async def test_a_best_rated_search_gives_each_rated_card_its_rating() -> None:
    listings = [
        ProductListing(rating=Decimal("5.00"), review_count=7),
        ProductListing(),
    ]
    outcome = ToolOutcome(facts={"products": [{"card": 1}, {"card": 2}]})

    done = await sorted_card_facts(
        outcome, _run(ProductSort.BEST_RATED, listings), agent_services(), tool_context()
    )

    assert done.facts["products"] == [
        {"card": 1, "rating": {"stars": "5", "reviews": 7}},
        {"card": 2},
    ]


async def test_a_normal_search_gets_no_ratings_or_sales_on_its_cards() -> None:
    listings = [ProductListing(rating=Decimal("5.00"), review_count=7)]
    outcome = ToolOutcome(facts={"products": [{"card": 1}]})

    done = await sorted_card_facts(
        outcome,
        _run(ProductSort.DEFAULT, listings),
        agent_services(best_sellers=_Sellers({10})),
        tool_context(),
    )

    assert done.facts["products"] == [{"card": 1}]


async def test_a_best_selling_search_marks_only_its_best_sellers() -> None:
    listings = [ProductListing(), ProductListing()]
    outcome = ToolOutcome(facts={"products": [{"card": 1}, {"card": 2}]})

    done = await sorted_card_facts(
        outcome,
        _run(ProductSort.BEST_SELLING, listings),
        agent_services(best_sellers=_Sellers({11})),
        tool_context(),
    )

    assert done.facts["products"] == [{"card": 1}, {"card": 2, "best_seller": True}]


# ── review fixes ════════════════════════════════════════════════════════════


async def test_an_exact_match_leads_whatever_the_sort_says_of_widened_ones() -> None:
    """ "The most expensive under 2000": the sofas a widened budget let in come
    after those that met it, however dear."""

    def at(pid: int, price: str, depth: int) -> RelaxedCandidate:
        return RelaxedCandidate(
            product=EligibleProduct(product_id=pid, price_amount=Decimal(price)),
            relaxation_depth=depth,
        )

    cards = [at(1, "1900", 0), at(2, "1500", 0), at(3, "2150", 1), at(4, "2200", 1)]

    assert await _order(ProductSort.PRICE_DESC, cards) == [1, 2, 4, 3]


class _PoolRepository:
    def __init__(self) -> None:
        self.requests: list[ProductSearchRequest] = []

    async def search_eligible_pool(self, request: Any, context: Any, **_: Any) -> Any:
        self.requests.append(request)
        return [EligibleProduct(product_id=1, price_amount=Decimal("100"), sold=490)]


async def test_with_best_sellers_switched_off_sales_order_nothing() -> None:
    from typing import cast

    from app.core.config import DiscoverySettings
    from app.repositories.products import ProductRepository
    from app.services.discovery import ProductDiscoveryService
    from app.taxonomy.attributes import load_catalog_attributes
    from app.taxonomy.dimensions import load_dimension_semantics
    from app.taxonomy.registry import load_taxonomy

    taxonomy = load_taxonomy()
    repository = _PoolRepository()
    discovery = ProductDiscoveryService(
        cast(ProductRepository, repository),
        taxonomy,
        DiscoverySettings(),
        load_catalog_attributes(),
        load_dimension_semantics(taxonomy=taxonomy),
        best_sellers_enabled=False,
    )

    pool = await discovery.eligible_pool(
        SOFAS.model_copy(update={"sort": ProductSort.BEST_SELLING}), CONTEXT
    )

    assert repository.requests[0].sort is ProductSort.DEFAULT
    assert pool[0].sold is None


async def test_not_this_one_keeps_a_best_rated_pages_ratings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Not this one" on a best-rated page returns a page that still carries
    each card's rating - the same as search and "show me more"."""
    from dataclasses import replace

    import app.agents.tools.shopping as shopping

    run = _run(ProductSort.BEST_RATED, [ProductListing(rating=Decimal("4.50"), review_count=2)])

    class _Search:
        async def again_excluding(self, ids: Any, state: Any, retailer: Any) -> Any:
            return run

    monkeypatch.setattr(
        shopping, "search_outcome", lambda r, extra: ToolOutcome(facts={"products": [{"card": 1}]})
    )
    tool = LOOP._registry.get("leave_out_product")
    assert tool is not None and tool.handler is not None
    services = replace(agent_services(), search=_Search())  # type: ignore[arg-type]

    outcome = await tool.handler(
        tool.args.model_validate({"which": {"kind": "presented_ordinal", "position": 1}}),
        tool_context(services),
        TurnWork.starting(_state(presented=(10, 11, 12))),
    )

    assert outcome.facts["products"] == [{"card": 1, "rating": {"stars": "4.5", "reviews": 2}}]
