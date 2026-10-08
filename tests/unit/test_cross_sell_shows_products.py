"""A pick answered with products that go with it (CLAUDE.md 10.4).

With `cross_sell_shows_products` on, a pick - ticked, typed, or opened from
its "Goes with" button - shows one kind of product beneath it at once: the
kind the design specialist chooses from what the store stocks, leaning towards
the colours and styles the customer has expressed. The reviewed pairings are
the fallback and stay as chips. Off, a pick is offered the kinds, as before.
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import CatalogUnavailableError, LLMResponseInvalidError
from app.schemas.agent_state import AgentStateV1, CustomerPreferenceState
from app.schemas.design import DesignCategoryNeed, DesignPriority, InteriorDesignResult
from app.schemas.product_action import GoesWithPickAction
from app.schemas.query import ConstraintStrength, SemanticPreference
from app.schemas.response import ResponseGroundingView, ResponseOutcomeKind
from app.services.bundle_reference import BundleReferenceResolver
from app.services.cross_sell import CompanionSearchBuilder
from app.services.design_discovery import DesignDiscoveryService
from app.services.refinement_composer import SearchRefinementComposer
from app.services.response_view import route_response
from app.services.similar_search import SimilarSearchBuilder
from app.services.turn_coordinator import CustomerTurnCoordinator
from app.taxonomy.attributes import AttributeFamily

from tests.unit.test_product_actions import (
    ATTRIBUTES,
    BED,
    BEDROOM_STOCK,
    COMPLEMENTS,
    DIMENSIONS,
    OTHER,
    TAXONOMY,
    Catalog,
    References,
    ScriptedPipeline,
    _state,
    _turn,
    _typed,
    _typed_pick,
)
from tests.unit.test_turn_coordinator import (
    FakeCapabilities,
    FakeComparison,
    FakeDecisions,
    FakeDesign,
    FakeOptimizer,
    FakeQueryUnderstanding,
    FakeRelativePrice,
    FakeSeatingPlanner,
)

RUGS = DesignCategoryNeed(
    commerce_category="decor",
    commerce_subcategory="carpet",
    priority=DesignPriority.RECOMMENDED,
    semantic_intent="soft and grounding",
)
WARDROBES = DesignCategoryNeed(
    commerce_category="bedroom",
    commerce_subcategory="wardrobe",
    priority=DesignPriority.RECOMMENDED,
)


def _engine(
    *,
    design: FakeDesign | None = None,
    pipeline: ScriptedPipeline | None = None,
    stock: tuple[tuple[str, str | None], ...] = BEDROOM_STOCK,
    complements: Any = COMPLEMENTS,
    decision: Any = None,
    shows_products: bool = True,
) -> tuple[CustomerTurnCoordinator, FakeDesign, ScriptedPipeline]:
    from app.schemas.agent_decision import AgentAction, CustomerAgentDecision

    design = design or FakeDesign(InteriorDesignResult(needs=(RUGS,)))
    pipeline = pipeline or ScriptedPipeline((30, 31, 32))
    coordinator = CustomerTurnCoordinator(
        FakeDecisions(decision or CustomerAgentDecision(action=AgentAction.ANSWER)),
        FakeQueryUnderstanding(),  # type: ignore[arg-type]
        SearchRefinementComposer(ATTRIBUTES, DIMENSIONS, None),
        References(),  # type: ignore[arg-type]
        FakeRelativePrice(None),  # type: ignore[arg-type]
        FakeComparison(None),  # type: ignore[arg-type]
        pipeline,  # type: ignore[arg-type]
        Catalog(),  # type: ignore[arg-type]
        SimilarSearchBuilder(TAXONOMY, ATTRIBUTES),
        FakeCapabilities(pairs=stock),  # type: ignore[arg-type]
        design,  # type: ignore[arg-type]
        DesignDiscoveryService(pipeline, TAXONOMY),  # type: ignore[arg-type]
        BundleReferenceResolver(TAXONOMY),
        FakeOptimizer(),  # type: ignore[arg-type]
        FakeSeatingPlanner(),  # type: ignore[arg-type]
        DIMENSIONS,
        TAXONOMY,
        complements=complements,
        companion_search=CompanionSearchBuilder(ATTRIBUTES),
        cross_sell_shows_products=shows_products,
    )
    return coordinator, design, pipeline


def _liking(*preferences: tuple[AttributeFamily, str]) -> CustomerPreferenceState:
    return CustomerPreferenceState(
        semantic_preferences=tuple(
            SemanticPreference(
                family=family,
                raw_value=value,
                canonical_value=value,
                strength=ConstraintStrength.PREFERRED,
            )
            for family, value in preferences
        )
    )


def _with_taste(state: AgentStateV1, preferences: CustomerPreferenceState) -> AgentStateV1:
    return state.model_copy(update={"customer_preferences": preferences})


def _values(preferences: Any) -> list[str | None]:
    return [p.canonical_value for p in preferences]


async def test_a_pick_shows_the_kind_the_designer_suggests_beneath_it() -> None:
    coordinator, design, pipeline = _engine()

    result = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    assert len(design.requests) == 1
    assert [(r.commerce_category, r.commerce_subcategory) for r in pipeline.requests] == [
        ("decor", "carpet")
    ]
    assert result.focus is not None and result.focus.name_english == f"Piece {BED}"
    assert result.grounding.search is not None and len(result.grounding.search.products) == 3
    # The other reviewed kinds stay as chips; the one shown is not repeated.
    assert [c.subcategory for c in result.companions] == ["nightstand", "wardrobe"]
    assert result.state.product_interaction.focused_product_id == BED
    assert result.state.active_search is not None


async def test_the_cards_are_worded_as_a_suggestion_beside_the_pick() -> None:
    coordinator, _, _ = _engine()

    result = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.SEARCH_RESULTS
    assert route.primary.search_was_suggested is True
    assert route.primary.picked_kind == "bed"
    # Nothing they described ordered these: no card is their best match.
    assert route.primary.best_match_first is False


async def test_the_designer_and_the_cards_lean_to_the_colour_they_like() -> None:
    """Beige from the chat, and the bed's own style where they named none."""
    coordinator, design, pipeline = _engine()
    state = _with_taste(_state(picks=(BED,)), _liking((AttributeFamily.COLOR, "Beige")))

    await coordinator.run(_turn(state, GoesWithPickAction(pick=1)))

    assert _values(design.requests[0].design_preferences) == ["Beige", "Scandinavian"]
    assert pipeline.preferences[0] == ("Beige", "Scandinavian")


async def test_a_style_they_stated_replaces_the_picks_own() -> None:
    coordinator, design, pipeline = _engine()
    state = _with_taste(_state(picks=(BED,)), _liking((AttributeFamily.STYLE, "Modern")))

    await coordinator.run(_turn(state, GoesWithPickAction(pick=1)))

    assert _values(design.requests[0].design_preferences) == ["Modern"]
    assert pipeline.preferences[0] == ("Modern",)


async def test_the_designer_is_told_every_pick_and_may_choose_any_stocked_kind() -> None:
    """A wardrobe is not among the bed's first pairings to show; the
    specialist's choice stands whatever the reviewed order says."""
    coordinator, design, pipeline = _engine(
        design=FakeDesign(InteriorDesignResult(needs=(WARDROBES,)))
    )

    result = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    request = design.requests[0]
    assert request.task.value == "complementary_recommendation"
    assert request.catalog_capabilities is not None
    assert [a.commerce_subcategory for a in request.anchors] == ["bed"]
    assert pipeline.requests[0].commerce_subcategory == "wardrobe"
    assert [c.subcategory for c in result.companions] == ["nightstand", "carpet"]


async def test_when_the_designer_cannot_answer_a_reviewed_pairing_is_shown() -> None:
    coordinator, _, pipeline = _engine(design=FakeDesign(error=LLMResponseInvalidError(reason="x")))

    result = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    assert [r.commerce_subcategory for r in pipeline.requests] == ["nightstand"]
    assert result.focus is not None and result.grounding.search is not None
    assert [c.subcategory for c in result.companions] == ["wardrobe", "carpet"]


async def test_when_nothing_the_designer_proposes_is_found_the_fallback_is_shown() -> None:
    coordinator, _, pipeline = _engine(pipeline=ScriptedPipeline((), (40, 41)))

    result = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    assert [r.commerce_subcategory for r in pipeline.requests] == ["carpet", "nightstand"]
    assert result.grounding.search is not None
    assert [p.name_english for p in result.grounding.search.products] == ["Piece 40", "Piece 41"]


async def test_a_catalog_failure_falls_back_to_offering_the_kinds() -> None:
    coordinator, _, _ = _engine(
        pipeline=ScriptedPipeline(CatalogUnavailableError(), CatalogUnavailableError())
    )

    result = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    assert result.grounding.search is None
    assert result.focus is not None
    assert [c.subcategory for c in result.companions] == ["nightstand", "wardrobe", "carpet"]


async def test_with_nothing_to_show_the_pick_stands_alone() -> None:
    coordinator, _, pipeline = _engine(
        design=FakeDesign(InteriorDesignResult()), stock=(("seating", "sofa"),)
    )

    result = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    assert pipeline.requests == []
    assert result.focus is not None
    assert result.grounding.search is None and result.companions == ()


async def test_a_kind_with_no_reviewed_pairings_still_gets_the_designers_suggestion() -> None:
    coordinator, _, pipeline = _engine(complements=None)

    result = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    assert pipeline.requests[0].commerce_subcategory == "carpet"
    assert result.grounding.search is not None and result.companions == ()


async def test_a_typed_pick_shows_what_goes_with_it_too() -> None:
    coordinator, _, pipeline = _engine(decision=_typed_pick())
    beds_on_screen = _state(picks=(), presented=(BED, OTHER))

    result = await coordinator.run(_typed(beds_on_screen))

    assert result.state.product_interaction.selected_product_ids == (BED,)
    assert result.selection_added is True
    assert pipeline.requests[0].commerce_subcategory == "carpet"
    assert result.focus is not None and result.focus.name_english == f"Piece {BED}"
    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.SEARCH_RESULTS
    assert route.primary.search_was_suggested is True


async def test_switched_off_a_pick_is_offered_the_kinds_and_nothing_is_searched() -> None:
    coordinator, design, pipeline = _engine(shows_products=False)

    result = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    assert design.requests == [] and pipeline.requests == []
    assert result.focus is not None and result.grounding.search is None
    assert [c.subcategory for c in result.companions] == ["nightstand", "wardrobe", "carpet"]


async def test_a_kind_already_picked_is_never_the_fallback_or_a_chip() -> None:
    """A wardrobe in their picks: the bed's fallback is the next stocked
    pairing, and wardrobes are not offered again."""
    from tests.unit.test_product_actions import WARDROBE

    coordinator, _, pipeline = _engine(design=FakeDesign(error=LLMResponseInvalidError(reason="x")))

    result = await coordinator.run(_turn(_state(picks=(WARDROBE, BED)), GoesWithPickAction(pick=2)))

    assert [r.commerce_subcategory for r in pipeline.requests] == ["nightstand"]
    assert [c.subcategory for c in result.companions] == ["carpet"]


async def test_the_suggested_cards_are_the_list_on_screen() -> None:
    """Committed like any search, so a tick, "show me more" and "not this
    one" work on them - and marked as chosen to go with the pick."""
    coordinator, _, _ = _engine()
    before = _state(picks=(BED,))

    result = await coordinator.run(_turn(before, GoesWithPickAction(pick=1)))

    interaction = result.state.product_interaction
    assert interaction.presented_product_ids == (30, 31, 32)
    assert result.state.active_search is not None
    assert result.state.active_search.revision == before.active_search.revision + 1  # type: ignore[union-attr]
    assert interaction.presented_search_revision == result.state.active_search.revision
    assert result.state.active_search.ordered_by_pick is True


async def test_a_whole_category_or_a_seat_count_from_the_designer_is_not_searched() -> None:
    """A need naming no kind could show the pick's own; a seat count is a
    requirement nobody stated. The first is skipped, the second dropped."""
    from app.schemas.discovery import SeatingCapacityConstraint

    whole_category = DesignCategoryNeed(
        commerce_category="seating", priority=DesignPriority.RECOMMENDED
    )
    seated = RUGS.model_copy(update={"seating_capacity": SeatingCapacityConstraint(min_capacity=4)})
    coordinator, _, pipeline = _engine(
        design=FakeDesign(InteriorDesignResult(needs=(whole_category, seated)))
    )

    await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    assert [r.commerce_subcategory for r in pipeline.requests] == ["carpet"]
    assert pipeline.requests[0].seating_capacity is None


async def test_beside_the_suggestions_the_next_step_never_offers_what_goes_with_it() -> None:
    """Rugs shown and nothing else stocked: the step offers a room or more
    browsing, not "what goes with the bed" - that is what is on screen."""
    coordinator, _, _ = _engine(stock=(("decor", "carpet"),))

    result = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    assert result.grounding.search is not None and result.companions == ()
    assert result.next_step is not None
    assert all(chip.product_action is None for chip in result.next_step.chips)
    assert result.next_step.kind.value == "room_around_picks"


async def test_no_card_is_their_best_match_on_a_later_page_of_suggestions() -> None:
    from app.services.response_view import best_match_first

    coordinator, _, _ = _engine()
    shown = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))
    assert shown.grounding.search is not None
    # The next page: the pick card and chips are gone, the ordering is not.
    later = shown.model_copy(
        update={
            "focus": None,
            "companions": (),
            "grounding": shown.grounding.model_copy(
                update={"search": shown.grounding.search.model_copy(update={"semantic_used": True})}
            ),
        }
    )

    assert best_match_first(later) is False
    unmarked = later.model_copy(
        update={
            "state": later.state.model_copy(
                update={
                    "active_search": later.state.active_search.model_copy(  # type: ignore[union-attr]
                        update={"ordered_by_pick": False}
                    )
                }
            )
        }
    )
    assert best_match_first(unmarked) is True


async def test_the_next_page_of_suggestions_is_still_ordered_by_the_pick() -> None:
    """ "Show me more" on the suggestions - tapped or typed, one path - keeps
    the mark, so no card on the next page is labelled their best match."""
    from app.schemas.agent_turn import CustomerTurnInput
    from app.schemas.search_action import MoreOptionsAction

    from tests.unit.test_turn_coordinator import CONTEXT

    coordinator, _, pipeline = _engine(pipeline=ScriptedPipeline((30, 31, 32), (33, 34)))
    shown = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    more = await coordinator.run(
        CustomerTurnInput(
            message="show me more",
            state=shown.state,
            context=CONTEXT,
            search_action=MoreOptionsAction(),
        )
    )

    assert pipeline.requests[1].exclude_product_ids == (30, 31, 32)
    assert more.state.active_search is not None
    assert more.state.active_search.ordered_by_pick is True


async def test_the_next_page_never_offers_what_goes_with_it_either() -> None:
    from app.schemas.agent_turn import CustomerTurnInput
    from app.schemas.search_action import MoreOptionsAction

    from tests.unit.test_turn_coordinator import CONTEXT

    coordinator, _, _ = _engine(
        pipeline=ScriptedPipeline((30, 31, 32), (33, 34)), stock=(("decor", "carpet"),)
    )
    shown = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    more = await coordinator.run(
        CustomerTurnInput(
            message="show me more",
            state=shown.state,
            context=CONTEXT,
            search_action=MoreOptionsAction(),
        )
    )

    assert more.next_step is not None
    assert all(chip.product_action is None for chip in more.next_step.chips)
