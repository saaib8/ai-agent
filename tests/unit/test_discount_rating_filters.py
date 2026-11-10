"""Discounts and ratings the customer asks for are filters the catalog answers.

"Sofas on sale" shows only sofas with a checked discount - the same check every
card's discount passes - and "the biggest discounts" orders by the share off.
"Rated 4 and up" shows only products the store rates at least 4. A rating is
said with its count wherever the store gives one, and as "rated 4.5" where it
gives none; a rating from a count of 0 is from nobody (CLAUDE.md 37).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from app.agents.facts import searched_for
from app.core.config import RelaxationSettings
from app.core.exceptions import LLMResponseInvalidError
from app.integrations.llm import StructuredLLMClient
from app.repositories.products import ProductRepository, _rating
from app.schemas.agent_state import ActiveSearchState
from app.schemas.discovery import ProductSearchRequest, ProductSort
from app.schemas.language import ReplyLanguage
from app.schemas.product import EligibleProduct, ProductListing
from app.schemas.query import CommerceInterpretation, ConstraintSemantics, ResolvedSearch
from app.schemas.relaxation import SetAsideField
from app.schemas.retailer import RetailerContext
from app.services.controlled_search import ControlledRelaxationService
from app.services.discovery import ProductDiscoveryService
from app.services.product_display import rating_words
from app.services.query_understanding import QueryUnderstandingService
from app.services.relaxation import RelaxationPlanner
from app.services.semantic_ranking import _SORT_KEYS
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_query_understanding import ATTRIBUTES, DIMENSIONS, TAXONOMY, FakeLLMClient
from tests.unit.test_repository_scoping import CONTEXT, RecordingSession, _sql


def _resolved(**asked: Any) -> Any:
    service = QueryUnderstandingService(
        cast(StructuredLLMClient, FakeLLMClient()), TAXONOMY, ATTRIBUTES, DIMENSIONS
    )
    return service.resolve(
        CommerceInterpretation(commerce_category="seating", commerce_subcategory="sofa", **asked)
    )


def _sofas(**asked: Any) -> ProductSearchRequest:
    return ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa", **asked)


async def _pool_sql(request: ProductSearchRequest) -> str:
    session = RecordingSession()
    await ProductRepository(cast(AsyncSession, session)).search(
        request, CONTEXT, limit=5, discount_max_share=0.8
    )
    return _sql(session.statements[0])


# ── the agent's words become the request ────────────────────────────────────


def test_on_sale_and_a_lowest_rating_reach_the_request() -> None:
    outcome = _resolved(on_discount=True, rating_at_least="4", sort="biggest_discount")

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.on_discount is True
    assert outcome.request.rating_at_least == Decimal(4)
    assert outcome.request.sort is ProductSort.BIGGEST_DISCOUNT


def test_nothing_asked_is_no_filter() -> None:
    request = _resolved().request

    assert request.on_discount is False and request.rating_at_least is None


def test_a_rating_of_nothing_is_refused_not_guessed() -> None:
    with pytest.raises(LLMResponseInvalidError):
        _resolved(rating_at_least="0")


def test_a_search_without_them_is_stored_as_before() -> None:
    stored = _sofas().model_dump(mode="json")

    assert "on_discount" not in stored and "rating_at_least" not in stored


# ── the database answers them ───────────────────────────────────────────────


async def test_on_sale_is_the_cards_discount_check_in_sql() -> None:
    sql = await _pool_sql(_sofas(on_discount=True))

    # The old price above a positive current price, at least 1% and at most
    # the configured share off - the same rule `discount_of` applies.
    assert "core_product.regular_price > core_product.price_amount" in sql
    assert "core_product.price_amount > 0" in sql
    assert "* 100 >= core_product.regular_price" in sql
    assert "core_product.regular_price * 0.8" in sql


async def test_a_lowest_rating_needs_a_rating_from_someone() -> None:
    sql = await _pool_sql(_sofas(rating_at_least=Decimal(4)))

    assert "core_product.rating >= 4" in sql
    assert "core_product.rating IS NOT NULL" in sql
    assert "coalesce(core_product.review_count, 1) > 0" in sql


async def test_without_them_neither_is_asked_about() -> None:
    sql = await _pool_sql(_sofas())

    assert "core_product.rating >=" not in sql
    assert "regular_price >" not in sql


async def test_the_biggest_discount_orders_by_the_share_off() -> None:
    sql = await _pool_sql(_sofas(sort=ProductSort.BIGGEST_DISCOUNT))

    order = sql[sql.index("ORDER BY") :]
    assert "regular_price" in order and "DESC NULLS LAST" in order


def test_ranking_keeps_the_biggest_discount_first_and_undiscounted_last() -> None:
    key = _SORT_KEYS[ProductSort.BIGGEST_DISCOUNT]

    def candidate(share: str | None) -> Any:
        product = EligibleProduct(
            product_id=1,
            price_amount=Decimal(100),
            discount_share=None if share is None else Decimal(share),
        )
        return type("C", (), {"product": product})()

    ordered = sorted([candidate(None), candidate("0.1"), candidate("0.3")], key=key)

    assert [c.product.discount_share for c in ordered] == [Decimal("0.3"), Decimal("0.1"), None]


# ── reported, and set aside when nothing meets them ─────────────────────────


def test_the_search_summary_says_what_ran() -> None:
    active = ActiveSearchState(
        request=_sofas(on_discount=True, rating_at_least=Decimal("4.5")),
        semantics=ConstraintSemantics(),
        revision=1,
    )

    asked = searched_for(active)
    assert asked["on_discount"] is True and asked["rating_at_least"] == Decimal("4.5")


class _Catalog:
    """Sofas, none on sale and none rated."""

    async def eligible_pool(
        self, request: ProductSearchRequest, context: RetailerContext
    ) -> tuple[EligibleProduct, ...]:
        if request.on_discount or request.rating_at_least is not None:
            return ()
        return tuple(EligibleProduct(product_id=n, price_amount=Decimal(900)) for n in (1, 2, 3))


@pytest.mark.parametrize(
    ("asked", "field"),
    [
        ({"on_discount": True}, SetAsideField.DISCOUNT),
        ({"rating_at_least": Decimal(4)}, SetAsideField.RATING),
    ],
)
async def test_nothing_met_offers_setting_it_aside(
    asked: dict[str, Any], field: SetAsideField
) -> None:
    settings = RelaxationSettings()
    service = ControlledRelaxationService(
        cast(ProductDiscoveryService, _Catalog()), RelaxationPlanner(settings), settings
    )
    resolved = ResolvedSearch(request=_sofas(**asked), semantics=ConstraintSemantics())

    result = await service.search(resolved, CONTEXT, explain_empty=True)

    assert result.candidates == ()
    assert [(o.field, o.eligible_count) for o in result.set_aside] == [(field, 3)]


# ── a rating, said as the store gives it ────────────────────────────────────


def test_a_rating_is_read_with_its_count_alone_or_not_at_all() -> None:
    assert _rating(Decimal("4.5"), 12) == {"rating": Decimal("4.5"), "review_count": 12}
    assert _rating(Decimal("4.5"), None) == {"rating": Decimal("4.5")}
    assert _rating(Decimal("4.5"), 0) == {}
    assert _rating(None, 1) == {}


def test_a_rating_with_no_count_is_said_without_one() -> None:
    assert rating_words(Decimal("4.5"), 12, ReplyLanguage.EN) == "4.5 from 12 reviews"
    assert rating_words(Decimal("4.5"), None, ReplyLanguage.EN) == "rated 4.5"
    assert rating_words(Decimal("4.5"), None, ReplyLanguage.AR) == "التقييم 4.5"
    assert ProductListing(rating=Decimal("4.5")).review_count is None


# ── the sale badge and the "on sale" search always agree ─────────────────────

# (old price, current price): a plain 25% off, exactly 1% off, half a percent
# off (too small to be a sale), 95% off (a store's typo), exactly the largest
# share allowed, just past it, no reduction, no old price, and a price of 0.
PRICES = [
    (1000, 750),
    (1000, 990),
    (1000, 995),
    (10000, 500),
    (1000, 200),
    (1000, 199),
    (1000, 1000),
    (None, 500),
    (1000, 0),
]


def test_the_sale_badge_and_the_on_sale_search_agree() -> None:
    """Two pieces of code decide whether a product is on sale: the card's
    badge (`discount_of`) and the "on sale" search (`_discount_share`, in SQL).
    Run on the same prices, they must give the same answer - so a product the
    search calls on sale always shows its sale badge, and the other way round."""
    from app.db.tables import core_product
    from app.repositories.products import _discount_share
    from app.services.product_value import discount_of
    from sqlalchemy import create_engine, select, text

    max_share = 0.8
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "create table core_product "
                "(id integer primary key, regular_price numeric, price_amount numeric)"
            )
        )
        for n, (old, price) in enumerate(PRICES):
            connection.execute(
                text("insert into core_product values (:id, :old, :price)"),
                {"id": n, "old": old, "price": price},
            )
        searched = dict(
            connection.execute(
                select(core_product.c.id, _discount_share(max_share)).order_by(core_product.c.id)
            ).all()
        )

    for n, (old, price) in enumerate(PRICES):
        listing = ProductListing(regular_price=None if old is None else Decimal(old))
        badge = discount_of(Decimal(price), listing, max_share)
        on_sale = searched[n]
        assert (badge is None) == (on_sale is None), (old, price)
        if badge is not None:
            assert badge.percent == int(Decimal(str(on_sale)) * 100), (old, price)
