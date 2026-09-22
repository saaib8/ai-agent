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
    InteriorDesignRequest,
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
    _resolved,
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
    # Its own code, so the reply can say the *question* went unanswered. A
    # customer who asked how big a rug should be was told a room plan could
    # not be put together - an answer to something they never asked (M16 3).
    assert result.grounding.failure.code is (
        TurnFailureCode.DESIGN_ADVICE_UNAVAILABLE
    )


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


# ── a question that spans turns ─────────────────────────────────────────────
#
# From a real session:
#
#   "How big should a rug be under a sofa?"  -> answered well
#   "5x5"                                    -> "what unit?"
#   "m"                                      -> "I wasn't able to put a room
#                                               plan together just now."
#
# Three things were wrong. The specialist was handed the word **m** and had no
# conversation to recover the question from; the failure borrowed the
# room-plan wording for a question about a rug; and the turn ended in a dead
# end rather than offering the step the answer had earned.


def test_the_decision_can_restate_a_question_that_spans_turns() -> None:
    decision = _advice(
        design_question="how big should a rug be under a sofa in a 5 by 5 metre room"
    )

    assert decision.design_question is not None
    assert "5 by 5" in decision.design_question


async def test_the_restated_question_is_what_the_specialist_is_asked() -> None:
    """Not the bare continuation. "m" is not a question."""
    design = FakeDesign(InteriorDesignResult(guidance=(_guidance(),)))
    coordinator, _ = _coordinator(
        _advice(design_question="how big should a rug be for a 5 by 5 metre room"),
        design=design,
    )

    await coordinator.run(_turn(_state(), "m"))

    assert design.requests[0].question == (
        "how big should a rug be for a 5 by 5 metre room"
    )


async def test_the_message_is_used_when_it_asks_the_whole_question() -> None:
    """The common case, unchanged: most questions arrive whole."""
    design = FakeDesign(InteriorDesignResult(guidance=(_guidance(),)))
    coordinator, _ = _coordinator(_advice(), design=design)

    await coordinator.run(_turn(_state(), WALNUT))

    assert design.requests[0].question == WALNUT


async def test_an_unanswered_question_does_not_talk_about_a_room_plan() -> None:
    """The customer asked about a rug. Telling them a room plan failed answers
    something they never asked."""
    from app.services.response_wording import FAILURE_WORDING

    coordinator, _ = _coordinator(_advice(), design=FakeDesign(InteriorDesignResult()))

    result = await coordinator.run(_turn(_state(), WALNUT))

    assert result.grounding.failure is not None
    wording = FAILURE_WORDING[result.grounding.failure.code]
    assert "room plan" not in wording
    assert "answer that one" in wording


async def test_an_advice_turn_folds_the_customers_facts_in_exactly_once() -> None:
    """A room size stated in the same breath as the question is part of it, so
    proposals land before the specialist is asked - and must not then be
    applied a second time, which would duplicate every preference."""
    from app.schemas.agent_decision import CustomerStateProposal

    coordinator, _ = _coordinator(
        _advice(state_proposal=CustomerStateProposal(room_type="living room")),
        design=FakeDesign(InteriorDesignResult(guidance=(_guidance(),))),
    )

    result = await coordinator.run(_turn(_state(), WALNUT))

    room = result.state.room_project
    assert room is not None
    assert room.room_type == "living room"


async def test_the_specialist_sees_facts_stated_in_the_same_message() -> None:
    from app.schemas.agent_decision import CustomerStateProposal

    design = FakeDesign(InteriorDesignResult(guidance=(_guidance(),)))
    coordinator, _ = _coordinator(
        _advice(state_proposal=CustomerStateProposal(room_type="living room")),
        design=design,
    )

    await coordinator.run(_turn(_state(), WALNUT))

    assert design.requests[0].room_type == "living room"


def test_a_design_answer_may_offer_to_go_and_look() -> None:
    """The dead end this closes: an answer, and nothing to do with it."""
    from app.schemas.agent_decision import FollowUpGoal, FollowUpPolicy

    decision = _advice(
        follow_up_policy=FollowUpPolicy.OPTIONAL,
        follow_up_goal=FollowUpGoal.PRODUCT_SEARCH,
    )

    assert decision.follow_up_goal is FollowUpGoal.PRODUCT_SEARCH


def test_the_agent_is_told_to_offer_rather_than_deliver() -> None:
    """They asked a question. Searching anyway answers one they did not ask."""
    from app.prompts.customer_commerce.v1 import INSTRUCTIONS

    flat = " ".join(INSTRUCTIONS.split())

    assert "A DESIGN ANSWER SHOULD LEAD SOMEWHERE" in flat
    assert "Offer; do not deliver" in flat
    assert "A CONTINUED QUESTION IS STILL THE QUESTION" in flat


# ── an agent has to be able to see the question it asked ────────────────────


def test_the_follow_up_question_is_stored_as_part_of_what_was_said() -> None:
    """The defect behind "yes please" meaning nothing.

    The agent offered to look for rugs, the offer lived only in
    `follow_up_question`, and only `message` was written to history - so the
    next turn saw an answer to a question that was not there and asked what
    kind of furniture they wanted (M16 5).

    Two fields on the wire, one utterance to the person reading it.
    """
    from app.core.config import SessionSettings
    from app.schemas.agent_turn import CustomerResponse
    from app.schemas.chat import ChatRequest
    from app.schemas.session import new_session
    from app.services.chat_runtime import ChatRuntime, LoadedSession

    runtime = ChatRuntime(None, None, None, SessionSettings())  # type: ignore[arg-type]
    loaded = LoadedSession(envelope=new_session(), loaded_revision=0, existed=False)

    conversation = runtime.next_conversation(
        loaded,
        ChatRequest(session_id="s", store_id=50, message="how big should a rug be?"),
        CustomerResponse(
            message="As a rule, about 15-30 cm beyond each end of the sofa.",
            follow_up_question="Would you like me to look for rugs that size?",
        ),
    )

    said = conversation.messages[-1].content
    assert "15-30 cm beyond" in said
    assert "Would you like me to look for rugs that size?" in said


def test_a_turn_with_no_question_stores_only_the_prose() -> None:
    from app.core.config import SessionSettings
    from app.schemas.agent_turn import CustomerResponse
    from app.schemas.chat import ChatRequest
    from app.schemas.session import new_session
    from app.services.chat_runtime import ChatRuntime, LoadedSession

    runtime = ChatRuntime(None, None, None, SessionSettings())  # type: ignore[arg-type]
    loaded = LoadedSession(envelope=new_session(), loaded_revision=0, existed=False)

    conversation = runtime.next_conversation(
        loaded,
        ChatRequest(session_id="s", store_id=50, message="thanks"),
        CustomerResponse(message="Happy to help."),
    )

    assert conversation.messages[-1].content == "Happy to help."


# ── a complement is something else ──────────────────────────────────────────
#
# From a real session: "I want 6 dining chairs, I like the first one". The
# specialist proposed dining chairs and chairs - more of what they had just
# chosen - and put a seating capacity of 6 on them, reading a quantity as a
# per-chair capacity. Both searches returned nothing, so the turn had no cards
# and the reply became a receipt: "I've got that as your choice for the 6
# dining chairs." (M18)


def _anchor(subcategory: str = "dining-chair", category: str = "seating") -> Any:
    from app.schemas.design import AnchorProduct

    return AnchorProduct(
        commerce_category=category,
        commerce_subcategory=subcategory,
        main_color="Beige",
        styles=("Modern",),
        locked=True,
        quantity=1,
    )


def _complement_request(
    anchor_subcategory: str = "dining-chair",
) -> InteriorDesignRequest:
    from tests.unit.test_design_revision import _capabilities

    return InteriorDesignRequest(
        task=DesignTask.COMPLEMENTARY_RECOMMENDATION,
        design_brief="I like the first one",
        anchors=(_anchor(anchor_subcategory),),
        # Everything the tests below propose is stocked, so what survives is
        # decided by the anchor rule rather than by capability filtering.
        catalog_capabilities=_capabilities(
            ("seating", "dining-chair"),
            ("seating", "stool"),
            ("tables", "dining-table"),
            ("decor", "carpet"),
        ),
    )


def _validated(
    needs: list[DesignCategoryNeed], request: InteriorDesignRequest
) -> InteriorDesignResult:
    from app.services.interior_design import InteriorDesignAgent

    agent = object.__new__(InteriorDesignAgent)
    agent._taxonomy = __import__(
        "app.taxonomy.registry", fromlist=["load_taxonomy"]
    ).load_taxonomy()
    return agent._complementary(InteriorDesignResult(needs=tuple(needs)), request)


def test_the_anchors_own_kind_is_never_proposed() -> None:
    """They chose a dining chair. Offering dining chairs answers nothing."""
    kept = _validated(
        [_need("dining-chair", "seating"), _need("dining-table")],
        _complement_request(),
    )

    assert [n.commerce_subcategory for n in kept.needs] == ["dining-table"]


def test_a_different_kind_of_seating_is_still_allowed() -> None:
    """Matched on the pair, so a chair does not block every kind of seating -
    only chairs. A dining set may genuinely want a bench beside it."""
    kept = _validated(
        [_need("stool", "seating"), _need("dining-table")], _complement_request()
    )

    assert {n.commerce_subcategory for n in kept.needs} == {"stool", "dining-table"}


def test_with_no_anchor_nothing_is_filtered() -> None:
    """A room plan has no single anchor to be beside."""
    from tests.unit.test_design_revision import _capabilities

    request = InteriorDesignRequest(
        task=DesignTask.COMPLEMENTARY_RECOMMENDATION,
        design_brief="what goes here?",
        anchors=(_anchor(),),
        catalog_capabilities=_capabilities(("tables", "dining-table")),
    )
    kept = _validated([_need("dining-table")], request)

    assert len(kept.needs) == 1


def test_dropping_the_anchors_kind_can_leave_nothing() -> None:
    """The real failure, reduced: every proposal was the same kind as the
    anchor, so the shortlist empties - and the turn shows nothing rather than
    running a search for what they already have."""
    kept = _validated([_need("dining-chair", "seating")], _complement_request())

    assert kept.needs == ()


def test_the_specialist_is_told_a_quantity_is_not_a_product_type() -> None:
    """"Six dining chairs" is a quantity. It is not a request for a second
    kind of thing, and it is not a capacity for any one chair."""
    from app.prompts.interior_design.v1 import build_instructions
    from app.taxonomy.registry import load_taxonomy

    flat = " ".join(build_instructions(load_taxonomy()).split())

    assert "A complement is a *different* kind of thing" in flat
    assert "has given you a quantity, not a second product type" in flat
    assert "never how many of it the room wants" in flat


def test_the_reply_is_told_not_to_stop_at_a_receipt() -> None:
    from app.prompts.customer_commerce.response_v1 import INSTRUCTIONS

    flat = " ".join(INSTRUCTIONS.split())

    assert "Never end on the acknowledgement alone" in flat
    assert "acknowledge in a clause, not a sentence" in flat


# ── a request finished across turns ─────────────────────────────────────────
#
# "How big should a rug be under a sofa?" -> a good answer, 15-30 cm beyond
# each arm. Then "Do you have anything like that in store?" -> "What size rug
# are you looking for?" - asking back the very thing it had just answered.
#
# Query understanding reads one message and never the conversation, so the
# second message arrived naming no product at all (M22 1).


async def test_the_restated_request_is_what_gets_interpreted() -> None:
    """Not the bare continuation, which names nothing."""
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, search_request="rugs"),
        interpretation=_resolved(),
    )

    await coordinator.run(_turn(_state(), "do you have anything like that in store?"))

    assert parts["m7"].messages == ["rugs"]


async def test_the_message_is_used_when_it_asks_by_itself() -> None:
    """The common case, unchanged."""
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH), interpretation=_resolved()
    )

    await coordinator.run(_turn(_state(), "show me modern sofas"))

    assert parts["m7"].messages == ["show me modern sofas"]


async def test_the_restatement_is_still_interpreted_and_validated() -> None:
    """It is read exactly as a message would be, so a product type invented in
    it is rejected by the same validation (CLAUDE.md 14.3)."""
    from app.schemas.query import ClarificationReason, ClarificationRequired

    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, search_request="luxury couches"),
        interpretation=ClarificationRequired(
            reason=ClarificationReason.NO_COMMERCE_CATEGORY
        ),
    )

    result = await coordinator.run(_turn(_state(), "anything like that?"))

    assert parts["pipeline"].calls == [], "nothing unapproved reached the catalog"
    assert result.grounding.deterministic_clarification is not None


def test_a_restatement_is_bounded() -> None:
    """A request, not a transcript - so a conversation cannot be pasted in."""
    import pytest as _pytest
    from app.schemas.agent_decision import MAX_SEARCH_REQUEST_CHARS
    from pydantic import ValidationError

    assert MAX_SEARCH_REQUEST_CHARS == 300
    with _pytest.raises(ValidationError):
        CustomerAgentDecision(
            action=AgentAction.SEARCH, search_request="x" * (MAX_SEARCH_REQUEST_CHARS + 1)
        )


def test_the_agent_is_told_to_restate_rather_than_resolve() -> None:
    from app.prompts.customer_commerce.v1 import INSTRUCTIONS

    flat = " ".join(INSTRUCTIONS.split())

    assert "A REQUEST FINISHED ACROSS TURNS IS STILL THE REQUEST" in flat
    assert "Restate; do not resolve" in flat
    assert "never answer a question with the same question" in flat
