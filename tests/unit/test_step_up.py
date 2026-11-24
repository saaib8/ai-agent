"""A step up from one product: the same type and look, dearer within reach, and
different upward on a listed fact - said as differences, never as "better"
(docs/upselling-plan.md)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from app.agents.tools.shopping import step_up_beside
from app.agents.work import TurnWork
from app.core.config import UpsellSettings
from app.repositories.products import ProductRepository
from app.schemas.agent_state import ActiveSearchState, AgentStateV1, ProductInteractionState
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import (
    PriceConstraint,
    ProductSearchRequest,
    ProductSort,
    StepUpBasis,
)
from app.schemas.grounding import SearchExecutionGrounding, SearchOutcome
from app.schemas.product import CommerceClassification, ProductCandidate, ProductListing
from app.schemas.query import ConstraintSemantics, ConstraintStrength
from app.schemas.relaxation import StopReason
from app.schemas.resolution import ProductSearchExecutionResult, ResolvedProductReference
from app.services.grounding_builder import to_grounded_product
from app.services.step_up import PriceStep, differences, step_basis, stepped_search
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.agents.fakes import FakeReferences, agent_services, real_loop, tool_context
from tests.unit.test_repository_scoping import CONTEXT, RecordingSession, _sql

SETTINGS = UpsellSettings()
LOOP = real_loop()
SOFAS = ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa")


def _sofa(
    product_id: int,
    price: str,
    *,
    material: str | None = "Polyester",
    seats: int | None = 2,
    length: str | None = "180",
    warranty: int | None = None,
    rating: str | None = None,
    reviews: int | None = None,
) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Sofa {product_id}",
        name_arabic="كنبة",
        price_amount=Decimal(price),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(
            category="seating", subcategory="sofa", seating_capacity=seats
        ),
        dimensions=(
            NormalisedDimensions(
                status=DimensionStatus.NORMALISED,
                length_cm=Decimal(length),
                width_cm=Decimal(85),
            )
            if length
            else NormalisedDimensions(status=DimensionStatus.ABSENT)
        ),
        main_color="Grey",
        styles=("Modern",),
        listing=ProductListing(
            material=material,
            warranty_months=warranty,
            rating=Decimal(rating) if rating else None,
            review_count=reviews,
        ),
    )


THEIRS = _sofa(10, "2000")


# -- the search a step up runs ---------------------------------------------


def test_a_step_up_is_ten_percent_to_twice_dearer_with_no_fewer_seats() -> None:
    stepped = stepped_search(SOFAS, ConstraintSemantics(), THEIRS, PriceStep.HIGHER, SETTINGS)

    assert stepped is not None
    request, semantics = stepped
    assert request.price == PriceConstraint.between(Decimal(2200), Decimal(4000), "SAR")
    assert request.seating_capacity is not None
    assert request.seating_capacity.min_capacity == 2
    assert request.seating_capacity.max_capacity is None
    assert semantics.price_min is ConstraintStrength.LOCKED
    assert semantics.price_max is ConstraintStrength.LOCKED
    assert request.step_up is not None and request.step_up.from_product_id == 10


def test_their_budget_caps_a_step_up_and_can_leave_no_room() -> None:
    capped = stepped_search(
        SOFAS,
        ConstraintSemantics(),
        THEIRS,
        PriceStep.HIGHER,
        SETTINGS,
        budget_at_most=Decimal(2500),
    )
    none = stepped_search(
        SOFAS,
        ConstraintSemantics(),
        THEIRS,
        PriceStep.HIGHER,
        SETTINGS,
        budget_at_most=Decimal(2100),
    )

    assert capped is not None and capped[0].price is not None
    assert capped[0].price.max_amount == Decimal(2500)
    assert none is None


def test_cheaper_is_a_real_step_down_with_no_listed_difference() -> None:
    stepped = stepped_search(SOFAS, ConstraintSemantics(), THEIRS, PriceStep.LOWER, SETTINGS)

    assert stepped is not None
    request, _ = stepped
    assert request.price == PriceConstraint.at_most(Decimal(1800), "SAR")
    assert request.step_up is None


def test_the_basis_is_only_what_the_product_lists() -> None:
    listed = step_basis(_sofa(1, "2000", warranty=12, rating="4.1", reviews=5), SETTINGS)
    bare = step_basis(_sofa(2, "2000", material=None, seats=None, length=None), SETTINGS)

    assert listed.material_other_than == "Polyester"
    assert listed.longer_side_at_least_cm == Decimal(198)
    assert listed.longer_side_not_below_cm == Decimal(162)
    assert listed.seats_more_than == 2
    assert listed.warranty_more_than == 12
    assert listed.rated_above == Decimal("4.1")
    assert bare == StepUpBasis(from_product_id=2, min_reviews=3)


def test_odd_store_data_is_no_fact_to_step_up_from() -> None:
    odd = step_basis(_sofa(1, "2000", seats=0, warranty=-1, rating="7", reviews=9), SETTINGS)

    assert odd.seats_more_than is None
    assert odd.warranty_more_than is None
    assert odd.rated_above is None
    assert (
        stepped_search(SOFAS, ConstraintSemantics(), _sofa(2, "0"), PriceStep.HIGHER, SETTINGS)
        is None
    )


def test_step_ups_come_nearest_price_first_either_way() -> None:
    higher = stepped_search(SOFAS, ConstraintSemantics(), THEIRS, PriceStep.HIGHER, SETTINGS)
    lower = stepped_search(SOFAS, ConstraintSemantics(), THEIRS, PriceStep.LOWER, SETTINGS)

    assert higher is not None and higher[0].sort is ProductSort.PRICE_ASC
    assert lower is not None and lower[0].sort is ProductSort.PRICE_DESC


# -- what differs ------------------------------------------------------------


def test_each_upward_difference_is_said_as_what_it_is_instead_of() -> None:
    theirs = _sofa(1, "2000", warranty=12)
    other = _sofa(2, "2600", material="Velvet", seats=3, length="210", warranty=60)

    found = differences(theirs, other, SETTINGS)

    assert found == [
        {"what": "main_material", "is": "Velvet", "instead_of": "Polyester"},
        {"what": "longer_side_cm", "is": "210", "instead_of": "180"},
        {"what": "seats", "is": "3", "instead_of": "2"},
        {"what": "warranty_months", "is": "60", "instead_of": "12"},
    ]


def test_smaller_fewer_or_only_dearer_is_no_difference() -> None:
    theirs = _sofa(1, "2000", seats=3, length="200")

    assert differences(theirs, _sofa(2, "2600", seats=2, length="190"), SETTINGS) == []
    assert differences(theirs, _sofa(3, "2600", seats=3, length="210"), SETTINGS) == []


def test_a_rating_counts_only_from_enough_reviews() -> None:
    theirs = _sofa(1, "2000")

    one_review = differences(theirs, _sofa(2, "2600", rating="5", reviews=1), SETTINGS)
    many = differences(theirs, _sofa(3, "2600", rating="4.8", reviews=12), SETTINGS)

    assert one_review == []
    assert many == [{"what": "rating", "is": "4.8", "reviews": "12"}]


def test_beside_an_unrated_product_only_a_good_rating_counts() -> None:
    theirs = _sofa(1, "2000")

    poor = differences(theirs, _sofa(2, "2600", rating="1.8", reviews=5), SETTINGS)
    rated = _sofa(3, "2000", rating="4.2", reviews=4)
    above = differences(rated, _sofa(4, "2600", rating="4.5", reviews=6), SETTINGS)

    assert poor == []
    assert above == [{"what": "rating", "is": "4.5", "reviews": "6", "instead_of": "4.2"}]


# -- the database decides who qualifies -------------------------------------


async def test_a_step_up_is_any_one_upward_difference_in_sql() -> None:
    session = RecordingSession()
    request = SOFAS.model_copy(
        update={"step_up": step_basis(_sofa(1, "2000", warranty=12), SETTINGS)}
    )

    await ProductRepository(cast(AsyncSession, session)).search(request, CONTEXT, limit=5)

    sql = _sql(session.statements[0])
    assert "nullif(trim(core_product.material), '') IS NOT NULL" in sql
    assert "nullif(trim(core_product.material), '') != 'Polyester'" in sql
    assert "core_product.rating >= 4" in sql
    # Never a noticeably smaller piece, whatever else differs.
    assert ">= 162.00" in sql and " IS NULL OR " in sql
    assert "core_product.seating_capacity > 2" in sql
    assert "core_product.warranty_months > 12" in sql
    assert "core_product.review_count >= 3" in sql
    assert " OR " in sql


# -- the agent's tools -------------------------------------------------------


class _Catalog:
    """A pipeline and hydration over a fixed set of products."""

    def __init__(self, products: dict[int, ProductCandidate], found: tuple[int, ...]):
        self.products, self.found = products, found
        self.calls: list[Any] = []
        self.ids = found

    async def execute(self, resolved: Any, context: Any, **_: Any) -> Any:
        self.calls.append(resolved)
        return ProductSearchExecutionResult(
            presented_product_ids=self.found,
            grounding=SearchExecutionGrounding(
                outcome=SearchOutcome.RESULTS if self.found else SearchOutcome.ZERO_RESULTS,
                products=tuple(
                    to_grounded_product(
                        self.products[p], grounding_ref=n, presented_ordinal=n, relaxation_depth=0
                    )
                    for n, p in enumerate(self.found, start=1)
                ),
                eligible_count=len(self.found),
                ranked_count=len(self.found),
                selected_count=len(self.found),
                presented_count=len(self.found),
                exact_candidate_count=len(self.found),
                stop_reason=StopReason.EXACT_SUFFICIENT,
            ),
        )

    async def hydrate_ids(self, ids: Any, context: Any) -> tuple[ProductCandidate, ...]:
        return tuple(self.products[p] for p in ids if p in self.products)


def _services(
    found: tuple[int, ...] = (20,), refers_to: int = 10, **products: ProductCandidate
) -> Any:
    catalog = _Catalog(
        {
            10: THEIRS,
            20: _sofa(20, "2600", material="Velvet"),
            **{int(k[1:]): v for k, v in products.items()},
        },
        found,
    )
    references = FakeReferences(default=ResolvedProductReference(product_id=refers_to))
    services = agent_services(
        pipeline=cast(Any, catalog), hydration=cast(Any, catalog), references=references
    )
    return services, catalog


def _on_screen(**search: Any) -> AgentStateV1:
    return AgentStateV1(
        active_search=ActiveSearchState(request=SOFAS, revision=1, **search),
        product_interaction=ProductInteractionState(presented_product_ids=(10,)),
    )


async def _call(name: str, args: dict[str, Any], state: AgentStateV1, services: Any) -> Any:
    tool = LOOP._registry.get(name)
    assert tool is not None and tool.handler is not None
    return await tool.handler(
        tool.args.model_validate(args), tool_context(services), TurnWork.starting(state)
    )


FIRST = {"kind": "presented_ordinal", "position": 1}


async def test_more_premium_searches_a_step_up_and_says_what_differs() -> None:
    services, catalog = _services()

    outcome = await _call(
        "more_like_this", {"which": FIRST, "price": "higher"}, _on_screen(), services
    )

    request = catalog.calls[-1].request
    assert request.step_up is not None and request.price.min_amount == Decimal(2200)
    assert request.colors_any_of == ("Grey",) and request.styles_all_of == ()
    assert outcome.facts["price_step"] == "higher"
    assert outcome.facts["vs_this"] == [
        {
            "card": 1,
            "price_more": "600",
            "differs": [{"what": "main_material", "is": "Velvet", "instead_of": "Polyester"}],
        }
    ]
    assert outcome.facts["searched_for"]["a_step_up_from_a_product"] is True
    assert outcome.state.active_search.from_product


async def test_a_locked_budget_with_no_room_above_is_said() -> None:
    services, _ = _services()
    state = AgentStateV1(
        active_search=ActiveSearchState(
            request=SOFAS.model_copy(
                update={"price": PriceConstraint.at_most(Decimal(2100), "SAR")}
            ),
            semantics=ConstraintSemantics(price_max=ConstraintStrength.LOCKED),
            revision=1,
        ),
        product_interaction=ProductInteractionState(presented_product_ids=(10,)),
    )

    outcome = await _call("more_like_this", {"which": FIRST, "price": "higher"}, state, services)

    assert outcome.code == "budget_leaves_no_step_up"


async def test_details_mention_a_step_up_only_when_one_differs() -> None:
    services, _ = _services()
    plain_services, _ = _services(found=(30,), p30=_sofa(30, "2600"))

    found = await step_up_beside(services, THEIRS, _on_screen(), tool_context(services))
    none = await step_up_beside(plain_services, THEIRS, _on_screen(), tool_context(plain_services))

    assert found is not None
    assert found["price_more"] == "600"
    assert found["differs"] == [
        {"what": "main_material", "is": "Velvet", "instead_of": "Polyester"}
    ]
    assert "one line" in found["say"]
    assert none is None


async def test_the_quiet_check_keeps_the_colour_and_leans_to_the_styles() -> None:
    services, catalog = _services()

    await step_up_beside(services, THEIRS, _on_screen(), tool_context(services))

    ran = catalog.calls[-1]
    assert ran.request.sort is ProductSort.PRICE_ASC
    assert ran.request.colors_any_of == ("Grey",)
    assert ran.request.styles_all_of == ()
    leanings = tuple(p.canonical_value for p in ran.semantic_preferences)
    assert "Modern" in leanings


async def test_no_step_up_beside_a_cheapest_first_search() -> None:
    services, _ = _services()
    cheapest = AgentStateV1(
        active_search=ActiveSearchState(
            request=SOFAS.model_copy(update={"sort": ProductSort.PRICE_ASC}), revision=1
        )
    )

    assert await step_up_beside(services, THEIRS, cheapest, tool_context(services)) is None


async def test_product_details_carry_the_step_up() -> None:
    services, _ = _services()

    outcome = await _call("product_details", {"which": FIRST}, _on_screen(), services)

    assert outcome.facts["step_up"]["price_more"] == "600"


async def test_adding_a_step_up_of_a_cart_item_offers_the_swap() -> None:
    services, _ = _services()
    in_cart = AgentStateV1(
        active_search=ActiveSearchState(request=SOFAS, revision=1),
        product_interaction=ProductInteractionState(
            presented_product_ids=(10,), selected_product_ids=(10,)
        ),
    )
    stepped = await _call("more_like_this", {"which": FIRST, "price": "higher"}, in_cart, services)
    assert stepped.facts["stepping_up_from"] == {"in_cart": True, "pick": 1}

    on_the_step_up, _ = _services(refers_to=20)
    added = await _call("add_to_cart", {"items": [{"which": FIRST}]}, stepped.state, on_the_step_up)

    assert added.facts["offer_swap"] == {"take_out": "Sofa 10", "pick": 1, "its_quantity": 1}
    assert 10 in added.state.product_interaction.selected_product_ids


async def test_only_a_budget_for_its_own_kind_caps_a_step_up() -> None:
    services, catalog = _services()
    rugs = ProductSearchRequest(
        commerce_category="decor",
        commerce_subcategory="carpet",
        price=PriceConstraint.at_most(Decimal(500), "SAR"),
    )
    state = AgentStateV1(
        active_search=ActiveSearchState(
            request=rugs,
            semantics=ConstraintSemantics(price_max=ConstraintStrength.LOCKED),
            revision=1,
        ),
        product_interaction=ProductInteractionState(presented_product_ids=(10,)),
    )

    outcome = await _call("more_like_this", {"which": FIRST, "price": "higher"}, state, services)

    assert outcome.code != "budget_leaves_no_step_up"
    assert catalog.calls[-1].request.price.max_amount == Decimal(4000)


def test_a_step_up_price_is_no_budget_in_the_brief() -> None:
    from app.agents.views import _budgets

    stepped = ActiveSearchState(
        request=SOFAS.model_copy(
            update={"price": PriceConstraint.between(Decimal(2200), Decimal(4000), "SAR")}
        ),
        revision=1,
        from_product=True,
    )

    assert _budgets(AgentStateV1(active_search=stepped)) == ()


async def test_the_quiet_check_asks_nothing_about_setting_requirements_aside() -> None:
    services, catalog = _services()
    seen: list[dict[str, Any]] = []
    run = catalog.execute

    async def recording(resolved: Any, context: Any, **options: Any) -> Any:
        seen.append(options)
        return await run(resolved, context, **options)

    catalog.execute = recording  # type: ignore[method-assign]

    await step_up_beside(services, THEIRS, _on_screen(), tool_context(services))

    assert seen[-1]["explain_empty"] is False


async def test_a_refused_quiet_check_costs_only_the_mention() -> None:
    from app.core.exceptions import InvalidRequestError

    services, catalog = _services()

    async def refused(*_: Any, **__: Any) -> Any:
        raise InvalidRequestError(detail="refused", public_message="refused")

    catalog.execute = refused  # type: ignore[method-assign]

    assert await step_up_beside(services, THEIRS, _on_screen(), tool_context(services)) is None


def _after(search: ActiveSearchState) -> AgentStateV1:
    return AgentStateV1(active_search=search)


async def test_a_step_up_list_does_not_read_as_their_cheapest_first() -> None:
    services, _ = _services()
    step_list = stepped_search(SOFAS, ConstraintSemantics(), THEIRS, PriceStep.HIGHER, SETTINGS)
    assert step_list is not None and step_list[0].sort is ProductSort.PRICE_ASC
    after_step_ups = _after(ActiveSearchState(request=step_list[0], revision=1, from_product=True))

    found = await step_up_beside(services, THEIRS, after_step_ups, tool_context(services))

    assert found is not None


async def test_no_step_up_after_they_asked_for_one_cheaper() -> None:
    services, _ = _services()
    cheaper = stepped_search(SOFAS, ConstraintSemantics(), THEIRS, PriceStep.LOWER, SETTINGS)
    assert cheaper is not None
    after_cheaper = _after(ActiveSearchState(request=cheaper[0], revision=1, from_product=True))

    assert await step_up_beside(services, THEIRS, after_cheaper, tool_context(services)) is None


async def test_their_own_cheapest_first_still_holds_the_step_up_back() -> None:
    services, _ = _services()
    their_cheapest = _after(
        ActiveSearchState(
            request=SOFAS.model_copy(update={"sort": ProductSort.PRICE_ASC}), revision=1
        )
    )

    assert await step_up_beside(services, THEIRS, their_cheapest, tool_context(services)) is None


async def test_a_plain_more_like_this_list_allows_a_step_up() -> None:
    services, _ = _services()
    more_like = _after(ActiveSearchState(request=SOFAS, revision=1, from_product=True))

    assert await step_up_beside(services, THEIRS, more_like, tool_context(services)) is not None
