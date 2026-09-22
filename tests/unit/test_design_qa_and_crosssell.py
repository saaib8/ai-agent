"""Two capabilities the final experience pass added, and their boundaries.

**Interior design Q&A.** "What colours work with walnut?" is a question about
rooms, not a request to shop. It reaches the specialist as general advice,
comes back as guidance, and searches for nothing (CLAUDE.md 36, 38).

**Cross-sell that does not come back empty.** A retailer that *supports* lounge
chairs can still have one, and a suggestion that finds nothing left a customer
looking at an empty screen while being told about a product type they had never
mentioned. The specialist now returns an ordered shortlist and the application
shows the first role the catalog can actually fill (CLAUDE.md 26, 51).
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import LLMResponseInvalidError
from app.schemas.agent_decision import (
    AgentAction,
    CommercialReason,
    CustomerAgentDecision,
    DesignScope,
)
from app.schemas.design import (
    DesignCategoryNeed,
    DesignGuidance,
    DesignPriority,
    DesignTask,
    GuidanceMeasurement,
    GuidanceTopic,
    InteriorDesignResult,
)
from app.schemas.discovery import ProductSearchRequest
from app.schemas.grounding import TurnFailureCode
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.query import ConstraintSemantics
from app.schemas.resolution import ResolvedProductReference
from app.schemas.response import ResponseGroundingView, ResponseOutcomeKind
from app.services.response_view import route_response

from tests.unit.test_turn_coordinator import (
    FakeDesign,
    FakeHydration,
    FakeReferences,
    _coordinator,
    _state,
    _turn,
)

WALNUT = "What colours work well with walnut furniture?"


def _guidance(topic: GuidanceTopic = GuidanceTopic.COLOR) -> DesignGuidance:
    return DesignGuidance(
        topic=topic,
        summary="Walnut is a warm mid-tone, so the palette around it reads best kept light.",
        measurements=(),
    )


def _advice(**kwargs: Any) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF,
        design_scope=DesignScope.ADVICE,
        commercial_reason=CommercialReason.CUSTOMER_REQUEST,
        **kwargs,
    )


# ── design Q&A ──────────────────────────────────────────────────────────────


async def test_a_design_question_is_answered_and_searches_for_nothing() -> None:
    """The whole point of the scope: they asked a question, not for a shelf."""
    coordinator, parts = _coordinator(
        _advice(), design=FakeDesign(InteriorDesignResult(guidance=(_guidance(),)))
    )

    result = await coordinator.run(_turn(_state(), WALNUT))

    assert result.grounding.design_guidance
    assert result.grounding.search is None
    assert parts["pipeline"].calls == [], "no catalog query"
    assert parts["m7"].messages == [], "no query understanding"


async def test_the_specialist_is_asked_for_advice_not_a_plan() -> None:
    design = FakeDesign(InteriorDesignResult(guidance=(_guidance(),)))
    coordinator, _ = _coordinator(_advice(), design=design)

    await coordinator.run(_turn(_state(), WALNUT))

    request = design.requests[0]
    assert request.task is DesignTask.GENERAL_ADVICE
    assert request.catalog_capabilities is None, "advice is not about this shop"


async def test_general_advice_needs_no_retailer_capability_lookup() -> None:
    """General design knowledge is true of rooms, not of stock (CLAUDE.md 40)."""
    coordinator, parts = _coordinator(
        _advice(), design=FakeDesign(InteriorDesignResult(guidance=(_guidance(),)))
    )

    await coordinator.run(_turn(_state(), WALNUT))

    assert parts["capabilities"].calls == []


async def test_a_question_about_a_visible_product_carries_it_as_an_anchor() -> None:
    """"Would the second one work with a walnut table?" is still advice - about
    a specific piece, described by its design facts and never by its id
    (CLAUDE.md 42)."""
    design = FakeDesign(InteriorDesignResult(guidance=(_guidance(),)))
    coordinator, _ = _coordinator(
        _advice(reference=PresentedOrdinal(position=1)),
        references=FakeReferences(default=ResolvedProductReference(product_id=10)),
        hydration=FakeHydration(available=(10,)),
        design=design,
    )

    await coordinator.run(_turn(_state(), "Would the second one suit a walnut table?"))

    anchors = design.requests[0].anchors
    assert len(anchors) == 1
    assert anchors[0].commerce_category == "seating"
    assert "product_id" not in anchors[0].model_dump_json()


async def test_a_need_returned_on_an_advice_task_never_becomes_a_search() -> None:
    """The specialist is told not to turn advice into shopping, and the
    application drops any need anyway - a product cannot enter through this
    door (CLAUDE.md 38, 41)."""
    coordinator, parts = _coordinator(
        _advice(),
        design=FakeDesign(
            InteriorDesignResult(
                guidance=(_guidance(),),
                needs=(
                    DesignCategoryNeed(
                        commerce_category="decor",
                        commerce_subcategory="carpet",
                        priority=DesignPriority.RECOMMENDED,
                    ),
                ),
            )
        ),
    )

    result = await coordinator.run(_turn(_state(), WALNUT))

    assert parts["pipeline"].calls == []
    assert result.grounding.search is None


async def test_a_design_question_that_cannot_be_answered_says_so() -> None:
    """Theirs, so its failure is theirs to hear - unlike a suggestion of ours
    (CLAUDE.md 51)."""
    coordinator, _ = _coordinator(
        _advice(), design=FakeDesign(error=LLMResponseInvalidError(reason="bad"))
    )

    result = await coordinator.run(_turn(_state(), WALNUT))

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.DESIGN_UNAVAILABLE


async def test_advice_with_no_guidance_is_reported_rather_than_dressed_up() -> None:
    coordinator, _ = _coordinator(_advice(), design=FakeDesign(InteriorDesignResult()))

    result = await coordinator.run(_turn(_state(), WALNUT))

    assert result.grounding.failure is not None


async def test_advice_routes_to_its_own_response_outcome() -> None:
    coordinator, _ = _coordinator(
        _advice(), design=FakeDesign(InteriorDesignResult(guidance=(_guidance(),)))
    )

    result = await coordinator.run(_turn(_state(), WALNUT))
    view = route_response(result).primary

    assert isinstance(view, ResponseGroundingView)
    assert view.kind is ResponseOutcomeKind.DESIGN_ADVICE
    assert view.guidance
    assert view.presented_count == 0, "an answer shows no cards"


async def test_a_guidance_measurement_is_sayable_and_no_product_fact_is() -> None:
    """The figure reaches prose because the specialist produced it as a tagged
    measurement, which is why the summary forbids digits (CLAUDE.md 41)."""
    from app.services.numeric_guard import guidance_figures

    measured = DesignGuidance(
        topic=GuidanceTopic.SIZING,
        summary="A rug reads best when the front legs of the seating sit on it.",
        measurements=(
            GuidanceMeasurement(
                label="rug width under a three-seat sofa",
                minimum_cm="200",
                maximum_cm="240",
            ),
        ),
    )

    assert [str(bound) for bound in guidance_figures((measured,))] == ["200", "240"]


# ── cross-sell across roles ─────────────────────────────────────────────────


def _need(subcategory: str, category: str = "tables") -> DesignCategoryNeed:
    return DesignCategoryNeed(
        commerce_category=category,
        commerce_subcategory=subcategory,
        priority=DesignPriority.RECOMMENDED,
    )


class RoleAwareDiscovery:
    """Resolves each need to a search for that need's own category."""

    def resolve_need(self, need: DesignCategoryNeed, request: Any) -> Any:
        from app.schemas.query import ResolvedSearch

        return ResolvedSearch(
            request=ProductSearchRequest(
                commerce_category=need.commerce_category,
                commerce_subcategory=need.commerce_subcategory,
            ),
            semantics=ConstraintSemantics(),
        )


class ThinCatalogPipeline:
    """Empty for every subcategory but one."""

    def __init__(self, stocked: str) -> None:
        self.stocked = stocked
        self.calls: list[str | None] = []

    async def execute(self, resolved: Any, context: Any, **kwargs: Any) -> Any:
        from app.schemas.grounding import SearchExecutionGrounding, SearchOutcome
        from app.schemas.relaxation import StopReason
        from app.schemas.resolution import ProductSearchExecutionResult

        subcategory = resolved.request.commerce_subcategory
        self.calls.append(subcategory)
        hit = subcategory == self.stocked
        from app.services.grounding_builder import to_grounded_product

        from tests.unit.test_turn_coordinator import _product

        products = (
            (to_grounded_product(_product(99), grounding_ref=1, presented_ordinal=1,
                                 relaxation_depth=0),)
            if hit
            else ()
        )
        return ProductSearchExecutionResult(
            presented_product_ids=(99,) if hit else (),
            grounding=SearchExecutionGrounding(
                outcome=SearchOutcome.RESULTS if hit else SearchOutcome.ZERO_RESULTS,
                products=products,
                eligible_count=1 if hit else 0,
                ranked_count=1 if hit else 0,
                selected_count=1 if hit else 0,
                presented_count=1 if hit else 0,
                exact_candidate_count=1 if hit else 0,
                stop_reason=StopReason.EXACT_SUFFICIENT,
            ),
        )


def _complement(**kwargs: Any) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF,
        design_scope=DesignScope.COMPLEMENT,
        commercial_reason=CommercialReason.PURCHASE_PROGRESSION,
        **kwargs,
    )


async def test_a_thin_first_role_falls_through_to_one_the_shop_can_fill() -> None:
    """The defect this closes: lounge chairs are *supported* and there is one
    of them, so the suggestion returned nothing (CLAUDE.md 26)."""
    pipeline = ThinCatalogPipeline(stocked="center-table")
    coordinator, _ = _coordinator(
        _complement(),
        design=FakeDesign(
            InteriorDesignResult(
                needs=(_need("lounge-chair", "seating"), _need("center-table"))
            )
        ),
        design_discovery=RoleAwareDiscovery(),
        pipeline=pipeline,  # type: ignore[arg-type]
        hydration=FakeHydration(available=(10, 99)),
    )

    result = await coordinator.run(_turn(_state(selected=(10,)), "I like the second one."))

    assert pipeline.calls == ["lounge-chair", "center-table"], "in the given order"
    assert result.grounding.search is not None
    assert result.grounding.search.presented_count == 1


async def test_only_one_category_is_shown_per_turn() -> None:
    """A sofa they like earns a rug, not a rug and a table and a lamp
    (CLAUDE.md 28)."""
    pipeline = ThinCatalogPipeline(stocked="center-table")
    coordinator, _ = _coordinator(
        _complement(),
        design=FakeDesign(
            InteriorDesignResult(needs=(_need("center-table"), _need("console")))
        ),
        design_discovery=RoleAwareDiscovery(),
        pipeline=pipeline,  # type: ignore[arg-type]
        hydration=FakeHydration(available=(10, 99)),
    )

    await coordinator.run(_turn(_state(selected=(10,)), "I like the second one."))

    assert pipeline.calls == ["center-table"], "it stopped at the first that worked"


async def test_every_role_empty_reports_nothing_rather_than_a_failure() -> None:
    """Our idea, and it came to nothing. The customer asked for none of it, so
    nothing they asked for failed (CLAUDE.md 51)."""
    pipeline = ThinCatalogPipeline(stocked="nothing-at-all")
    coordinator, _ = _coordinator(
        _complement(),
        design=FakeDesign(
            InteriorDesignResult(needs=(_need("center-table"), _need("console")))
        ),
        design_discovery=RoleAwareDiscovery(),
        pipeline=pipeline,  # type: ignore[arg-type]
        hydration=FakeHydration(available=(10,)),
    )

    result = await coordinator.run(_turn(_state(selected=(10,)), "I like the second one."))

    assert result.grounding.failure is None, "nothing of theirs failed"
    assert result.grounding.search is None
    route = route_response(result).primary
    assert isinstance(route, ResponseGroundingView)
    assert route.kind is ResponseOutcomeKind.ANSWER, "the turn is what they did"


# ── one statement, two shapes ───────────────────────────────────────────────


def test_which_piece_is_read_the_same_way_whichever_field_carried_it() -> None:
    """The live defect: "I like the second one" failed roughly one turn in
    three.

    A complement is *about* the piece they settled on, so the model names it -
    sometimes on `design_anchor`, sometimes on `reference`. Both say the same
    thing, the provider's strict schema offers both on every decision, and
    refusing the plainer one turned an artefact of that into a 502.
    """
    from app.schemas.agent_decision import DesignAnchorIntent
    from app.schemas.product_reference import PresentedOrdinal

    bare = _complement(reference=PresentedOrdinal(position=2))
    full = _complement(
        design_anchor=DesignAnchorIntent(reference=PresentedOrdinal(position=2))
    )

    assert bare.anchor_reference == PresentedOrdinal(position=2)
    assert full.anchor_reference == PresentedOrdinal(position=2)


def test_the_fuller_shape_wins_when_both_arrive() -> None:
    """`design_anchor` can also say they already own it, or want two, so a
    bare reference beside it is the less complete account."""
    from app.schemas.agent_decision import DesignAnchorIntent
    from app.schemas.product_reference import FocusedProduct, PresentedOrdinal

    decision = _complement(
        reference=FocusedProduct(),
        design_anchor=DesignAnchorIntent(reference=PresentedOrdinal(position=3), quantity=2),
    )

    assert decision.anchor_reference == PresentedOrdinal(position=3)


def test_a_bare_reference_claims_nothing_the_customer_did_not_say() -> None:
    """It names the piece and stops. Ownership and quantity stay at the
    application's defaults rather than being read into a pointing word
    (CLAUDE.md 3.3)."""
    from app.schemas.product_reference import PresentedOrdinal

    decision = _complement(reference=PresentedOrdinal(position=2))

    assert decision.design_anchor is None, "nothing was invented to hold it"


def test_a_reference_outside_a_design_handoff_is_not_an_anchor() -> None:
    from app.schemas.product_reference import PresentedOrdinal

    detail = CustomerAgentDecision(
        action=AgentAction.PRODUCT_DETAIL, reference=PresentedOrdinal(position=1)
    )

    assert detail.anchor_reference is None
