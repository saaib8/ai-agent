"""Stock and fit, checked before a new search's card (CLAUDE.md 14.7).

The shelf before the questions: a type the store does not carry is replaced by
the closest it does - same family first, then any - and disclosed; a kind
narrower than any approved type is looked up by name in the catalog. Whether
the store carries something is always a catalog fact, never the model's word,
and anything that cannot be checked claims nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

import pytest
from app.core.exceptions import IntegrationUnavailableError
from app.repositories.products import ProductRepository
from app.schemas.agent_state import AgentStateV1
from app.schemas.catalog_overview import CatalogOverview, SubcategoryShelf
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PriceConstraint,
    ProductSearchRequest,
    SeatingCapacityConstraint,
)
from app.schemas.grounding import DroppedConstraint, SearchExecutionGrounding
from app.schemas.product_brief import BriefMode
from app.schemas.query import (
    CommerceInterpretation,
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    ResolvedSearch,
    TypeFit,
)
from app.schemas.response import ResponseGroundingView, ResponseOutcomeKind
from app.schemas.retailer import RetailerContext
from app.services.catalog_capability import CatalogCapabilityService
from app.services.closest_type import ClosestTypeResolver
from app.services.query_understanding import _asked_kind
from app.services.response_view import route_response
from app.services.retype import as_type
from app.services.stock_fit import StockFit, StockFitCheck, StockFitOutcome, _name_words
from app.services.turn_coordinator import _disclosed, _Primary
from app.taxonomy.dimensions import DimensionRole
from app.taxonomy.seating import load_seating_semantics

from tests.unit import test_product_brief as brief_tests
from tests.unit.test_closest_type import TAXONOMY, FakeLLM

CONTEXT = RetailerContext(store_id=50)


def _shelf(category: str, subcategory: str) -> SubcategoryShelf:
    return SubcategoryShelf(
        commerce_category=category,
        commerce_subcategory=subcategory,
        active_count=3,
        price_minimum=Decimal("100"),
        price_maximum=Decimal("900"),
    )


STORE = CatalogOverview(
    store_id=50,
    shelves=(
        _shelf("seating", "sofa"),
        _shelf("seating", "lounge-chair"),
        _shelf("bedroom", "bed"),
        _shelf("fitness", "stationary-bike"),
    ),
)


@dataclass
class FakeCapabilities:
    overview_result: CatalogOverview = STORE
    error: Exception | None = None

    async def overview(self, context: RetailerContext) -> CatalogOverview:
        if self.error is not None:
            raise self.error
        return self.overview_result


@dataclass
class FakeProducts:
    named: int = 0
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    async def count_named(self, words: Any, context: RetailerContext) -> int:
        self.calls.append(tuple(words))
        return self.named


def _check(
    *picks: str | None,
    named: int = 0,
    overview: CatalogOverview = STORE,
    error: Exception | None = None,
) -> tuple[StockFitCheck, FakeLLM, FakeProducts]:
    llm = FakeLLM(*picks)
    products = FakeProducts(named=named)
    check = StockFitCheck(
        cast(CatalogCapabilityService, FakeCapabilities(overview, error)),
        cast(ProductRepository, products),
        ClosestTypeResolver(cast(Any, llm), TAXONOMY),
        load_seating_semantics(taxonomy=TAXONOMY),
    )
    return check, llm, products


def _resolved(
    category: str,
    subcategory: str | None,
    *,
    asked_kind: str | None = None,
    seats: int | None = None,
    width: int | None = None,
) -> ResolvedSearch:
    dimensions = (
        (
            DimensionConstraint(
                role=DimensionRole.OVERALL_WIDTH,
                kind=DimensionConstraintKind.MAX,
                max_cm=Decimal(width),
            ),
        )
        if width
        else ()
    )
    return ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category=category,
            commerce_subcategory=subcategory,
            price=PriceConstraint(max_amount=Decimal("5000"), currency="SAR"),
            seating_capacity=SeatingCapacityConstraint(min_capacity=seats) if seats else None,
            dimensions=dimensions,
        ),
        semantics=ConstraintSemantics(
            subcategory=ConstraintStrength.LOCKED if subcategory else None,
            price_max=ConstraintStrength.LOCKED,
            seating_min=ConstraintStrength.LOCKED if seats else None,
            dimensions=tuple(
                DimensionConstraintSemantics(role=d.role, strength=ConstraintStrength.LOCKED)
                for d in dimensions
            ),
        ),
        semantic_text="cosy",
        asked_kind=asked_kind,
    )


# ── stocked, and what they asked for ─────────────────────────────────────────


async def test_a_stocked_type_is_left_alone_and_costs_no_model_call() -> None:
    check, llm, products = _check()

    outcome = await check.check(_resolved("seating", "sofa"), CONTEXT)

    assert outcome.fit is StockFit.FITS and outcome.asked is None
    assert llm.calls == [] and products.calls == []


async def test_a_stocked_category_asked_in_broad_terms_fits() -> None:
    check, llm, _ = _check()

    outcome = await check.check(_resolved("seating", None), CONTEXT)

    assert outcome.fit is StockFit.FITS and llm.calls == []


# ── not stocked: the closest stocked type, disclosed ─────────────────────────


async def test_an_unstocked_type_becomes_its_closest_stocked_sibling() -> None:
    check, llm, _ = _check("lounge-chair")

    outcome = await check.check(_resolved("seating", "recliner", width=90), CONTEXT)

    assert outcome.fit is StockFit.SUBSTITUTED and outcome.asked == "recliner"
    request = outcome.resolved.request
    assert (request.commerce_category, request.commerce_subcategory) == (
        "seating",
        "lounge-chair",
    )
    # A size belongs to the type it was given for (CLAUDE.md 13.5).
    assert request.dimensions == () and outcome.resolved.semantics.dimensions == ()
    # ...and the size they gave is reported, never dropped quietly.
    assert [d.role for d in outcome.dropped] == [DimensionRole.OVERALL_WIDTH]
    # Everything else they asked for carries.
    assert request.price is not None and request.price.max_amount == Decimal("5000")
    assert outcome.resolved.semantic_text == "cosy"
    assert len(llm.calls) == 1


async def test_one_choice_over_every_stocked_type_its_family_first() -> None:
    """One model call, never two: siblings lead the list, the rest follow."""
    check, llm, _ = _check("lounge-chair")

    outcome = await check.check(_resolved("fitness", "treadmill"), CONTEXT)

    assert outcome.fit is StockFit.SUBSTITUTED and outcome.asked == "treadmill"
    request = outcome.resolved.request
    assert (request.commerce_category, request.commerce_subcategory) == (
        "seating",
        "lounge-chair",
    )
    assert len(llm.calls) == 1
    offered = llm.calls[0]["user_input"]
    assert offered.index("stationary-bike") < offered.index("sofa")


async def test_a_whole_family_not_stocked_looks_across_the_store() -> None:
    store = CatalogOverview(store_id=50, shelves=(_shelf("seating", "sofa"),))
    check, llm, _ = _check("sofa", overview=store)

    outcome = await check.check(_resolved("bedroom", "bed", seats=2), CONTEXT)

    assert outcome.fit is StockFit.SUBSTITUTED
    request = outcome.resolved.request
    assert (request.commerce_category, request.commerce_subcategory) == ("seating", "sofa")
    # A seat count only carries within seating.
    assert request.seating_capacity is None
    assert outcome.resolved.semantics.seating_min is None
    assert len(llm.calls) == 1


async def test_a_seat_count_carries_within_seating() -> None:
    check, _, _ = _check("sofa")

    outcome = await check.check(_resolved("seating", "sofa-set", seats=6), CONTEXT)

    capacity = outcome.resolved.request.seating_capacity
    assert capacity is not None and capacity.min_capacity == 6


async def test_several_seats_are_never_offered_a_one_seat_type() -> None:
    """ "A 3-seater recliner sofa": a lounge chair seats one, so it is not on
    the list - the choice is among types that seat several (CLAUDE.md 27.1)."""
    check, llm, _ = _check("sofa")

    outcome = await check.check(_resolved("seating", "recliner", seats=3), CONTEXT)

    assert outcome.resolved.request.commerce_subcategory == "sofa"
    capacity = outcome.resolved.request.seating_capacity
    assert capacity is not None and capacity.min_capacity == 3
    assert "lounge-chair" not in llm.calls[0]["user_input"]


async def test_one_seat_types_stay_on_offer_without_a_seat_count() -> None:
    check, llm, _ = _check("lounge-chair")

    await check.check(_resolved("seating", "recliner"), CONTEXT)

    assert "lounge-chair" in llm.calls[0]["user_input"]


async def test_nothing_close_is_not_carried_and_claims_no_substitute() -> None:
    check, llm, _ = _check(None, None)

    outcome = await check.check(_resolved("fitness", "treadmill"), CONTEXT)

    assert len(llm.calls) == 1

    assert outcome.fit is StockFit.NOT_CARRIED and outcome.asked == "treadmill"
    assert outcome.resolved.request.commerce_subcategory == "treadmill"


async def test_a_pick_outside_the_offered_types_is_refused() -> None:
    """The schema is constrained; the re-check is the real guarantee."""
    check, _, _ = _check("chandelier", "chandelier", "chandelier", "chandelier")

    outcome = await check.check(_resolved("seating", "recliner"), CONTEXT)

    assert outcome.fit is StockFit.NOT_CARRIED


# ── narrower than any type: a catalog name lookup ───────────────────────────


async def test_a_kind_named_in_the_catalog_fits() -> None:
    check, llm, products = _check(named=2)

    outcome = await check.check(_resolved("bedroom", "bed", asked_kind="bunk bed"), CONTEXT)

    assert outcome.fit is StockFit.FITS
    # Asked of the whole store: a kind filed under another type is still carried.
    assert products.calls == [("bunk", "bed")]
    assert llm.calls == []


async def test_a_kind_no_product_is_named_as_is_reported_not_found() -> None:
    check, _, _ = _check(named=0)

    outcome = await check.check(_resolved("bedroom", "bed", asked_kind="bunk bed"), CONTEXT)

    assert outcome.fit is StockFit.KIND_NOT_FOUND and outcome.asked == "bunk bed"
    # The broader type is still what is searched.
    assert outcome.resolved.request.commerce_subcategory == "bed"


async def test_a_kind_too_vague_to_look_up_claims_nothing() -> None:
    check, _, products = _check()

    outcome = await check.check(_resolved("bedroom", "bed", asked_kind="a b"), CONTEXT)

    assert outcome.fit is StockFit.FITS and products.calls == []


@pytest.mark.parametrize(
    ("kind", "words"),
    [
        ("bunk beds", ("bunk", "bed")),
        ("bean bag", ("bean", "bag")),
        ("bean bags", ("bean", "bag")),
        ("a rocking chair", ("rocking", "chair")),
        ("of a", ()),
    ],
)
def test_name_words_are_spelling_only(kind: str, words: tuple[str, ...]) -> None:
    assert _name_words(kind) == words


# ── fail open ───────────────────────────────────────────────────────────────


async def test_an_unreadable_catalog_leaves_the_search_as_it_was() -> None:
    resolved = _resolved("seating", "recliner")
    check, llm, _ = _check(error=IntegrationUnavailableError())

    outcome = await check.check(resolved, CONTEXT)

    assert outcome.fit is StockFit.FITS and outcome.resolved is resolved and llm.calls == []


# ── the type swap ───────────────────────────────────────────────────────────


def test_a_type_swap_never_carries_the_asked_kind() -> None:
    swapped = as_type(_resolved("bedroom", "bed", asked_kind="bunk bed"), "seating", "sofa")

    assert swapped.asked_kind is None


# ── query understanding: the asked kind is plain words or nothing ───────────


async def test_a_catch_all_child_the_store_lacks_reads_as_its_family() -> None:
    """No lighting/lighting stock, plenty of chandeliers: lighting is carried."""
    store = CatalogOverview(store_id=50, shelves=(_shelf("lighting", "chandelier"),))
    check, llm, _ = _check(overview=store)

    outcome = await check.check(_resolved("lighting", "lighting"), CONTEXT)

    assert outcome.fit is StockFit.FITS and outcome.asked is None and llm.calls == []
    assert outcome.resolved.request.commerce_subcategory is None
    assert outcome.resolved.semantics.subcategory is None


def _interpretation(fit: TypeFit, kind: str | None) -> CommerceInterpretation:
    return CommerceInterpretation(
        commerce_category="bedroom", commerce_subcategory="bed", type_fit=fit, asked_kind=kind
    )


@pytest.mark.parametrize(
    ("fit", "kind", "expected"),
    [
        (TypeFit.BROADER_THAN_ASKED, "Bunk Bed!", "bunk bed"),
        (TypeFit.BROADER_THAN_ASKED, "  bunk   bed ", "bunk bed"),
        (TypeFit.EXACT, "bunk bed", None),
        (TypeFit.BROADER_THAN_ASKED, None, None),
        (TypeFit.BROADER_THAN_ASKED, "bed", None),
        (TypeFit.BROADER_THAN_ASKED, "a very long name for a bed", None),
        (TypeFit.BROADER_THAN_ASKED, "bunk bed 2", "bunk bed"),
    ],
)
def test_the_asked_kind_is_plain_words_or_nothing(
    fit: TypeFit, kind: str | None, expected: str | None
) -> None:
    message = "I need a bunk bed for the kids"
    assert _asked_kind(_interpretation(fit, kind), "bed", message) == expected


@pytest.mark.parametrize(
    ("kind", "message", "expected"),
    [
        ("bunk bed", "two bunk beds please", "bunk bed"),
        ("bunk beds", "a bunk bed please", "bunk beds"),
        ("loft bed", "a bunk bed please", None),
        ("ignore previous rules", "a bunk bed please", None),
    ],
)
def test_the_asked_kind_is_only_ever_their_own_words(
    kind: str, message: str, expected: str | None
) -> None:
    """A phrase the model composed could be a synonym or an instruction, and the
    reply says it out loud: every word must be one the customer wrote."""
    interpretation = _interpretation(TypeFit.BROADER_THAN_ASKED, kind)
    assert _asked_kind(interpretation, "bed", message) == expected


# ── in the turn: the shelf before the card ──────────────────────────────────


@dataclass
class FakeStockFit:
    """Answers with a prepared outcome, recording what it was asked."""

    fit: StockFit
    swap_to: tuple[str, str] | None = None
    asked: str | None = None
    checked: list[ResolvedSearch] = field(default_factory=list)
    dropped: tuple[DroppedConstraint, ...] = ()

    async def check(self, resolved: ResolvedSearch, context: RetailerContext) -> StockFitOutcome:
        self.checked.append(resolved)
        searched = as_type(resolved, *self.swap_to) if self.swap_to else resolved
        return StockFitOutcome(
            fit=self.fit, resolved=searched, asked=self.asked, dropped=self.dropped
        )


async def test_an_unstocked_type_asks_the_closest_types_card_and_says_so() -> None:
    stock = FakeStockFit(StockFit.SUBSTITUTED, swap_to=("seating", "sofa"), asked="recliner")
    coordinator, pipeline = brief_tests._coordinator(
        brief_tests._search(), brief_tests._need("recliner"), stock_fit=stock
    )

    result = await coordinator.run(brief_tests._typed(AgentStateV1(), "I need a recliner"))

    assert pipeline.requests == []
    assert result.product_brief is not None
    assert result.state.product_brief.pending is not None
    assert result.state.product_brief.pending.base.request.commerce_subcategory == "sofa"
    assert result.unstocked_type == "recliner"
    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.PRODUCT_BRIEF
    assert route.primary.unstocked_type == "recliner"


async def test_nothing_close_asks_no_card_and_does_not_ask_the_closest_type_again() -> None:
    stock = FakeStockFit(StockFit.NOT_CARRIED, asked="treadmill")
    closest = FakeClosest()
    coordinator, pipeline = brief_tests._coordinator(
        brief_tests._search(), stock_fit=stock, closest_type=closest
    )

    result = await coordinator.run(brief_tests._typed(AgentStateV1(), "show me treadmills"))

    assert result.product_brief is None
    assert len(pipeline.requests) == 1
    assert closest.calls == 0
    assert result.unstocked_type is None


async def test_a_kind_not_found_keeps_the_broader_types_card_and_says_so() -> None:
    stock = FakeStockFit(StockFit.KIND_NOT_FOUND, asked="bunk bed")
    coordinator, pipeline = brief_tests._coordinator(brief_tests._search(), stock_fit=stock)

    result = await coordinator.run(brief_tests._typed(AgentStateV1(), "a bunk bed for my kids"))

    assert pipeline.requests == []
    assert result.product_brief is not None
    assert result.kind_not_found == "bunk bed"
    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind_not_found == "bunk bed"


async def test_declining_the_card_shows_the_substitute_and_discloses_it() -> None:
    stock = FakeStockFit(StockFit.SUBSTITUTED, swap_to=("seating", "sofa"), asked="recliner")
    coordinator, pipeline = brief_tests._coordinator(
        brief_tests._search(skip_questions=True), brief_tests._need("recliner"), stock_fit=stock
    )

    result = await coordinator.run(brief_tests._typed(AgentStateV1(), "just show me recliners"))

    assert [r.request.commerce_subcategory for r in pipeline.requests] == ["sofa"]
    assert result.unstocked_type == "recliner"
    # A substitute is its own next step: the card is not folded beside it.
    assert result.product_brief is None


async def test_a_fitting_type_runs_exactly_as_before() -> None:
    stock = FakeStockFit(StockFit.FITS)
    coordinator, pipeline = brief_tests._coordinator(brief_tests._search(), stock_fit=stock)

    result = await coordinator.run(brief_tests._typed(AgentStateV1()))

    assert len(stock.checked) == 1 and pipeline.requests == []
    assert result.product_brief is not None
    assert result.unstocked_type is None and result.kind_not_found is None


@dataclass
class FakeClosest:
    calls: int = 0

    async def closest(self, **_: Any) -> str | None:
        self.calls += 1
        return None


@pytest.mark.parametrize(
    ("unstocked", "kind", "offered"),
    [("recliner", "bunk bed", None), ("recliner", None, "sofa")],
)
def test_one_turn_gives_one_substitution_reason(
    unstocked: str, kind: str | None, offered: str | None
) -> None:
    with pytest.raises(ValueError, match="one substitution reason"):
        ResponseGroundingView(
            kind=ResponseOutcomeKind.ANSWER,
            unstocked_type=unstocked,
            kind_not_found=kind,
            offered_instead_of=offered,
        )


@pytest.mark.parametrize("words", [(), ("Bunk",), ("bunk%",), ("bunk bed",)])
async def test_the_name_lookup_takes_only_plain_lowercase_words(words: tuple[str, ...]) -> None:
    """No customer text ever shapes a LIKE pattern."""
    with pytest.raises(ValueError, match="plain lowercase words"):
        await ProductRepository(cast(Any, None)).count_named(words, CONTEXT)


class CapturingPipeline(brief_tests.Pipeline):
    """The harness's pipeline, also recording what each run was told."""

    def __init__(self) -> None:
        super().__init__()
        self.kwargs: list[dict[str, Any]] = []

    async def execute(self, resolved: Any, context: Any, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        return await super().execute(resolved, context, **kwargs)


async def test_a_size_given_for_the_unstocked_type_is_reported_beside_the_substitute() -> None:
    """A size belongs to its type (13.5): dropped on the swap, and said so."""
    dropped = (DroppedConstraint(role=DimensionRole.OVERALL_WIDTH),)
    stock = FakeStockFit(StockFit.SUBSTITUTED, swap_to=("seating", "sofa"), asked="recliner")
    stock.dropped = dropped
    pipeline = CapturingPipeline()
    coordinator, _ = brief_tests._coordinator(
        brief_tests._search(skip_questions=True),
        brief_tests._need("recliner"),
        stock_fit=stock,
        pipeline=pipeline,
    )

    await coordinator.run(brief_tests._typed(AgentStateV1(), "a recliner under 90 cm wide"))

    assert pipeline.kwargs[0]["dropped_constraints"] == dropped


async def test_a_typed_answer_to_the_substitutes_card_is_searched_not_asked_again() -> None:
    """ "Under 3000" typed to the sofa card shown for a recliner request."""
    stock = FakeStockFit(StockFit.SUBSTITUTED, swap_to=("seating", "sofa"), asked="recliner")
    asking, _ = brief_tests._coordinator(
        brief_tests._search(), brief_tests._need("recliner"), stock_fit=stock
    )
    carded = await asking.run(brief_tests._typed(AgentStateV1(), "I need a recliner"))
    assert carded.product_brief is not None

    budget = PriceConstraint(max_amount=Decimal("3000"), currency="SAR")
    answering, pipeline = brief_tests._coordinator(
        brief_tests._search(), brief_tests._need("recliner", price=budget), stock_fit=stock
    )
    result = await answering.run(brief_tests._typed(carded.state, "under 3000 SAR"))

    assert [r.request.commerce_subcategory for r in pipeline.requests] == ["sofa"]
    assert result.product_brief is None or result.product_brief.mode is not BriefMode.ASK
    # Said once already, beside the card: not said again.
    assert result.unstocked_type is None


def test_an_empty_substitute_search_still_says_the_type_is_not_carried() -> None:
    """ "A recliner under 500": no lounge chairs under 500 either - the reply
    still says recliners are not carried."""
    empty = _Primary(
        state=AgentStateV1(),
        search=SearchExecutionGrounding.model_construct(products=()),
    )
    fit = StockFitOutcome(
        fit=StockFit.SUBSTITUTED, resolved=_resolved("seating", "sofa"), asked="recliner"
    )

    assert _disclosed(empty, fit).unstocked_type == "recliner"


def test_a_kind_not_found_beside_an_empty_search_claims_nothing() -> None:
    empty = _Primary(
        state=AgentStateV1(),
        search=SearchExecutionGrounding.model_construct(products=()),
    )
    fit = StockFitOutcome(
        fit=StockFit.KIND_NOT_FOUND, resolved=_resolved("bedroom", "bed"), asked="bunk bed"
    )

    assert _disclosed(empty, fit).kind_not_found is None


async def test_the_overview_is_read_once_per_request() -> None:
    """The stock check, the seat ceiling and the closest type share one scan."""

    class CountingRepository:
        def __init__(self) -> None:
            self.reads = 0

        async def catalog_overview(self, context: RetailerContext) -> tuple[Any, ...]:
            self.reads += 1
            return ()

    repository = CountingRepository()
    service = CatalogCapabilityService(cast(Any, repository), TAXONOMY, cast(Any, None))

    first = await service.overview(CONTEXT)
    second = await service.overview(CONTEXT)

    assert first is second and repository.reads == 1


@pytest.mark.parametrize("header", ["model_calls_header", "turn_action_header"])
def test_the_debug_headers_are_refused_in_production(header: str) -> None:
    from tests.conftest import build_settings

    with pytest.raises(ValueError, match="debug headers"):
        build_settings(environment="prod", observability={header: "X-Debug"})


def test_each_model_call_and_the_turn_action_are_traced() -> None:
    from app.core.request_trace import (
        count_model_call,
        record_turn_action,
        start_request_trace,
    )

    trace = start_request_trace()
    count_model_call()
    count_model_call()
    record_turn_action("search")

    assert trace.model_calls == 2 and trace.turn_action == "search"


async def test_a_kind_they_insisted_on_is_not_carried_rather_than_swapped() -> None:
    """ "Only recliners, nothing else": the closest thing is not what they asked
    for, and they said so - no substitute, no model call."""
    check, llm, _ = _check("lounge-chair")
    insisted = _resolved("seating", "recliner").model_copy(update={"kind_required": True})

    outcome = await check.check(insisted, CONTEXT)

    assert outcome.fit is StockFit.NOT_CARRIED and llm.calls == []


@pytest.mark.parametrize(
    ("environment", "expected"),
    [("local", True), ("stage", True), ("test", False), ("prod", False)],
)
def test_the_agent_features_default_on_in_local_and_stage_only(
    environment: str, expected: bool
) -> None:
    """Built from explicit values only: a developer's `.env` never decides it."""
    from tests.conftest import build_settings

    agent = build_settings(environment=environment).customer_agent

    assert agent.stock_fit_check is expected
    assert agent.agent_loop is expected
    assert agent.speculative_interpretation is expected
    # Measured and rejected as a default (plan 11): it skipped room questions.
    assert agent.decision_reasoning_effort is None


def test_an_explicit_setting_wins_over_the_environment_default() -> None:
    from tests.conftest import build_settings

    agent = build_settings(environment="local", customer_agent={"agent_loop": False}).customer_agent

    assert agent.agent_loop is False
    assert agent.stock_fit_check is True
