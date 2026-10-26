"""The cart's rules, kept whichever agent adds: the store's own cart, its limits."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

from app.agents.operations.cart import CartOperations, room_pieces_to_buy
from app.agents.work import TurnWork
from app.core.exceptions import IntegrationUnavailableError
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_state import (
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    CartQuantity,
    ProductInteractionState,
    RoomProjectState,
)
from app.schemas.cart import CartRefusalReason
from app.schemas.discovery import DimensionConstraint, DimensionConstraintKind, ProductSearchRequest
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    ResolvedSearch,
)
from app.services.store_cart_check import CartCheck
from app.taxonomy.dimensions import DimensionRole

from tests.unit.agents.fakes import (
    CONTEXT,
    OFF_SCREEN,
    FakePipeline,
    agent_services,
    real_loop,
    tool_context,
)

LOOP = real_loop()
FIRST_CARD = {"which": {"kind": "presented_ordinal", "position": 1}}


class FakeCheck:
    """The store's own cart: takes `takes` of each, or cannot be asked."""

    def __init__(
        self,
        takes: int | None = None,
        reason: CartRefusalReason = CartRefusalReason.OUT_OF_STOCK,
        down: bool = False,
    ) -> None:
        self.takes, self.reason, self.down = takes, reason, down

    async def refusals(self, wanted: Any, context: Any) -> dict[int, CartCheck]:
        if self.down:
            raise IntegrationUnavailableError(reason="salla down")
        if self.takes is None:
            return {}
        return {
            pid: CartCheck(
                product_id=pid,
                name_english="Sofa",
                name_arabic=None,
                takes=self.takes,
                reason=self.reason,
            )
            for pid, count in wanted.items()
            if count > self.takes
        }


def _services(check: Any = None, *, max_picks: int = 10) -> Any:
    services = agent_services()
    return dataclasses.replace(
        services,
        cart_operations=CartOperations(
            max_picks=max_picks, max_quantity=10, max_likes=20, check=check
        ),
    )


async def _add(items: list[dict[str, Any]], state: AgentStateV1, services: Any) -> Any:
    tool = LOOP._registry.get("add_to_cart")
    assert tool is not None and tool.handler is not None
    return await tool.handler(
        tool.args.model_validate({"items": items}),
        tool_context(services),
        TurnWork.starting(state),
    )


def _cart(outcome: Any) -> ProductInteractionState:
    assert outcome.state is not None
    return outcome.state.product_interaction


async def test_a_piece_the_stores_cart_refuses_never_enters_ours() -> None:
    outcome = await _add([FIRST_CARD], AgentStateV1(), _services(FakeCheck(takes=0)))

    assert OFF_SCREEN not in _cart(outcome).selected_product_ids
    assert outcome.facts["refused_by_store"][0]["reason"] == "out_of_stock"


async def test_a_piece_the_store_could_not_confirm_stays_out() -> None:
    outcome = await _add([FIRST_CARD], AgentStateV1(), _services(FakeCheck(down=True)))

    assert OFF_SCREEN not in _cart(outcome).selected_product_ids
    assert outcome.facts["refused_by_store"][0]["reason"] == "unconfirmed"


async def test_the_stores_per_order_limit_is_kept() -> None:
    check = FakeCheck(takes=2, reason=CartRefusalReason.OVER_LIMIT)

    outcome = await _add([{**FIRST_CARD, "quantity": 5}], AgentStateV1(), _services(check))

    assert _cart(outcome).quantity_of(OFF_SCREEN) == 2


async def test_a_typed_quantity_is_capped_at_the_carts_limit() -> None:
    outcome = await _add([{**FIRST_CARD, "quantity": 20}], AgentStateV1(), _services())

    assert _cart(outcome).quantity_of(OFF_SCREEN) == 10


async def test_a_full_cart_takes_nothing_more() -> None:
    state = AgentStateV1(product_interaction=ProductInteractionState(selected_product_ids=(7,)))

    outcome = await _add([FIRST_CARD], state, _services(max_picks=1))

    assert outcome.code == "cart_full"
    assert _cart(outcome).selected_product_ids == (7,)


def test_adding_a_room_twice_never_doubles_it() -> None:
    room = RoomProjectState(
        room_type="living room",
        bundle_items=(
            BundleItemState(
                line_id=1,
                product_id=5,
                quantity=2,
                acquisition=BundleAcquisition.TO_BUY,
                status=BundleItemStatus.SUGGESTED,
            ),
        ),
        next_bundle_line_id=2,
    )
    in_cart = ProductInteractionState(
        selected_product_ids=(5,), cart_quantities=(CartQuantity(product_id=5, quantity=2),)
    )

    assert room_pieces_to_buy(AgentStateV1(room_project=room, product_interaction=in_cart)) == [
        (5, 2)
    ]


async def test_a_search_that_is_ours_saves_no_sizes() -> None:
    services = agent_services(pipeline=FakePipeline(ids=(10,)))
    resolved = ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sofa",
            dimensions=(
                DimensionConstraint(
                    role=DimensionRole.OVERALL_WIDTH,
                    kind=DimensionConstraintKind.MAX,
                    max_cm=Decimal("220"),
                ),
            ),
        ),
        semantics=ConstraintSemantics(
            dimensions=(
                DimensionConstraintSemantics(
                    role=DimensionRole.OVERALL_WIDTH, strength=ConstraintStrength.LOCKED
                ),
            )
        ),
    )

    ours = await services.search.new(
        resolved, AgentStateV1(), CONTEXT, alongside=False, asked=False
    )
    theirs = await services.search.new(resolved, AgentStateV1(), CONTEXT, alongside=False)

    assert ours.state.customer_preferences.measurements_by_type == ()
    assert theirs.state.customer_preferences.measurements_by_type != ()


async def test_a_seat_for_one_on_a_sofa_tells_the_agent_the_one_seat_types() -> None:
    from app.taxonomy.seating import load_seating_semantics

    from tests.unit.agents.fakes import TAXONOMY

    pipeline = FakePipeline(ids=(10,))
    services = dataclasses.replace(
        agent_services(pipeline=pipeline), seating=load_seating_semantics(taxonomy=TAXONOMY)
    )
    tool = LOOP._registry.get("search_products")
    assert tool is not None and tool.handler is not None
    search = {
        "commerce_category": "seating",
        "commerce_subcategory": "sofa",
        "seating_capacity_min": 1,
        "seating_capacity_max": 1,
    }

    outcome = await tool.handler(
        tool.args.model_validate({"search": search}),
        tool_context(services),
        TurnWork.starting(AgentStateV1()),
    )

    # A fact for the agent, never a refusal: it reads the customer and decides.
    assert outcome.facts["type_never_seats_one"] is True
    assert "single seater sofa" in outcome.facts["one_seat_types"]
    assert pipeline.calls != []


async def test_nora_judges_a_card_against_what_they_already_have() -> None:
    from tests.unit.agents.fakes import FakeHydration

    services = agent_services(hydration=FakeHydration(available=(OFF_SCREEN, 10, 11)))
    tool = LOOP._registry.get("measure_fit")
    assert tool is not None and tool.handler is not None
    state = AgentStateV1(
        product_interaction=ProductInteractionState(
            selected_product_ids=(10,), liked_product_ids=(11,)
        )
    )
    args = {
        "about": [{"kind": "presented_ordinal", "position": 1}],
        "alongside": ["cart", "liked"],
    }

    outcome = await tool.handler(
        tool.args.model_validate(args), tool_context(services), TurnWork.starting(state)
    )

    # The facts her judgement needs - the verdict is hers, no model is asked.
    assert len(outcome.facts["products"]) == 1
    assert [j["where"] for j in outcome.facts["already_theirs"]] == ["in their cart", "liked"]
    assert outcome.facts["room_size_known"] is False
