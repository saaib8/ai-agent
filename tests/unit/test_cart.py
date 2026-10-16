"""The cart: the customer's picks with how many of each, priced fresh.

Selecting a product adds it to the cart, whatever route selected it, so the
checks here are about what the cart adds on top of the picks: quantities that
only ever name a product in it, totals summed in code per currency, the changes
a client copies into the store's own cart, and every route - a tap, a quantity,
a typed choice with a number, a seating combination, a whole room - landing in
the same place (CLAUDE.md 36).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from app.api.dependencies import picks_runtime, retailer_context_provider
from app.api.errors import register_exception_handlers
from app.api.routes.cart import router as cart_router
from app.core.config import CustomerAgentSettings
from app.core.exceptions import CartQuantityError, PickUnavailableError
from app.repositories.products import ProductRepository, StoreProductIdentity
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    ProductInteractionIntent,
    ProductInteractionOp,
)
from app.schemas.agent_state import (
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    CartQuantity,
    ProductInteractionState,
    RoomProjectState,
)
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.agent_updates import (
    AgentStateUpdate,
    ProductInteractionUpdate,
    RemoveItems,
    SetCartQuantity,
)
from app.schemas.bundle_action import AddRoomToCartAction
from app.schemas.cart import CartQuery
from app.schemas.language import ReplyLanguage
from app.schemas.picks import (
    DeselectPickAction,
    PicksRequest,
    PickView,
    SetQuantityAction,
)
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.retailer import RetailerContext
from app.services.agent_state import apply_update, commit_search_results
from app.services.cart import CartService
from app.services.picks import PicksRuntime
from app.services.reference_resolver import ProductReferenceResolver
from app.taxonomy.attributes import load_catalog_attributes
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from tests.unit.test_chat_api import FakeRetailers, FakeSessionStore
from tests.unit.test_picks import STORE, Hydration, _state, _stored
from tests.unit.test_reference_resolver import FakeRepository, _row
from tests.unit.test_turn_coordinator import FakeHydration, _coordinator

CONTEXT = RetailerContext(store_id=STORE)
SESSION = "sess-1"


class Identities:
    """`ProductRepository.store_identities`, from memory."""

    def __init__(self, gone: tuple[int, ...] = ()) -> None:
        self.gone = gone

    async def store_identities(self, product_ids: Any, context: Any) -> list[StoreProductIdentity]:
        return [
            StoreProductIdentity(
                product_id=p,
                uuid=f"00000000-0000-0000-0000-{p:012d}",
                salla_product_id=str(9000 + p) if p % 2 else None,
                product_url=f"https://store.test/p/{p}",
                name_english=f"Sofa {p}",
                name_arabic="كنبة",
            )
            for p in product_ids
            if p not in self.gone
        ]


def _cart(gone: tuple[int, ...] = ()) -> CartService:
    return CartService(cast(ProductRepository, Identities(gone)))


def _pick(position: int, product_id: int, *, price: str = "2000", unit: str = "SAR") -> PickView:
    return PickView(
        pick=position,
        name_english=f"Sofa {product_id}",
        image_url=f"https://example.test/{product_id}.jpg",
        price_amount=Decimal(price),
        price_unit=unit,
        kind="sofa",
    )


def _with_cart(picks: tuple[int, ...], quantities: dict[int, int] | None = None) -> AgentStateV1:
    state = _state(picks=picks)
    return state.model_copy(
        update={
            "product_interaction": ProductInteractionState.model_validate(
                {
                    **state.product_interaction.model_dump(),
                    "cart_quantities": [
                        {"product_id": p, "quantity": q} for p, q in (quantities or {}).items()
                    ],
                }
            )
        }
    )


# ── the state ───────────────────────────────────────────────────────────────


def test_a_quantity_names_a_product_in_the_cart() -> None:
    with pytest.raises(ValidationError):
        ProductInteractionState(
            selected_product_ids=(101,),
            cart_quantities=(CartQuantity(product_id=102, quantity=2),),
        )
    with pytest.raises(ValidationError):
        ProductInteractionState(
            selected_product_ids=(101,),
            cart_quantities=(
                CartQuantity(product_id=101, quantity=2),
                CartQuantity(product_id=101, quantity=3),
            ),
        )


def test_one_is_the_default_and_never_stored() -> None:
    with pytest.raises(ValidationError):
        CartQuantity(product_id=101, quantity=1)
    interaction = ProductInteractionState(selected_product_ids=(101,))

    assert interaction.quantity_of(101) == 1
    # A session that never set a quantity is written exactly as before.
    assert "cart_quantities" not in interaction.model_dump()


def test_setting_a_quantity_and_setting_it_back_to_one() -> None:
    state = _state(picks=(101, 102))
    two = apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                cart_quantities=(SetCartQuantity(product_id=102, quantity=2),)
            )
        ),
    )
    one = apply_update(
        two,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                cart_quantities=(SetCartQuantity(product_id=102, quantity=1),)
            )
        ),
    )

    assert two.product_interaction.quantity_of(102) == 2
    assert one.product_interaction.cart_quantities == ()


def test_a_product_leaving_the_cart_takes_its_quantity_with_it() -> None:
    state = _with_cart((101, 102), {102: 3})
    removed = apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                selected_product_ids=RemoveItems(items=(102,))
            )
        ),
    )

    assert removed.product_interaction.cart_quantities == ()


def test_new_results_keep_the_cart() -> None:
    state = _with_cart((101,), {101: 4})

    after = commit_search_results(state, (201, 202))

    assert after.product_interaction.quantity_of(101) == 4


# ── pricing it ──────────────────────────────────────────────────────────────


async def test_lines_carry_quantities_line_totals_and_the_stores_own_ids() -> None:
    state = _with_cart((101, 102), {101: 2})

    cart = await _cart().view(state, (_pick(1, 101), _pick(2, 102)), CONTEXT, ReplyLanguage.EN)

    assert [(line.pick, line.quantity, line.line_total) for line in cart.lines] == [
        (1, 2, Decimal("4000")),
        (2, 1, Decimal("2000")),
    ]
    assert cart.item_count == 3
    assert [(t.price_unit, t.amount, t.display) for t in cart.totals] == [
        ("SAR", Decimal("6000"), "6,000 SAR")
    ]
    assert cart.lines[0].product.salla_id == "9101"
    assert cart.lines[1].product.salla_id is None
    assert cart.lines[0].product.url == "https://store.test/p/101"
    assert cart.changes == (), "a plain read reports nothing changed"


async def test_totals_are_never_summed_across_currencies() -> None:
    state = _state(picks=(101, 102))
    picks = (_pick(1, 101), _pick(2, 102, price="300", unit="USD"))

    cart = await _cart().view(state, picks, CONTEXT, ReplyLanguage.EN)

    assert [(t.price_unit, t.amount) for t in cart.totals] == [
        ("SAR", Decimal("2000")),
        ("USD", Decimal("300")),
    ]


async def test_totals_are_worded_in_the_conversations_language() -> None:
    state = _state(picks=(101,))

    cart = await _cart().view(state, (_pick(1, 101),), CONTEXT, ReplyLanguage.AR)

    assert cart.totals[0].display == "2,000 ريال"


async def test_a_product_the_catalog_no_longer_returns_is_counted_not_priced() -> None:
    state = _state(picks=(101, 102))

    # The picks read left 102 out; its number stays a gap.
    cart = await _cart().view(state, (_pick(1, 101),), CONTEXT, ReplyLanguage.EN)

    assert [line.pick for line in cart.lines] == [1]
    assert cart.unavailable == 1
    assert cart.totals[0].amount == Decimal("2000")


async def test_changes_say_what_to_copy_into_the_stores_cart() -> None:
    before = _with_cart((101, 102), {102: 2})
    after = _with_cart((101, 103), {101: 3})
    picks = (_pick(1, 101), _pick(2, 103))

    cart = await _cart().view(after, picks, CONTEXT, ReplyLanguage.EN, before=before)

    assert [
        (change.product.uuid[-3:], change.quantity_before, change.quantity_after)
        for change in cart.changes
    ] == [("101", 1, 3), ("103", 0, 1), ("102", 2, 0)]


# ── the tray: quantities by tap ─────────────────────────────────────────────


def _runtime(sessions: FakeSessionStore, **settings: Any) -> PicksRuntime:
    repository = FakeRepository([_row(p) for p in (101, 102, 103, 104)])
    return PicksRuntime(
        ProductReferenceResolver(cast(ProductRepository, repository), load_catalog_attributes()),
        Hydration(),  # type: ignore[arg-type]
        sessions,  # type: ignore[arg-type]
        CustomerAgentSettings(**settings),
        cart=_cart(),
    )


def _quantity(pick: int, quantity: int) -> PicksRequest:
    return PicksRequest(
        session_id=SESSION,
        store_id=STORE,
        action=SetQuantityAction(pick=pick, quantity=quantity),
    )


async def test_a_quantity_tap_changes_the_cart_and_says_so() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=(101, 102)))

    response = await _runtime(sessions).apply(_quantity(2, 3), CONTEXT)

    assert response.cart is not None
    assert [line.quantity for line in response.cart.lines] == [1, 3]
    assert response.cart.item_count == 4
    assert [(c.quantity_before, c.quantity_after) for c in response.cart.changes] == [(1, 3)]
    assert response.goes_with is None, "a quantity is not a new pick"


async def test_the_same_quantity_commits_nothing() -> None:
    sessions = FakeSessionStore()
    envelope = await _stored(sessions, _state(picks=(101,)))

    response = await _runtime(sessions).apply(_quantity(1, 1), CONTEXT)

    assert response.session_revision == envelope.session_revision
    assert response.cart is not None and response.cart.changes == ()


async def test_more_than_the_cart_takes_is_refused() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=(101,)))

    with pytest.raises(CartQuantityError) as raised:
        await _runtime(sessions, cart_max_quantity=5).apply(_quantity(1, 6), CONTEXT)

    assert "up to 5" in raised.value.public_message


async def test_a_quantity_for_a_line_not_in_the_cart_is_refused() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=(101,)))

    with pytest.raises(PickUnavailableError):
        await _runtime(sessions).apply(_quantity(2, 2), CONTEXT)


async def test_removing_a_line_reports_it_leaving() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101, 102), {102: 2}))

    response = await _runtime(sessions).apply(
        PicksRequest(session_id=SESSION, store_id=STORE, action=DeselectPickAction(pick=2)),
        CONTEXT,
    )

    assert response.cart is not None
    assert [(c.quantity_before, c.quantity_after) for c in response.cart.changes] == [(2, 0)]


async def test_reading_a_cart_that_has_not_started_is_empty_not_an_error() -> None:
    response = await _runtime(FakeSessionStore()).read_cart(
        CartQuery(session_id="new-one", store_id=STORE), CONTEXT
    )

    assert response.session_revision == 0
    assert response.cart.lines == () and response.cart.totals == ()


# ── the route ───────────────────────────────────────────────────────────────


async def test_the_route_reads_the_cart_and_changes_nothing() -> None:
    sessions = FakeSessionStore()
    envelope = await _stored(sessions, _with_cart((101,), {101: 2}))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(cart_router, prefix="/v1")
    app.dependency_overrides[picks_runtime] = lambda: _runtime(sessions)
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        answer = await client.get("/v1/cart", params={"session_id": SESSION, "store_id": STORE})

    assert answer.status_code == 200
    body = answer.json()
    assert body["session_revision"] == envelope.session_revision
    assert body["cart"]["item_count"] == 2
    assert body["cart"]["lines"][0]["product"]["salla_id"] == "9101"
    assert "product_id" not in answer.text
    assert (await sessions.load(STORE, SESSION)) == envelope


# ── a turn of the conversation ──────────────────────────────────────────────


def _room() -> RoomProjectState:
    def line(line_id: int, product_id: int, quantity: int, owned: bool = False) -> Any:
        return BundleItemState(
            line_id=line_id,
            product_id=product_id,
            quantity=quantity,
            acquisition=BundleAcquisition.ALREADY_OWNED if owned else BundleAcquisition.TO_BUY,
            status=BundleItemStatus.LOCKED if owned else BundleItemStatus.SUGGESTED,
        )

    return RoomProjectState(
        room_type="living_room",
        bundle_items=(line(1, 10, 1), line(2, 11, 2), line(3, 12, 1, owned=True)),
        next_bundle_line_id=4,
    )


async def test_add_all_to_cart_adds_the_pieces_being_bought_with_their_quantities() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        hydration=FakeHydration(available=(10, 11, 12, 42)),
    )
    state = _with_cart((42,)).model_copy(update={"room_project": _room()})

    result = await coordinator.run(
        CustomerTurnInput(
            message="Add all of these to my cart",
            state=state,
            context=CONTEXT,
            bundle_action=AddRoomToCartAction(),
        )
    )

    interaction = result.state.product_interaction
    assert interaction.selected_product_ids == (42, 10, 11)
    assert interaction.quantity_of(11) == 2
    assert 12 not in interaction.selected_product_ids, "a piece they own is not bought again"
    assert result.selection_added


async def test_add_all_twice_never_doubles_a_piece() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        hydration=FakeHydration(available=(10, 11, 12)),
    )
    state = _with_cart((10, 11), {11: 2}).model_copy(update={"room_project": _room()})

    result = await coordinator.run(
        CustomerTurnInput(
            message="Add all of these to my cart",
            state=state,
            context=CONTEXT,
            bundle_action=AddRoomToCartAction(),
        )
    )

    assert result.state.product_interaction == state.product_interaction
    assert not result.selection_added


async def test_add_all_without_a_room_changes_nothing() -> None:
    coordinator, _ = _coordinator(CustomerAgentDecision(action=AgentAction.ANSWER))
    state = _with_cart((42,))

    result = await coordinator.run(
        CustomerTurnInput(
            message="Add all of these to my cart",
            state=state,
            context=CONTEXT,
            bundle_action=AddRoomToCartAction(),
        )
    )

    assert result.state.product_interaction == state.product_interaction
    assert result.grounding.failure is not None


async def test_typed_add_everything_puts_the_room_in_the_cart() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER, add_room_to_cart=True),
        hydration=FakeHydration(available=(10, 11, 12, 42)),
    )
    state = _with_cart((42,)).model_copy(update={"room_project": _room()})

    result = await coordinator.run(
        CustomerTurnInput(message="add everything to my cart", state=state, context=CONTEXT)
    )

    assert result.state.product_interaction.selected_product_ids == (42, 10, 11)
    assert result.state.product_interaction.quantity_of(11) == 2


async def test_a_typed_choice_with_a_number_sets_how_many() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT,
                reference=PresentedOrdinal(position=1),
                quantity=3,
            ),
        ),
    )

    result = await coordinator.run(
        CustomerTurnInput(message="add 3 of the first one", state=_with_cart(()), context=CONTEXT)
    )

    interaction = result.state.product_interaction
    assert 42 in interaction.selected_product_ids
    assert interaction.quantity_of(42) == 3


async def test_a_typed_quantity_is_capped_at_what_the_cart_takes() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT,
                reference=PresentedOrdinal(position=1),
                quantity=60,
            ),
        ),
    )

    result = await coordinator.run(
        CustomerTurnInput(message="60 of these", state=_with_cart(()), context=CONTEXT)
    )

    assert result.state.product_interaction.quantity_of(42) == 10


# ── what the reply may say ──────────────────────────────────────────────────


async def test_the_reply_may_repeat_the_total_it_shows_and_nothing_worked_out_from_it() -> None:
    from app.services.numeric_guard import (
        build_allowance,
        cart_counts,
        cart_figures,
        check_numeric_policy,
    )

    state = _with_cart((101, 102), {101: 2})
    cart = await _cart().view(state, (_pick(1, 101), _pick(2, 102)), CONTEXT, ReplyLanguage.EN)
    allowance = build_allowance(
        "what's my total? my budget is 9000",
        counts=cart_counts(cart),
        figures=cart_figures(cart),
    )

    said = "Your cart comes to 6,000 SAR for 3 items."
    worked_out = "That leaves 3,000 SAR of your budget."
    assert check_numeric_policy(message=said, follow_up_question=None, allowance=allowance) is None
    assert (
        check_numeric_policy(message=worked_out, follow_up_question=None, allowance=allowance)
        is not None
    )


# ── "the first sofa" after the screen moved on ──────────────────────────────


class KindHydration:
    """Products of the kinds named, so a list of lamps can sit on top of sofas."""

    def __init__(self, kinds: dict[int, str]) -> None:
        self.kinds = kinds

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[Any, ...]:
        from tests.unit.test_turn_coordinator import _product

        return tuple(_product(p, subcategory=self.kinds[p]) for p in product_ids if p in self.kinds)


SOFAS_THEN_A_LAMP = {10: "sectional-sofa", 11: "sofa", 20: "floor-lamp"}


def _lamp_over_sofas() -> AgentStateV1:
    """Sofas were shown, one was picked, and what goes with it - a floor
    lamp - is the newest list on screen."""
    from app.schemas.agent_state import ActiveSearchState, PresentedList
    from app.schemas.discovery import ProductSearchRequest

    return AgentStateV1(
        active_search=ActiveSearchState(
            request=ProductSearchRequest(commerce_category="lighting"), revision=2
        ),
        product_interaction=ProductInteractionState(
            presented_product_ids=(20,),
            presented_search_revision=2,
            earlier_lists=(PresentedList(revision=1, product_ids=(10, 11)),),
            selected_product_ids=(11,),
        ),
    )


def _add_first(expected: str | None, quantity: int | None = 2) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.ANSWER,
        interaction=ProductInteractionIntent(
            op=ProductInteractionOp.SELECT,
            reference=PresentedOrdinal(position=1),
            expected_subcategory=expected,
            quantity=quantity,
        ),
    )


def _lamp_coordinator(decision: CustomerAgentDecision) -> Any:
    from app.schemas.resolution import ResolvedProductReference
    from app.taxonomy.compare_groups import load_compare_groups

    from tests.unit.test_turn_coordinator import FakeReferences

    coordinator, _ = _coordinator(
        decision,
        references=FakeReferences(default=ResolvedProductReference(product_id=20)),
        hydration=KindHydration(SOFAS_THEN_A_LAMP),  # type: ignore[arg-type]
        compare_groups=load_compare_groups(),
    )
    return coordinator


async def test_the_first_sofa_reaches_the_sofas_under_the_suggestions() -> None:
    result = await _lamp_coordinator(_add_first("sofa")).run(
        CustomerTurnInput(
            message="add 2 of the first sofa", state=_lamp_over_sofas(), context=CONTEXT
        )
    )

    interaction = result.state.product_interaction
    assert 20 not in interaction.selected_product_ids, "not the lamp on top"
    assert interaction.quantity_of(10) == 2, "the first sofa - a sectional is a sofa"


async def test_a_kind_on_no_list_still_asks() -> None:
    state = _lamp_over_sofas()

    result = await _lamp_coordinator(_add_first("bed")).run(
        CustomerTurnInput(message="add the first bed", state=state, context=CONTEXT)
    )

    assert result.state.product_interaction.selected_product_ids == (11,)
    assert result.grounding.deterministic_clarification is not None


async def test_a_bare_position_means_the_newest_cards() -> None:
    result = await _lamp_coordinator(_add_first(None)).run(
        CustomerTurnInput(
            message="add 2 of the first one", state=_lamp_over_sofas(), context=CONTEXT
        )
    )

    assert result.state.product_interaction.quantity_of(20) == 2


async def test_the_reply_is_told_what_was_already_in_the_cart() -> None:
    from app.schemas.agent_turn import CustomerTurnResult, TurnGrounding
    from app.services.response_view import _cart_facts

    state = _with_cart((101, 102), {102: 2})
    state = state.model_copy(
        update={
            "product_interaction": state.product_interaction.model_copy(
                update={"focused_product_id": 102}
            )
        }
    )
    cart = await _cart().view(state, (_pick(1, 101), _pick(2, 102)), CONTEXT, ReplyLanguage.EN)
    asked_again = CustomerTurnResult(
        state=state,
        decision=_add_first("sofa"),
        grounding=TurnGrounding(),
        cart=cart,
    )

    facts = _cart_facts(asked_again)

    assert facts is not None and not facts.changed
    assert facts.already_there is not None
    assert (facts.already_there.name, facts.already_there.quantity_after) == ("Sofa 102", 2)
