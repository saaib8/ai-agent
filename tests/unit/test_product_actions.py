"""Asking about a pick, comparing two, and the kinds offered beside one.

The screen-driven actions on the customer's picks, end to end through the
coordinator, with the catalog and the pipeline faked. The composer, the
companion builder, the reviewed pairings and the routing that words the reply
are the real ones: they are pure and registry-driven, so faking them would
only test the fake.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarificationReason,
    CommercialReason,
)
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    ProductInteractionState,
)
from app.schemas.agent_turn import CustomerTurnInput, CustomerTurnResult
from app.schemas.comparison import ProductComparisonResult
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import ProductSearchRequest
from app.schemas.grounding import SearchExecutionGrounding, SearchOutcome, TurnFailureCode
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.product_action import (
    CompanionAction,
    CompanionOffer,
    ComparePicksAction,
    GoesWithPickAction,
)
from app.schemas.product_reference import FocusedProduct, PickedOrdinal, PresentedOrdinal
from app.schemas.relaxation import StopReason
from app.schemas.resolution import (
    ProductSearchExecutionResult,
    ReferenceFailureReason,
    ReferenceUnresolved,
    ResolvedProductReference,
)
from app.schemas.response import ResponseGroundingView, ResponseOutcomeKind
from app.services.bundle_reference import BundleReferenceResolver
from app.services.cross_sell import CompanionSearchBuilder, companion_choices
from app.services.grounding_builder import to_grounded_product
from app.services.refinement_composer import SearchRefinementComposer
from app.services.response_view import route_response
from app.services.similar_search import SimilarSearchBuilder
from app.services.turn_coordinator import CustomerTurnCoordinator
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes
from app.taxonomy.complements import load_complements
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy

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
COMPLEMENTS = load_complements(taxonomy=TAXONOMY)

BED, SOFA, OTHER = 7, 8, 9
"""Two picks, and a product that is on screen but not picked."""

SOFAS = ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa")

# A bedroom store: nightstands, wardrobes and rugs, but no mattresses or lamps.
BEDROOM_STOCK = (
    ("tables", "nightstand"),
    ("bedroom", "wardrobe"),
    ("decor", "carpet"),
    ("seating", "sofa"),
)

WARDROBE, OTHER_BED = 10, 11
"""A wardrobe to have picked already, and a second bed to weigh against the first."""

KINDS = {
    BED: ("bedroom", "bed"),
    SOFA: ("seating", "sofa"),
    OTHER: ("seating", "sofa"),
    WARDROBE: ("bedroom", "wardrobe"),
    OTHER_BED: ("bedroom", "bed"),
}


def _product(product_id: int) -> ProductCandidate:
    category, subcategory = KINDS.get(product_id, ("tables", "nightstand"))
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Piece {product_id}",
        name_arabic="قطعة",
        price_amount=Decimal("1500"),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/p/{product_id}",
        commerce=CommerceClassification(category=category, subcategory=subcategory),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Walnut",
        styles=("Scandinavian",),
    )


class Catalog:
    """Hydration: every id is readable unless listed as gone."""

    def __init__(self, gone: tuple[int, ...] = ()) -> None:
        self.gone = gone
        self.calls: list[list[int]] = []

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[ProductCandidate, ...]:
        self.calls.append(list(product_ids))
        return tuple(_product(p) for p in product_ids if p not in self.gone)


class References:
    """Resolves picks and focus against the state it is given, like the real
    resolver, without a database. A position can be made to fail."""

    def __init__(self, fail: ReferenceFailureReason | None = None) -> None:
        self.fail = fail
        self.calls: list[Any] = []

    async def resolve(self, selector: Any, state: AgentStateV1, context: Any) -> Any:
        self.calls.append(selector)
        if self.fail is not None:
            return ReferenceUnresolved(reason=self.fail)
        interaction = state.product_interaction
        match selector:
            case PickedOrdinal(position=position):
                picked = interaction.selected_product_ids
                if position > len(picked):
                    return ReferenceUnresolved(
                        reason=ReferenceFailureReason.PICKED_ORDINAL_OUT_OF_RANGE
                    )
                return ResolvedProductReference(product_id=picked[position - 1])
            case FocusedProduct():
                if interaction.focused_product_id is None:
                    return ReferenceUnresolved(reason=ReferenceFailureReason.NO_FOCUSED_PRODUCT)
                return ResolvedProductReference(product_id=interaction.focused_product_id)
            case PresentedOrdinal(position=position):
                return ResolvedProductReference(
                    product_id=interaction.presented_product_ids[position - 1]
                )
        raise AssertionError(f"unexpected selector {selector!r}")


class ScriptedPipeline:
    """Returns the next scripted page on each run: ids, or an error."""

    def __init__(self, *pages: tuple[int, ...] | Exception) -> None:
        self.pages = list(pages)
        self.requests: list[ProductSearchRequest] = []
        self.preferences: list[tuple[str, ...]] = []
        self.limits: list[int | None] = []

    async def execute(
        self,
        resolved: Any,
        context: Any,
        *,
        dropped_constraints: Any = (),
        earlier_sizes_applied: bool = False,
        presentation_limit: int | None = None,
    ) -> ProductSearchExecutionResult:
        self.requests.append(resolved.request)
        self.preferences.append(
            tuple(p.canonical_value for p in resolved.semantic_preferences if p.canonical_value)
        )
        self.limits.append(presentation_limit)
        page = self.pages.pop(0) if self.pages else ()
        if isinstance(page, Exception):
            raise page
        return ProductSearchExecutionResult(
            presented_product_ids=page,
            grounding=SearchExecutionGrounding(
                outcome=SearchOutcome.RESULTS if page else SearchOutcome.ZERO_RESULTS,
                products=tuple(
                    to_grounded_product(
                        _product(pid), grounding_ref=n, presented_ordinal=n, relaxation_depth=0
                    )
                    for n, pid in enumerate(page, start=1)
                ),
                eligible_count=len(page),
                ranked_count=len(page),
                selected_count=len(page),
                presented_count=len(page),
                exact_candidate_count=len(page),
                stop_reason=StopReason.EXACT_SUFFICIENT,
            ),
        )


def _coordinator(
    *,
    pipeline: ScriptedPipeline | None = None,
    stock: tuple[tuple[str, str | None], ...] = BEDROOM_STOCK,
    comparison: Any = None,
    references: References | None = None,
    catalog: Catalog | None = None,
    complements: Any = COMPLEMENTS,
    capabilities: Any = None,
    decision: Any = None,
) -> tuple[CustomerTurnCoordinator, dict[str, Any]]:
    parts: dict[str, Any] = {
        "decisions": FakeDecisions(decision or _never_decided()),
        "references": references or References(),
        "comparison": comparison or FakeComparison(None),
        "pipeline": pipeline or ScriptedPipeline((30, 31, 32)),
        "catalog": catalog or Catalog(),
        "capabilities": capabilities or FakeCapabilities(pairs=stock),
    }
    coordinator = CustomerTurnCoordinator(
        parts["decisions"],
        FakeQueryUnderstanding(),  # type: ignore[arg-type]
        SearchRefinementComposer(ATTRIBUTES, DIMENSIONS, None),
        parts["references"],
        FakeRelativePrice(None),  # type: ignore[arg-type]
        parts["comparison"],
        parts["pipeline"],
        parts["catalog"],
        SimilarSearchBuilder(TAXONOMY, ATTRIBUTES),
        parts["capabilities"],
        FakeDesign(),  # type: ignore[arg-type]
        FakeDesignDiscovery(),  # type: ignore[arg-type]
        BundleReferenceResolver(TAXONOMY),
        FakeOptimizer(),  # type: ignore[arg-type]
        FakeSeatingPlanner(),  # type: ignore[arg-type]
        DIMENSIONS,
        TAXONOMY,
        complements=complements,
        companion_search=CompanionSearchBuilder(ATTRIBUTES),
    )
    return coordinator, parts


def _never_decided() -> Any:
    from app.schemas.agent_decision import CustomerAgentDecision

    return CustomerAgentDecision(action=AgentAction.ANSWER)


def _state(
    *,
    picks: tuple[int, ...] = (BED, SOFA),
    presented: tuple[int, ...] = (SOFA, OTHER),
    focus: int | None = None,
) -> AgentStateV1:
    return AgentStateV1(
        active_search=ActiveSearchState(request=SOFAS, revision=1),
        product_interaction=ProductInteractionState(
            presented_product_ids=presented,
            presented_search_revision=1,
            selected_product_ids=picks,
            focused_product_id=focus,
        ),
    )


def _turn(state: AgentStateV1, action: Any, message: str = "tap") -> CustomerTurnInput:
    return CustomerTurnInput(message=message, state=state, context=CONTEXT, product_action=action)


async def _run(coordinator: CustomerTurnCoordinator, turn: CustomerTurnInput) -> CustomerTurnResult:
    return await coordinator.run(turn)


# ── asking about a pick ═════════════════════════════════════════════════════


async def test_asking_about_a_pick_offers_the_kinds_that_go_with_it() -> None:
    """Nothing is searched: which kind comes next is theirs to say."""
    coordinator, parts = _coordinator()

    result = await _run(coordinator, _turn(_state(), GoesWithPickAction(pick=1)))

    assert parts["pipeline"].requests == []
    assert result.focus is not None and result.focus.name_english == f"Piece {BED}"
    assert result.focus.presented_ordinal is None
    assert result.grounding.search is None and result.grounding.product_detail is None
    # Every kind that goes with a bed and is stocked here - mattresses and
    # table lamps are paired with a bed but not sold by this store.
    assert [(c.subcategory, c.label) for c in result.companions] == [
        ("nightstand", "nightstands"),
        ("wardrobe", "wardrobes"),
        ("carpet", "rugs"),
    ]


async def test_the_offer_is_the_turns_question_and_the_reply_knows_the_kinds() -> None:
    coordinator, _ = _coordinator()

    result = await _run(coordinator, _turn(_state(), GoesWithPickAction(pick=1)))

    assert result.decision.commercial_reason is CommercialReason.UPSELL
    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.GOES_WITH_OFFER
    assert route.primary.goes_well_with == ("nightstands", "wardrobes", "rugs")
    # The pick's card leads the screen, so the reply frames it rather than
    # saying it has no details for it.
    assert route.primary.picked_kind == "bed"
    assert route.follow_up_allowed is False


async def test_offering_changes_only_the_focus() -> None:
    coordinator, _ = _coordinator()
    state = _state()

    result = await _run(coordinator, _turn(state, GoesWithPickAction(pick=1)))

    interaction = result.state.product_interaction
    assert interaction.focused_product_id == BED
    assert interaction.presented_product_ids == state.product_interaction.presented_product_ids
    assert interaction.selected_product_ids == (BED, SOFA), "asking picks nothing new"
    assert result.state.active_search == state.active_search


async def test_the_offer_puts_the_most_stocked_kind_first() -> None:
    coordinator, _ = _coordinator(capabilities=CountedStock(pairs=BEDROOM_STOCK))

    result = await _run(coordinator, _turn(_state(), GoesWithPickAction(pick=1)))

    assert [offer.subcategory for offer in result.companions] == [
        "carpet",
        "wardrobe",
        "nightstand",
    ]


async def test_a_kind_already_picked_is_not_offered_again() -> None:
    coordinator, _ = _coordinator()
    with_a_wardrobe = _state(picks=(BED, WARDROBE))

    result = await _run(coordinator, _turn(with_a_wardrobe, GoesWithPickAction(pick=1)))

    assert [offer.subcategory for offer in result.companions] == ["nightstand", "carpet"]


def test_the_offer_chips_are_the_kinds_and_a_way_to_say_no() -> None:
    offers = (
        CompanionOffer(category="tables", subcategory="center-table", label="centre tables"),
        CompanionOffer(category="decor", subcategory="carpet", label="rugs"),
    )

    offered = companion_choices(offers, offering=True)
    beside_results = companion_choices(offers)

    assert [chip.label for chip in offered] == ["Centre tables", "Rugs", "No thanks"]
    assert offered[0].product_action == CompanionAction(
        category="tables", subcategory="center-table"
    )
    assert offered[-1].product_action is None
    assert [chip.label for chip in beside_results] == ["Centre tables", "Rugs"]


class CountedStock(FakeCapabilities):
    """The bedroom store, holding many more rugs than wardrobes."""

    async def capabilities(self, context: Any) -> Any:
        from app.schemas.retailer import RetailerCatalogCapabilities, RetailerCatalogCapability

        counts = {"nightstand": 9, "wardrobe": 32, "carpet": 142, "sofa": 173}
        return RetailerCatalogCapabilities(
            capabilities=tuple(
                RetailerCatalogCapability(
                    commerce_category=category,
                    commerce_subcategory=subcategory,
                    active_product_count=counts[subcategory or ""],
                )
                for category, subcategory in BEDROOM_STOCK
            )
        )


# ── a pick they typed ═══════════════════════════════════════════════════════


def _typed_pick(position: int = 1) -> Any:
    from app.schemas.agent_decision import (
        CustomerAgentDecision,
        ProductInteractionIntent,
        ProductInteractionOp,
    )

    return CustomerAgentDecision(
        action=AgentAction.ANSWER,
        interaction=ProductInteractionIntent(
            op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=position)
        ),
    )


def _typed(state: AgentStateV1, message: str = "I like the first one") -> CustomerTurnInput:
    return CustomerTurnInput(message=message, state=state, context=CONTEXT)


async def test_a_typed_pick_is_offered_what_goes_with_it() -> None:
    """ "I like the first one": the same offer a tick brings, nothing searched."""
    coordinator, parts = _coordinator(decision=_typed_pick())
    beds_on_screen = _state(picks=(), presented=(BED, OTHER))

    result = await _run(coordinator, _typed(beds_on_screen))

    assert result.state.product_interaction.selected_product_ids == (BED,)
    assert result.selection_added is True
    assert parts["pipeline"].requests == []
    assert result.focus is not None and result.focus.name_english == f"Piece {BED}"
    assert [c.subcategory for c in result.companions] == ["nightstand", "wardrobe", "carpet"]
    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.GOES_WITH_OFFER
    assert route.follow_up_allowed is False


async def test_a_second_option_of_the_same_kind_is_offered_what_goes_with_it() -> None:
    """A second bed is cross-sold like the first - comparing is theirs to ask."""
    coordinator, _ = _coordinator(decision=_typed_pick())
    another_bed = _state(picks=(BED,), presented=(OTHER_BED, OTHER))

    result = await _run(coordinator, _typed(another_bed))

    assert result.state.product_interaction.selected_product_ids == (BED, OTHER_BED)
    assert result.focus is not None and result.companions


async def test_a_typed_pick_of_something_with_nothing_to_go_with_it_just_answers() -> None:
    coordinator, _ = _coordinator(decision=_typed_pick(), stock=(("seating", "sofa"),))
    beds_on_screen = _state(picks=(), presented=(BED, OTHER))

    result = await _run(coordinator, _typed(beds_on_screen))

    assert result.state.product_interaction.selected_product_ids == (BED,)
    assert result.focus is None and result.companions == ()
    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.ANSWER


async def test_with_nothing_to_offer_the_turn_is_simply_their_pick() -> None:
    coordinator, parts = _coordinator(stock=(("seating", "sofa"),))

    result = await _run(coordinator, _turn(_state(), GoesWithPickAction(pick=1)))

    assert parts["pipeline"].requests == []
    # Drawn as their pick, not as a detail offering "add to picks".
    assert result.grounding.product_detail is None
    assert result.focus is not None and result.focus.name_english == f"Piece {BED}"
    assert result.companions == ()
    assert result.state.product_interaction.focused_product_id == BED


async def test_a_type_with_no_pairings_shows_just_the_pick() -> None:
    coordinator, parts = _coordinator(complements=None)

    result = await _run(coordinator, _turn(_state(), GoesWithPickAction(pick=1)))

    assert parts["pipeline"].requests == []
    assert result.focus is not None and result.companions == ()


async def test_a_pick_that_is_not_there_is_a_question_and_changes_nothing() -> None:
    coordinator, parts = _coordinator()
    state = _state()

    result = await _run(coordinator, _turn(state, GoesWithPickAction(pick=5)))

    assert result.state == state
    assert parts["pipeline"].requests == []
    clarification = result.grounding.deterministic_clarification
    assert clarification is not None
    assert clarification.reason is BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE
    assert clarification.reference_reason is ReferenceFailureReason.PICKED_ORDINAL_OUT_OF_RANGE


async def test_a_pick_that_left_the_catalog_is_a_failure_not_a_question() -> None:
    coordinator, _ = _coordinator(
        references=References(fail=ReferenceFailureReason.PRODUCT_UNAVAILABLE)
    )

    result = await _run(coordinator, _turn(_state(), GoesWithPickAction(pick=1)))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.PRODUCT_UNAVAILABLE


async def test_no_model_is_consulted_for_a_tap() -> None:
    coordinator, parts = _coordinator()

    await _run(coordinator, _turn(_state(), GoesWithPickAction(pick=1)))

    assert parts["decisions"].inputs == []


# ── comparing two picks ═════════════════════════════════════════════════════


def _comparison() -> ProductComparisonResult:
    return ProductComparisonResult(
        products=tuple(
            to_grounded_product(
                _product(pid), grounding_ref=n, presented_ordinal=None, relaxation_depth=None
            )
            for n, pid in enumerate((SOFA, BED), start=1)
        ),
        rows=(),
    )


async def test_comparing_two_picks_compares_those_products_in_the_order_asked() -> None:
    comparison = FakeComparison(_comparison())
    coordinator, parts = _coordinator(comparison=comparison)

    result = await _run(coordinator, _turn(_state(), ComparePicksAction(picks=(2, 1))))

    assert comparison.calls == [[SOFA, BED]]
    assert result.grounding.comparison is not None
    assert result.state.product_interaction.compared_product_ids == (SOFA, BED)
    assert result.decision.action is AgentAction.COMPARE
    assert result.decision.comparison_references == (
        PickedOrdinal(position=2),
        PickedOrdinal(position=1),
    )
    assert parts["pipeline"].requests == []


def test_any_number_of_different_picks_can_be_compared() -> None:
    from pydantic import ValidationError

    assert ComparePicksAction(picks=(1, 2, 3)).picks == (1, 2, 3)
    for picks in [(1,), (1, 1), (2, 3, 2)]:
        with pytest.raises(ValidationError):
            ComparePicksAction(picks=picks)


async def test_comparing_three_picks_names_all_three() -> None:
    comparison = FakeComparison(_comparison())
    coordinator, _ = _coordinator(comparison=comparison)
    three = _state(picks=(BED, SOFA, OTHER))

    result = await _run(coordinator, _turn(three, ComparePicksAction(picks=(3, 1, 2))))

    assert comparison.calls == [[OTHER, BED, SOFA]]
    assert result.decision.comparison_references == (
        PickedOrdinal(position=3),
        PickedOrdinal(position=1),
        PickedOrdinal(position=2),
    )


async def test_a_pick_that_is_not_there_stops_the_comparison() -> None:
    comparison = FakeComparison(_comparison())
    coordinator, _ = _coordinator(comparison=comparison)

    result = await _run(coordinator, _turn(_state(), ComparePicksAction(picks=(1, 4))))

    assert comparison.calls == []
    assert result.grounding.deterministic_clarification is not None


# ── a companion chip ════════════════════════════════════════════════════════


async def test_a_chip_shows_that_companion_for_the_product_in_focus() -> None:
    coordinator, parts = _coordinator()

    result = await _run(
        coordinator,
        _turn(_state(focus=BED), CompanionAction(category="bedroom", subcategory="wardrobe")),
    )

    (request,) = parts["pipeline"].requests
    assert request.commerce_subcategory == "wardrobe"
    assert parts["pipeline"].limits == [None], "their request: a normal page"
    # Their request, not our suggestion.
    assert result.decision.commercial_reason is CommercialReason.CUSTOMER_REQUEST
    assert result.grounding.design_handoff_requested is False
    assert [c.subcategory for c in result.companions] == ["nightstand", "carpet"]


async def test_a_chip_for_a_type_the_focused_product_is_not_paired_with_is_refused() -> None:
    coordinator, parts = _coordinator()

    result = await _run(
        coordinator,
        # Nightstands go with a bed, not with the sofa in focus now.
        _turn(_state(focus=SOFA), CompanionAction(category="tables", subcategory="nightstand")),
    )

    assert parts["pipeline"].requests == []
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.REFERENCE_UNRESOLVED


async def test_a_chip_with_nothing_in_focus_asks_rather_than_guessing() -> None:
    coordinator, parts = _coordinator()

    result = await _run(
        coordinator, _turn(_state(), CompanionAction(category="decor", subcategory="carpet"))
    )

    assert parts["pipeline"].requests == []
    assert result.grounding.deterministic_clarification is not None


# ── the picks every turn reports ════════════════════════════════════════════


async def test_every_action_reports_the_picks_after_it() -> None:
    coordinator, _ = _coordinator()

    result = await _run(coordinator, _turn(_state(), GoesWithPickAction(pick=1)))

    assert result.picks is not None
    assert [(p.pick, p.name_english, p.focused) for p in result.picks] == [
        (1, f"Piece {BED}", True),
        (2, f"Piece {SOFA}", False),
    ]
    assert [p.kind for p in result.picks] == ["bed", "sofa"]


async def test_a_pick_on_screen_carries_its_card_number() -> None:
    coordinator, _ = _coordinator(pipeline=ScriptedPipeline((SOFA, 31, 32)))

    result = await _run(
        coordinator,
        _turn(_state(focus=BED), CompanionAction(category="bedroom", subcategory="wardrobe")),
    )

    assert result.picks is not None
    by_pick = {p.pick: p.presented_ordinal for p in result.picks}
    assert by_pick == {1: None, 2: 1}


async def test_a_pick_that_left_the_catalog_leaves_a_gap_not_a_renumbering() -> None:
    coordinator, _ = _coordinator(catalog=Catalog(gone=(BED,)))

    result = await _run(
        coordinator,
        _turn(_state(focus=SOFA), CompanionAction(category="decor", subcategory="carpet")),
    )

    assert result.picks is not None
    assert [p.pick for p in result.picks] == [2]


@pytest.mark.parametrize("family", [AttributeFamily.COLOR, AttributeFamily.STYLE])
async def test_companion_preferences_are_preferences_not_requirements(
    family: AttributeFamily,
) -> None:
    coordinator, parts = _coordinator()

    await _run(
        coordinator,
        _turn(_state(focus=BED), CompanionAction(category="tables", subcategory="nightstand")),
    )

    (request,) = parts["pipeline"].requests
    required = request.colors_any_of if family is AttributeFamily.COLOR else request.styles_all_of
    assert required == ()
