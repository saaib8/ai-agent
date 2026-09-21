"""Complete whole-room journeys, from a decision to what the customer sees.

Every test here runs the real coordinator and then the real response routing
and the real presentation renderer. Nothing asserts on a helper in isolation:
the point is that the phases compose - that a plan the specialist returned
becomes needs with fresh ids, that those needs become a discovery, that the
optimiser's choice becomes committed state, and that the cards rendered from
that state carry the prices the optimiser actually verified.

Two invariants recur and are worth naming once.

**State defines the room; the catalog defines whether it can be described.**
Card membership and order come from committed state, and a product the catalog
will not return makes the room unrenderable rather than silently shorter.

**Speculation never commits.** A failure anywhere after the customer's own
statements leaves the previous valid plan and bundle exactly as they were,
while the things they actually said - a budget, a lock, an acquisition - stay.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.core.exceptions import (
    BundlePresentationError,
    CatalogUnavailableError,
    LLMResponseInvalidError,
)
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    BundleInteractionIntent,
    BundleInteractionOp,
    BundleReplacementIntent,
    BundleReplacementMode,
    CustomerAgentDecision,
    DesignRevisionIntent,
)
from app.schemas.agent_state import AgentStateV1, BundleItemStatus
from app.schemas.bundle import (
    BundleLine,
    BundleStatus,
    RoomBundle,
    UnmetNeed,
    UnmetReason,
)
from app.schemas.bundle_reference import BundleItemOrdinal, DesignNeedCategoryMatch
from app.schemas.design import DesignPriority
from app.schemas.grounding import TurnFailureCode
from app.schemas.refinement import SemanticIntentOp, SemanticIntentRefinement
from app.schemas.response import (
    DeterministicResponseKind,
    ResponseGroundingView,
    ResponseOutcomeKind,
)
from app.services.bundle_presentation import (
    build_bundle_presentation,
    build_state_bundle_presentation,
)
from app.services.response_view import route_response

from tests.unit.test_design_revision import (
    a_room,
    category_need,
    chosen,
    line,
    need,
    plan_of,
    product,
    room,
    run,
)
from tests.unit.test_turn_coordinator import FakeDesign


def bundle_of(
    *entries: tuple[int, int, str],
    status: BundleStatus = BundleStatus.COMPLETE,
    locked: tuple[int, ...] = (),
    owned: tuple[int, ...] = (),
    unmet: tuple[UnmetNeed, ...] = (),
) -> RoomBundle:
    """A chosen room: (product_id, need_index, price) per line."""
    # A line that is not newly selected is a lock: the optimiser only ever
    # emits ALREADY_OWNED for a piece the customer already told us about, and
    # the contract enforces that pairing.
    held = set(locked) | set(owned)
    lines = tuple(
        BundleLine(
            need_index=index,
            product=product(product_id, price=price),
            quantity=1,
            locked=product_id in held,
            acquisition=(
                BundleAcquisition.ALREADY_OWNED
                if product_id in owned
                else BundleAcquisition.TO_BUY
            ),
            relaxation_depth=None if product_id in held else 0,
        )
        for product_id, index, price in entries
    )
    spend = sum(
        (Decimal(price) for product_id, _i, price in entries if product_id not in owned),
        Decimal(0),
    )
    return RoomBundle(
        lines=lines,
        status=status,
        unmet=unmet,
        new_spend_total=spend if spend else None,
        currency="SAR" if spend else None,
        total_unavailable=None if spend else "no_priced_lines",
    )


def refine(op: BundleInteractionOp, **kwargs: Any) -> CustomerAgentDecision:
    kwargs.setdefault("selector", BundleItemOrdinal(ordinal=1))
    return CustomerAgentDecision(
        action=AgentAction.BUNDLE_REFINE,
        bundle_interaction=BundleInteractionIntent(op=op, **kwargs),
    )


def shown(result: Any) -> Any:
    """The rendered room, asserted to exist so the test reads about content."""
    presentation = build_bundle_presentation(result)
    assert presentation is not None, "this turn should have produced a room"
    return presentation


def cards(result: Any) -> list[tuple[str, int]]:
    return [(item.name_english, item.quantity) for item in shown(result).items]


# ── A. an initial room, end to end ══════════════════════════════════════════


async def test_a_room_is_planned_discovered_optimised_committed_and_shown() -> None:
    plan = plan_of(
        category_need("seating", "sofa"), category_need("lighting", "floor-lamp")
    )
    outcome = bundle_of((10, 0, "1000.00"), (11, 1, "1000.00"))

    result, parts = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        AgentStateV1(),
        available=(10, 11),
        design=FakeDesign(plan),
        outcome=outcome,
    )

    # the specialist was asked once, and asked to plan rather than revise
    assert len(parts["design"].requests) == 1
    assert parts["design"].requests[0].revision is None

    # every planned need reached discovery
    assert len(parts["design_discovery"].calls) == 1
    assert len(parts["design_discovery"].calls[0][1].needs) == 2

    # the plan and the room committed together, with fresh durable ids
    after = room(result.state)
    assert [n.commerce_category for n in after.design_needs] == ["seating", "lighting"]
    assert [i.product_id for i in after.bundle_items] == [10, 11]
    assert {i.need_id for i in after.bundle_items} == {
        n.need_id for n in after.design_needs
    }

    # and the customer is shown the room, priced from what the optimiser verified
    assert route_response(result).primary.kind is ResponseOutcomeKind.ROOM_BUNDLE
    presentation = shown(result)
    assert presentation.status is BundleStatus.COMPLETE
    assert [item.grounding_ref for item in presentation.items] == [1, 2]
    assert presentation.totals.new_spend_total == Decimal("2000.00")
    assert presentation.totals.currency == "SAR"


async def test_a_partial_room_is_shown_as_partial() -> None:
    """A room missing something required is never framed as complete."""
    outcome = bundle_of(
        (10, 0, "1000.00"),
        status=BundleStatus.PARTIAL,
        unmet=(
            UnmetNeed(
                need_index=1,
                priority=DesignPriority.REQUIRED,
                shortfall=1,
                reason=UnmetReason.NO_CANDIDATES,
            ),
        ),
    )

    result, _ = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        AgentStateV1(),
        available=(10,),
        design=FakeDesign(
            plan_of(category_need("seating", "sofa"), category_need("lighting", "floor-lamp"))
        ),
        outcome=outcome,
    )

    assert shown(result).status is BundleStatus.PARTIAL
    primary = route_response(result).primary
    assert isinstance(primary, ResponseGroundingView)
    assert primary.bundle is not None
    assert primary.bundle.status is BundleStatus.PARTIAL


# ── B. replacing a product ══════════════════════════════════════════════════


async def test_replacing_a_product_keeps_the_role_and_rediscovers_the_room() -> None:
    state = a_room(needs=(need(), need("lighting", "floor-lamp")), lines=(line(10),))
    before = [n.need_id for n in room(state).design_needs]

    result, parts = await run(
        refine(
            BundleInteractionOp.REPLACE_PRODUCT,
            replacement=BundleReplacementIntent(mode=BundleReplacementMode.ALTERNATIVE),
        ),
        state,
        available=(10, 77),
        outcome=chosen(77),
    )

    after = room(result.state)
    assert [n.need_id for n in after.design_needs] == before, "same roles, same ids"
    assert [i.product_id for i in after.bundle_items] == [77]
    # the whole room was reconsidered, not just the changed role
    assert len(parts["design_discovery"].calls[0][1].needs) == 2
    assert route_response(result).primary.kind is ResponseOutcomeKind.ROOM_BUNDLE


# ── C. a different character ════════════════════════════════════════════════


async def test_new_wording_and_the_room_it_produced_commit_together() -> None:
    state = a_room(needs=(need(intent="plain"),), lines=(line(10),))

    result, parts = await run(
        refine(
            BundleInteractionOp.REPLACE_PRODUCT,
            replacement=BundleReplacementIntent(
                mode=BundleReplacementMode.SEMANTIC,
                semantic_intent=SemanticIntentRefinement(
                    op=SemanticIntentOp.SET, value="more minimal"
                ),
            ),
        ),
        state,
        available=(10, 77),
        outcome=chosen(77),
    )

    after = room(result.state)
    assert after.design_needs[0].semantic_intent == "more minimal"
    assert [i.product_id for i in after.bundle_items] == [77]
    # the staged wording is what was searched with
    staged = next(iter(parts["design_discovery"].overrides.values()))
    assert staged.semantic_intent.value == "more minimal"


async def test_failed_replacement_keeps_the_old_wording_and_the_old_room() -> None:
    state = a_room(needs=(need(intent="plain"),), lines=(line(10),))

    result, _ = await run(
        refine(
            BundleInteractionOp.REPLACE_PRODUCT,
            replacement=BundleReplacementIntent(
                mode=BundleReplacementMode.SEMANTIC,
                semantic_intent=SemanticIntentRefinement(
                    op=SemanticIntentOp.SET, value="more minimal"
                ),
            ),
        ),
        state,
        available=(10,),
        discovery_error=CatalogUnavailableError(),
    )

    after = room(result.state)
    assert after.design_needs[0].semantic_intent == "plain"
    assert [i.product_id for i in after.bundle_items] == [10]


# ── D. keeping a piece ══════════════════════════════════════════════════════


async def test_keeping_a_piece_searches_nothing_and_designs_nothing() -> None:
    state = a_room(needs=(need(),), lines=(line(10), line(10)))

    result, parts = await run(refine(BundleInteractionOp.LOCK), state)

    assert [i.status for i in room(result.state).bundle_items] == [
        BundleItemStatus.LOCKED,
        BundleItemStatus.LOCKED,
    ], "group-wide"
    assert parts["design"].requests == []
    assert parts["design_discovery"].calls == []
    assert route_response(result).primary.kind is DeterministicResponseKind.BUNDLE_KEPT


# ── E. what they already own ════════════════════════════════════════════════


async def test_an_already_owned_piece_stays_in_the_room_without_a_reoptimisation() -> None:
    """No budget, so nothing about the room's cost can change what fits.

    And no `RoomBundle` is produced: nothing was optimised, so inventing one
    would assert a status, unmet needs and a feasibility claim nobody
    established. The room is rendered from state instead.
    """
    state = a_room(needs=(need(),), lines=(line(10),))

    result, parts = await run(
        refine(
            BundleInteractionOp.SET_ACQUISITION,
            acquisition=BundleAcquisition.ALREADY_OWNED,
        ),
        state,
        available=(10,),
    )

    after = room(result.state)
    assert after.bundle_items[0].acquisition is BundleAcquisition.ALREADY_OWNED
    assert build_bundle_presentation(result) is None, "nothing optimised, nothing claimed"
    assert parts["design_discovery"].calls == []

    presentation = build_state_bundle_presentation(after, [product(10)])
    assert presentation is not None
    card = presentation.items[0]
    assert card.acquisition is BundleAcquisition.ALREADY_OWNED
    assert card.new_spend_line_total is None, "owned is not a zero-priced buy"
    assert card.unit_price == Decimal("1000.00"), "still a real product"
    assert presentation.totals.new_spend_total is None


async def test_a_budgeted_room_is_reoptimised_when_a_piece_becomes_owned() -> None:
    """What they own changes what the budget can still buy."""
    state = a_room(
        needs=(need(), need("decor", "carpet")), lines=(line(10),), budget="1500"
    )

    result, parts = await run(
        refine(
            BundleInteractionOp.SET_ACQUISITION,
            acquisition=BundleAcquisition.ALREADY_OWNED,
        ),
        state,
        available=(10, 77),
        outcome=bundle_of((10, 0, "1000.00"), (77, 1, "400.00"), owned=(10,)),
    )

    assert len(parts["design_discovery"].calls) == 1, "the whole room, re-costed"
    presentation = shown(result)
    assert presentation.totals.new_spend_total == Decimal("400.00")
    assert presentation.items[0].new_spend_line_total is None
    assert presentation.totals.within_budget is True


# ── F. removing a role ══════════════════════════════════════════════════════


async def test_removing_a_role_keeps_the_rest_of_the_plan() -> None:
    state = a_room(
        needs=(need(), need("lighting", "floor-lamp")),
        lines=(line(10, need_index=0), line(11, need_index=1)),
    )

    result, parts = await run(
        CustomerAgentDecision(
            action=AgentAction.BUNDLE_REFINE,
            bundle_interaction=BundleInteractionIntent(
                op=BundleInteractionOp.REMOVE_NEED,
                need_selector=DesignNeedCategoryMatch(commerce_category="lighting"),
            ),
        ),
        state,
        available=(10, 11, 77),
        outcome=chosen(10),
    )

    after = room(result.state)
    assert [n.commerce_category for n in after.design_needs] == ["seating"]
    assert 11 not in [i.product_id for i in after.bundle_items]
    assert len(parts["design_discovery"].calls[0][1].needs) == 1


# ── G. recomposing the room ═════════════════════════════════════════════════


async def test_adding_a_use_revises_the_existing_plan_and_reissues_its_ids() -> None:
    state = a_room(needs=(need(),), lines=(line(10),))
    old_ids = {n.need_id for n in room(state).design_needs}
    revised = plan_of(
        category_need("seating", "sofa"), category_need("seating", "lounge-chair")
    )

    result, parts = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        state,
        available=(10, 77),
        design=FakeDesign(revised),
        outcome=bundle_of((10, 0, "1000.00"), (77, 1, "500.00")),
    )

    # the plan being revised reached the specialist, without identity
    sent = parts["design"].requests[0].revision
    assert sent is not None
    assert [n.commerce_subcategory for n in sent.current_needs] == ["sofa"]
    assert "need_id" not in parts["design"].requests[0].model_dump_json()

    after = room(result.state)
    assert [n.commerce_subcategory for n in after.design_needs] == ["sofa", "lounge-chair"]
    assert {n.need_id for n in after.design_needs}.isdisjoint(old_ids), "fresh ids"
    assert len(cards(result)) == 2


# ── H. one use replaced by another ══════════════════════════════════════════


async def test_a_failed_recomposition_does_not_pre_delete_the_old_use() -> None:
    """The difference between "remove dining" and "replace dining with X"."""
    state = a_room(
        needs=(need("dining", "dining-table"), need()),
        lines=(line(10, need_index=1),),
    )

    result, _ = await run(
        CustomerAgentDecision(
            action=AgentAction.DESIGN_HANDOFF,
            design_revision=DesignRevisionIntent(
                removed_needs=(
                    DesignNeedCategoryMatch(
                        commerce_category="dining", commerce_subcategory="dining-table"
                    ),
                )
            ),
        ),
        state,
        available=(10,),
        design_error=LLMResponseInvalidError(reason="provider"),
    )

    after = room(result.state)
    assert [n.commerce_category for n in after.design_needs] == ["dining", "seating"]
    assert result.grounding.failure.code is TurnFailureCode.DESIGN_UNAVAILABLE


async def test_a_successful_recomposition_installs_the_whole_new_plan() -> None:
    state = a_room(
        needs=(need("dining", "dining-table"), need()),
        lines=(line(10, need_index=1),),
    )

    result, parts = await run(
        CustomerAgentDecision(
            action=AgentAction.DESIGN_HANDOFF,
            design_revision=DesignRevisionIntent(
                removed_needs=(
                    DesignNeedCategoryMatch(
                        commerce_category="dining", commerce_subcategory="dining-table"
                    ),
                )
            ),
        ),
        state,
        available=(10, 77),
        design=FakeDesign(
            plan_of(
                category_need("seating", "sofa"),
                category_need("seating", "lounge-chair"),
            )
        ),
        outcome=bundle_of((10, 0, "1000.00"), (77, 1, "500.00")),
    )

    assert parts["design"].requests[0].revision.excluded[0].commerce_subcategory == (
        "dining-table"
    )
    after = room(result.state)
    assert [n.commerce_category for n in after.design_needs] == ["seating", "seating"]


# ── I. keep this, and change the rest ═══════════════════════════════════════


async def test_a_preserved_piece_survives_into_the_recomposed_room() -> None:
    state = a_room(needs=(need(), need("decor", "carpet")), lines=(line(10),))
    original = room(state).bundle_items[0]

    result, _ = await run(
        CustomerAgentDecision(
            action=AgentAction.DESIGN_HANDOFF,
            design_revision=DesignRevisionIntent(
                preserved_items=(BundleItemOrdinal(ordinal=1),)
            ),
        ),
        state,
        available=(10, 77),
        design=FakeDesign(plan_of(category_need("lighting", "floor-lamp"))),
        outcome=bundle_of((77, 0, "500.00")),
    )

    kept = [i for i in room(result.state).bundle_items if i.product_id == 10]
    assert len(kept) == 1
    assert kept[0].line_id == original.line_id, "identity survives"
    assert kept[0].status is BundleItemStatus.LOCKED
    assert kept[0].need_id is None, "the role it filled no longer exists"


# ── failure: what survives, and what never happened ═════════════════════════


async def test_customer_facts_outlive_every_downstream_failure() -> None:
    """A budget they stated is theirs whatever the catalog did next."""
    from app.schemas.agent_decision import CustomerStateProposal, PriceProposal

    result, _ = await run(
        CustomerAgentDecision(
            action=AgentAction.DESIGN_HANDOFF,
            state_proposal=CustomerStateProposal(
                room_budget=PriceProposal(max_amount="12000", currency="SAR")
            ),
        ),
        AgentStateV1(),
        available=(),
        design_error=LLMResponseInvalidError(reason="provider"),
    )

    budget = room(result.state).budget
    assert budget is not None
    assert budget.max_amount == Decimal("12000")
    assert result.grounding.failure.code is TurnFailureCode.DESIGN_UNAVAILABLE


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("design", TurnFailureCode.DESIGN_UNAVAILABLE),
        ("discovery", TurnFailureCode.SEARCH_UNAVAILABLE),
    ],
)
async def test_an_existing_room_survives_a_failed_replan(
    failure: str, code: TurnFailureCode
) -> None:
    state = a_room(needs=(need(),), lines=(line(10),))
    kwargs: dict[str, Any] = (
        {"design_error": LLMResponseInvalidError(reason="provider")}
        if failure == "design"
        else {"discovery_error": CatalogUnavailableError()}
    )

    result, _ = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        state,
        available=(10,),
        **kwargs,
    )

    after = room(result.state)
    assert [n.commerce_subcategory for n in after.design_needs] == ["sofa"]
    assert [i.product_id for i in after.bundle_items] == [10]
    assert result.grounding.failure.code is code


async def test_an_invented_product_type_refuses_the_plan_and_keeps_the_room() -> None:
    """A taxonomy value nobody approved is a contract violation, not a room."""
    from tests.unit.test_design_revision import specialist

    state = a_room(needs=(need(),), lines=(line(10),))
    invented = plan_of(category_need("living-room-furniture", "luxury-couch"))

    result, _ = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        state,
        available=(10,),
        design=specialist(invented),
    )

    after = room(result.state)
    assert [n.commerce_subcategory for n in after.design_needs] == ["sofa"]
    assert result.grounding.failure.code is TurnFailureCode.DESIGN_UNAVAILABLE


async def test_a_stale_lock_stops_the_room_before_anything_is_planned() -> None:
    """A piece they asked to keep is never dropped, substituted or unlocked."""
    state = a_room(needs=(need(),), lines=(line(10, locked=True),))

    result, parts = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF), state, available=()
    )

    assert parts["design"].requests == [], "nothing is planned around a missing lock"
    assert result.grounding.failure.code is TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE
    assert [i.product_id for i in room(result.state).bundle_items] == [10]


async def test_an_infeasible_package_leaves_the_room_it_could_not_improve() -> None:
    state = a_room(needs=(need(),), lines=(line(10),))
    infeasible = RoomBundle(
        lines=(),
        status=BundleStatus.INFEASIBLE,
        new_spend_total=None,
        currency=None,
        total_unavailable="no_priced_lines",
    )

    result, _ = await run(
        CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        state,
        available=(10, 77),
        design=FakeDesign(plan_of(category_need("seating", "sofa"))),
        outcome=infeasible,
    )

    assert [i.product_id for i in room(result.state).bundle_items] == [10]


async def test_an_unrenderable_room_is_refused_rather_than_shown_short() -> None:
    """A card whose product the catalog will not return makes the room
    undescribable. Dropping it would renumber everything after it, so "the
    third one" in the reply would no longer be the third one they saw."""
    from app.services.bundle_presentation import build_state_bundle_presentation

    state = a_room(needs=(need(), need("decor", "carpet")), lines=(line(10), line(11)))

    with pytest.raises(BundlePresentationError):
        build_state_bundle_presentation(room(state), [product(10)])


async def test_a_changed_room_that_could_not_be_refreshed_tells_both_truths() -> None:
    """The lock happened and the refresh did not; neither may be hidden."""
    state = a_room(needs=(need(),), lines=(line(10),), budget="1500")

    result, _ = await run(
        refine(
            BundleInteractionOp.SET_ACQUISITION,
            acquisition=BundleAcquisition.ALREADY_OWNED,
        ),
        state,
        available=(10,),
        discovery_error=CatalogUnavailableError(),
    )

    assert (
        room(result.state).bundle_items[0].acquisition
        is BundleAcquisition.ALREADY_OWNED
    ), "what they told us about themselves survived"
    assert result.grounding.failure is not None, "and the refresh failure is reported"
    assert (
        route_response(result).primary.kind
        is DeterministicResponseKind.BUNDLE_CHANGED_NOT_REFRESHED
    )


# ── what the customer is told, and never told (M12F 7) ══════════════════════


def test_no_customer_wording_names_the_specialist_or_the_machinery() -> None:
    """One assistant, whatever runs underneath.

    The customer is never handed off, transferred, or told a second agent was
    consulted - the Customer/Commerce Agent is the only voice (CLAUDE.md 17.1).
    """
    import app.services.response_wording as wording

    phrases = [
        value
        for name, value in vars(wording).items()
        if name.isupper() and isinstance(value, str)
    ]
    assert phrases, "the wording module must actually expose wording"

    for phrase in phrases:
        lowered = phrase.lower()
        for forbidden in (
            "interior design",
            "specialist",
            "designer",
            "agent",
            "transfer",
            "handing",
            "handoff",
            "optimiser",
            "optimizer",
            "pinecone",
            "database",
        ):
            assert forbidden not in lowered, f"{forbidden!r} in {phrase!r}"


async def test_a_response_failure_never_undoes_what_the_turn_did() -> None:
    """Execution truth is committed before anything is worded.

    A room that was chosen, committed and can be described stays chosen even if
    wording it fails: the customer's room is not a function of whether a
    sentence was produced.
    """
    state = a_room(needs=(need(),), lines=(line(10),))

    result, _ = await run(
        refine(BundleInteractionOp.LOCK), state, available=(10,)
    )

    committed = room(result.state)
    assert committed.bundle_items[0].status is BundleItemStatus.LOCKED

    # The response layer reads the finished turn; it is handed no way to write.
    route = route_response(result)
    assert route.primary.kind is DeterministicResponseKind.BUNDLE_KEPT
    assert not hasattr(route, "state")
    assert room(result.state).bundle_items[0].status is BundleItemStatus.LOCKED
