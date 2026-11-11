"""What a shopper turned down stays out; what they saw can be found again.

"Not this one" is remembered: every later search - Sara's, a page, a room piece -
leaves it out, until they like it, add it or ask to see it again. What was
shown to them is kept, newest last, so "the lamp I saw before" is found again.
Both are capped, and both travel with the shopper's cart from chat to chat
(CLAUDE.md 10.2, 36).
"""

from __future__ import annotations

from typing import Any, cast

from app.agents.operations.search import SearchRunner
from app.agents.work import TurnWork
from app.schemas.agent_state import (
    MAX_SEEN,
    MAX_TURNED_DOWN,
    AgentStateV1,
    ProductInteractionState,
)
from app.schemas.agent_updates import (
    AddItems,
    AgentStateUpdate,
    ProductInteractionUpdate,
)
from app.schemas.product_reference import SeenBefore, TurnedDownProduct
from app.schemas.resolution import (
    ReferenceFailureReason,
    ReferenceUnresolved,
    ResolvedProductReference,
)
from app.schemas.session import ShopperCart
from app.services.agent_state import apply_update, commit_search_results, take_back, turn_down
from app.services.conversation_adoption import merged_cart
from app.services.refinement_composer import SearchRefinementComposer
from app.services.shopper_cart import with_shopper_cart

from tests.unit.agents.fakes import (
    ATTRIBUTES,
    CONTEXT,
    DIMENSIONS,
    SOFAS,
    FakeHydration,
    FakePipeline,
    FakeReferences,
    _resolved,
    _state,
    agent_services,
    real_loop,
    tool_context,
)

# ── the lists ────────────────────────────────────────────────────────────────


def test_turning_down_keeps_the_newest_once_and_caps_the_list() -> None:
    state = AgentStateV1()
    for product_id in range(1, MAX_TURNED_DOWN + 3):
        state = turn_down(state, product_id)
    state = turn_down(state, 5)

    turned = state.product_interaction.turned_down_product_ids
    assert len(turned) == MAX_TURNED_DOWN
    assert turned[-1] == 5 and turned.count(5) == 1
    assert 1 not in turned  # the oldest went first


def test_liking_or_adding_takes_the_no_back() -> None:
    state = turn_down(turn_down(AgentStateV1(), 7), 8)

    liked = apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(liked_product_ids=AddItems(items=(7,)))
        ),
    )

    assert liked.product_interaction.turned_down_product_ids == (8,)
    assert take_back(liked, (8,)).product_interaction.turned_down_product_ids == ()


def test_what_is_shown_is_seen_newest_last_and_capped() -> None:
    state = _state(presented=(10, 11))
    for page in range(MAX_SEEN // 5 + 2):
        state = commit_search_results(state, tuple(range(100 + page * 5, 105 + page * 5)))

    seen = state.product_interaction.seen_product_ids
    assert len(seen) == MAX_SEEN
    assert seen[-5:] == state.product_interaction.presented_product_ids


# ── searches leave them out ─────────────────────────────────────────────────


def _runner(pipeline: FakePipeline) -> SearchRunner:
    return SearchRunner(
        cast(Any, pipeline), SearchRefinementComposer(ATTRIBUTES, DIMENSIONS, None), None
    )


async def test_a_search_runs_without_what_they_turned_down() -> None:
    pipeline = FakePipeline(ids=(10, 11))
    state = turn_down(_state(), 42)

    run = await _runner(pipeline).new(_resolved(), state, CONTEXT, alongside=False)

    assert 42 in pipeline.calls[-1].request.exclude_product_ids
    # The search kept on record is the one they asked for, so stating it again
    # is still its next page.
    assert run.state.active_search is not None
    assert 42 not in run.state.active_search.request.exclude_product_ids


async def test_not_this_one_is_remembered_for_later_searches() -> None:
    tool = real_loop()._registry.get("leave_out_product")
    assert tool is not None and tool.handler is not None
    pipeline = FakePipeline(ids=(12, 13))
    services = agent_services(
        pipeline=pipeline,
        hydration=FakeHydration(available=(10, 11, 12, 13)),
        references=FakeReferences(default=ResolvedProductReference(product_id=11)),
    )

    outcome = await tool.handler(
        tool.args.model_validate({"which": {"kind": "presented_ordinal", "position": 2}}),
        tool_context(services),
        TurnWork.starting(_state(request=SOFAS)),
    )

    assert outcome.state.product_interaction.turned_down_product_ids == (11,)


async def test_asking_to_see_one_again_takes_it_back() -> None:
    tool = real_loop()._registry.get("product_details")
    assert tool is not None and tool.handler is not None
    services = agent_services(
        hydration=FakeHydration(available=(42,)),
        references=FakeReferences(default=ResolvedProductReference(product_id=42)),
    )

    outcome = await tool.handler(
        tool.args.model_validate({"which": {"kind": "turned_down_product"}}),
        tool_context(services),
        TurnWork.starting(turn_down(_state(), 42)),
    )

    assert outcome.facts["no_longer_turned_down"] is True
    assert outcome.state.product_interaction.turned_down_product_ids == ()
    assert outcome.state.product_interaction.focused_product_id == 42


# ── naming them ─────────────────────────────────────────────────────────────


async def test_turned_down_and_seen_are_named_in_the_order_they_happened() -> None:
    from app.services.reference_resolver import ProductReferenceResolver

    from tests.unit.test_reference_resolver import FakeRepository, _row

    resolver = ProductReferenceResolver(
        cast(Any, FakeRepository([_row(n) for n in (1, 2, 3)])), ATTRIBUTES
    )
    state = AgentStateV1(
        product_interaction=ProductInteractionState(
            turned_down_product_ids=(1, 2), seen_product_ids=(1, 2, 3)
        )
    )
    from tests.unit.test_reference_resolver import CONTEXT as STORE_50

    context = STORE_50

    assert await resolver.resolve(TurnedDownProduct(position=1), state, context) == (
        ResolvedProductReference(product_id=1)
    )
    assert await resolver.resolve(SeenBefore(position=2), state, context) == (
        ResolvedProductReference(product_id=2)
    )
    several = await resolver.resolve(TurnedDownProduct(), state, context)
    assert isinstance(several, ReferenceUnresolved)
    assert several.reason is ReferenceFailureReason.SEVERAL_TURNED_DOWN_PRODUCTS


# ── from chat to chat ───────────────────────────────────────────────────────


def test_a_new_chat_starts_with_what_they_turned_down_and_saw() -> None:
    cart = ShopperCart(turned_down_product_ids=(5,), seen_product_ids=(5, 6))

    state = with_shopper_cart(AgentStateV1(), cart, new_conversation=True)

    assert state.product_interaction.turned_down_product_ids == (5,)
    assert state.product_interaction.seen_product_ids == (5, 6)


def test_an_older_record_leaves_the_chats_own() -> None:
    own = turn_down(AgentStateV1(), 9)

    state = with_shopper_cart(own, ShopperCart(selected_product_ids=()), new_conversation=False)

    assert state.product_interaction.turned_down_product_ids == (9,)


def test_signing_in_keeps_what_the_guest_turned_down_and_saw() -> None:
    account = ShopperCart(turned_down_product_ids=(1,), seen_product_ids=(1,))
    guest = ShopperCart(turned_down_product_ids=(2,), seen_product_ids=(2, 3))

    merged, _ = merged_cart(account, guest)

    assert merged.turned_down_product_ids == (1, 2)
    assert merged.seen_product_ids == (1, 2, 3)


# ── room pieces ─────────────────────────────────────────────────────────────


def test_a_room_piece_is_never_filled_with_one_they_turned_down() -> None:
    from app.services.design_discovery import DesignDiscoveryService

    from tests.unit.test_design_discovery import TAXONOMY, _need, _request

    service = DesignDiscoveryService(cast(Any, FakePipeline()), TAXONOMY)

    resolved = service._resolve(_need(), _request(), None, (41, 42))

    assert set(resolved.request.exclude_product_ids) >= {41, 42}
