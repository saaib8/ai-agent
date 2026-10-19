"""The store's own cart is asked before ours holds a product (CLAUDE.md 36).

Salla's public storefront API says whether a product is the store's, in stock,
and how many one order may take. What it refuses never stays in our cart, so
the reply never says "it's in your cart" about something the store would turn
away - and a Salla that cannot be asked never blocks the cart.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
from app.core.config import SallaSettings
from app.core.exceptions import PickUnavailableError
from app.integrations.salla_storefront import SallaProduct, SallaStorefrontClient
from app.repositories.products import ProductRepository, StoreProductIdentity
from app.repositories.stores import StoreRepository
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    ProductInteractionIntent,
    ProductInteractionOp,
)
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.card_comparison import CardRef
from app.schemas.cart import CartRefusalReason
from app.schemas.next_step import NextStepKind
from app.schemas.picks import PicksRequest, SelectPickAction, SetQuantityAction
from app.schemas.product_action import MoreLikeThisAction
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.resolution import ResolvedProductReference
from app.schemas.retailer import RetailerContext
from app.services.store_cart_check import CartCheck, StoreCartCheck

from tests.unit.test_cart import (
    SOFAS_THEN_A_LAMP,
    KindHydration,
    _cart,
    _runtime,
    _two_sofas_and_a_lamp,
    _with_cart,
)
from tests.unit.test_chat_api import FakeSessionStore
from tests.unit.test_picks import STORE, _state, _stored
from tests.unit.test_turn_coordinator import FakeReferences, _coordinator

CONTEXT = RetailerContext(store_id=STORE)
SALLA_STORE = 306242292


# ── Salla's storefront API ──────────────────────────────────────────────────


def _salla(handler: Any, *, ttl: int = 60) -> SallaStorefrontClient:
    return SallaStorefrontClient(
        SallaSettings(cache_ttl_s=ttl), transport=httpx.MockTransport(handler)
    )


def _details(**data: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": 200, "success": True, "data": data})


async def test_an_available_product_with_a_per_order_limit() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _details(is_available=True, is_out_of_stock=False, max_quantity=3)

    answer = await _salla(handler).product(SALLA_STORE, "1228105825")

    assert answer == SallaProduct(in_store=True, available=True, max_quantity=3)
    assert seen[0].url.path.endswith("/products/1228105825/details")
    assert seen[0].headers["Store-Identifier"] == str(SALLA_STORE)


async def test_out_of_stock_and_not_in_the_store() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "111" in request.url.path:
            return _details(is_available=False, is_out_of_stock=True)
        return httpx.Response(410, json={"status": 410, "success": False})

    salla = _salla(handler)

    assert (await salla.product(SALLA_STORE, "111")) == SallaProduct(
        in_store=True, available=False, max_quantity=None
    )
    assert (await salla.product(SALLA_STORE, "222")) == SallaProduct(
        in_store=False, available=False, max_quantity=None
    )


async def test_a_salla_that_cannot_be_asked_is_unknown_never_a_refusal() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="maintenance")

    assert await _salla(down).product(SALLA_STORE, "123") is None
    assert await _salla(broken).product(SALLA_STORE, "123") is None


async def test_a_product_is_asked_once_within_the_cache_time() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _details(is_available=True)

    salla = _salla(handler)
    await salla.product(SALLA_STORE, "123")
    await salla.product(SALLA_STORE, "123")

    assert len(calls) == 1


async def test_an_id_that_is_not_a_salla_id_is_never_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request")

    answer = await _salla(handler).product(SALLA_STORE, "../admin")

    assert answer is not None and not answer.in_store


# ── the check ───────────────────────────────────────────────────────────────


class Stores:
    def __init__(self, salla_store_id: int | None) -> None:
        self.salla_store_id_value = salla_store_id

    async def salla_store_id(self, store_id: int) -> int | None:
        return self.salla_store_id_value


class Identities:
    def __init__(self, salla_ids: dict[int, str | None]) -> None:
        self.salla_ids = salla_ids

    async def store_identities(self, product_ids: Any, context: Any) -> list[StoreProductIdentity]:
        return [
            StoreProductIdentity(
                product_id=p,
                uuid=f"uuid-{p}",
                salla_product_id=self.salla_ids[p],
                product_url=f"https://store.test/p/{p}",
                name_english=f"Product {p}",
                name_arabic=None,
            )
            for p in product_ids
            if p in self.salla_ids
        ]


class Answers:
    def __init__(self, answers: dict[str, SallaProduct | None]) -> None:
        self.answers = answers

    async def product(self, salla_store_id: int, salla_product_id: str) -> SallaProduct | None:
        return self.answers.get(salla_product_id)


def _check(
    salla_ids: dict[int, str | None],
    answers: dict[str, SallaProduct | None],
    *,
    salla_store: int | None = SALLA_STORE,
) -> StoreCartCheck:
    return StoreCartCheck(
        cast(StoreRepository, Stores(salla_store)),
        cast(ProductRepository, Identities(salla_ids)),
        cast(SallaStorefrontClient, Answers(answers)),
    )


IN_STOCK = SallaProduct(in_store=True, available=True, max_quantity=3)
GONE = SallaProduct(in_store=False, available=False, max_quantity=None)


async def test_each_reason_the_store_refuses() -> None:
    check = _check(
        {1: "11", 2: "22", 3: "33", 4: None, 5: "55"},
        {
            "11": IN_STOCK,
            "22": GONE,
            "33": SallaProduct(in_store=True, available=False, max_quantity=None),
            "55": None,
        },
    )

    refused = await check.refusals({1: 5, 2: 1, 3: 1, 4: 1, 5: 1}, CONTEXT)

    assert {pid: (c.reason, c.takes) for pid, c in refused.items()} == {
        1: (CartRefusalReason.OVER_LIMIT, 3),
        2: (CartRefusalReason.NOT_IN_STORE, 0),
        3: (CartRefusalReason.OUT_OF_STOCK, 0),
        4: (CartRefusalReason.NOT_IN_STORE, 0),
    }, "5 could not be asked, so it is not refused"


async def test_a_store_not_on_salla_is_never_asked() -> None:
    check = _check({1: "11"}, {"11": GONE}, salla_store=None)

    assert await check.refusals({1: 1}, CONTEXT) == {}


# ── every way of adding ─────────────────────────────────────────────────────


class Refuses:
    """A store cart that turns away the products named, as told."""

    def __init__(self, refused: dict[int, tuple[CartRefusalReason, int, int | None]]) -> None:
        self.refused = refused
        self.asked: list[dict[int, int]] = []

    async def refusals(self, wanted: Any, context: Any) -> dict[int, CartCheck]:
        self.asked.append(dict(wanted))
        return {
            pid: CartCheck(
                product_id=pid,
                name_english=f"Product {pid}",
                name_arabic=None,
                takes=takes,
                reason=reason,
                max_quantity=limit,
                product_url=f"https://example.test/{pid}",
            )
            for pid, (reason, takes, limit) in self.refused.items()
            if pid in wanted
        }


def _adding(product_id: int, refuses: Refuses, quantity: int | None = None) -> Any:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT,
                reference=PresentedOrdinal(position=1),
                quantity=quantity,
            ),
        ),
        references=FakeReferences(default=ResolvedProductReference(product_id=product_id)),
        hydration=KindHydration(SOFAS_THEN_A_LAMP),  # type: ignore[arg-type]
        cart=_cart(),
    )
    coordinator._cart_check = refuses  # type: ignore[assignment]
    return coordinator


async def test_a_typed_add_the_store_refuses_never_stays_in_the_cart() -> None:
    refuses = Refuses({20: (CartRefusalReason.NOT_IN_STORE, 0, None)})

    result = await _adding(20, refuses).run(
        CustomerTurnInput(message="add the lamp", state=_two_sofas_and_a_lamp(), context=CONTEXT)
    )

    assert result.state.product_interaction.selected_product_ids == (10,)
    assert [(r.name_english, r.reason, r.kept) for r in result.cart_refusals] == [
        ("Product 20", CartRefusalReason.NOT_IN_STORE, 0)
    ]
    assert not result.selection_added
    assert result.cart is not None and result.cart.changes == ()
    assert refuses.asked == [{20: 1}], "asked once, before the choice was recorded"
    # The next step is more like the lamp, from its card - never "narrow these
    # down" for a list the reply is not beside.
    assert result.cart_refusals[0].card == CardRef(list_revision=2, ordinal=1)
    assert result.next_step is not None
    assert result.next_step.kind is NextStepKind.AFTER_REFUSAL
    assert result.next_step.chips[0].product_action == MoreLikeThisAction(
        card=CardRef(list_revision=2, ordinal=1)
    )


async def test_more_than_one_order_takes_is_kept_at_the_limit() -> None:
    refuses = Refuses({20: (CartRefusalReason.OVER_LIMIT, 3, 3)})

    result = await _adding(20, refuses, quantity=5).run(
        CustomerTurnInput(message="add 5 lamps", state=_two_sofas_and_a_lamp(), context=CONTEXT)
    )

    assert result.state.product_interaction.quantity_of(20) == 3
    assert result.cart_refusals[0].max_quantity == 3


async def test_an_offer_is_never_made_for_what_the_store_refuses() -> None:
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.CONSIDER, reference=PresentedOrdinal(position=1)
            ),
        ),
        references=FakeReferences(default=ResolvedProductReference(product_id=20)),
        hydration=KindHydration(SOFAS_THEN_A_LAMP),  # type: ignore[arg-type]
        cart=_cart(),
    )
    coordinator._cart_check = Refuses({20: (CartRefusalReason.OUT_OF_STOCK, 0, None)})  # type: ignore[assignment]

    result = await coordinator.run(
        CustomerTurnInput(message="I like the lamp", state=_two_sofas_and_a_lamp(), context=CONTEXT)
    )

    assert result.cart_offer is None
    assert [r.reason for r in result.cart_refusals] == [CartRefusalReason.OUT_OF_STOCK]


async def test_a_tap_the_store_refuses_says_why() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=()))
    runtime = _runtime(sessions)
    runtime._store_check = Refuses({101: (CartRefusalReason.OUT_OF_STOCK, 0, None)})  # type: ignore[assignment]

    with pytest.raises(PickUnavailableError) as raised:
        await runtime.apply(
            PicksRequest(session_id="sess-1", store_id=STORE, action=SelectPickAction(ordinal=1)),
            CONTEXT,
        )

    assert raised.value.public_message == "That product is out of stock."


async def test_a_quantity_past_one_orders_limit_is_refused_by_tap() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _with_cart((101,)))
    runtime = _runtime(sessions)
    runtime._store_check = Refuses({101: (CartRefusalReason.OVER_LIMIT, 2, 2)})  # type: ignore[assignment]

    with pytest.raises(PickUnavailableError) as raised:
        await runtime.apply(
            PicksRequest(
                session_id="sess-1",
                store_id=STORE,
                action=SetQuantityAction(pick=1, quantity=3),
            ),
            CONTEXT,
        )

    assert "up to 2" in raised.value.public_message


async def test_liking_a_sofa_on_an_earlier_list_offers_it() -> None:
    """The sofas sit under the suggestions for another piece: "I like the
    second sofa" points at one they can still see - it once failed the turn."""
    from app.taxonomy.compare_groups import load_compare_groups

    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.CONSIDER,
                reference=PresentedOrdinal(position=2),
                expected_subcategory="sofa",
            ),
        ),
        references=FakeReferences(default=ResolvedProductReference(product_id=20)),
        hydration=KindHydration(SOFAS_THEN_A_LAMP),  # type: ignore[arg-type]
        compare_groups=load_compare_groups(),
        cart=_cart(),
    )

    result = await coordinator.run(
        CustomerTurnInput(
            message="I like the second sofa", state=_two_sofas_and_a_lamp(), context=CONTEXT
        )
    )

    assert result.state.product_interaction.focused_product_id == 11
    assert result.cart_offer is not None
    assert result.cart_offer.card is not None and result.cart_offer.card.list_revision == 1


async def test_one_refused_does_not_stop_the_others_and_already_is_named_exactly() -> None:
    """ "Add the first and third sofa": the first is not the store's, the
    third is already in the cart - each is said of itself, never of the piece
    last in focus."""
    from app.schemas.agent_decision import CartAddition

    class ByPosition(FakeReferences):
        async def resolve(self, selector: Any, state: Any, context: Any) -> Any:
            return ResolvedProductReference(product_id={1: 11, 3: 10}[selector.position])

    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            interaction=ProductInteractionIntent(
                op=ProductInteractionOp.SELECT, reference=PresentedOrdinal(position=1)
            ),
            more_to_add=(CartAddition(reference=PresentedOrdinal(position=3)),),
        ),
        references=ByPosition(),
        hydration=KindHydration(SOFAS_THEN_A_LAMP),  # type: ignore[arg-type]
        cart=_cart(),
    )
    coordinator._cart_check = Refuses({11: (CartRefusalReason.NOT_IN_STORE, 0, None)})  # type: ignore[assignment]

    result = await coordinator.run(
        CustomerTurnInput(
            message="add the first and third sofa",
            state=_two_sofas_and_a_lamp(),
            context=CONTEXT,
        )
    )

    assert [r.name_english for r in result.cart_refusals] == ["Product 11"]
    assert result.already_in_cart == (10,), "the third was looked at, and it is already in"


# ── what a refused room says ────────────────────────────────────────────────


def test_the_reply_may_name_a_refused_piece_with_the_numbers_in_its_name() -> None:
    """A room's lamp "Floor Lamp 2.25m" refused by Salla: the reply naming it
    was refused twice for "2.25" and fell back to a sentence saying nothing."""
    from app.schemas.cart import CartRefusal
    from app.services.numeric_guard import (
        build_allowance,
        cart_refusal_counts,
        cart_refusal_figures,
        check_numeric_policy,
    )

    refused = (
        CartRefusal(
            name_english="Black LED Floor Lamp 2.25m",
            reason=CartRefusalReason.NOT_IN_STORE,
            kept=0,
        ),
    )
    allowance = build_allowance(
        "Add all of these to my cart",
        counts=cart_refusal_counts(refused),
        figures=cart_refusal_figures(refused),
    )

    named = "The Black LED Floor Lamp 2.25m isn't available in this store's cart."
    invented = "It would have cost 900 SAR."
    assert check_numeric_policy(message=named, follow_up_question=None, allowance=allowance) is None
    assert (
        check_numeric_policy(message=invented, follow_up_question=None, allowance=allowance)
        is not None
    )


@pytest.mark.parametrize(
    ("kind", "changed", "expected"),
    [
        ("answer", False, "The store's cart couldn't take that, so your cart hasn't changed."),
        (
            "product_detail",
            True,
            "I've updated your cart, but the store's cart couldn't take everything you "
            "asked for, so I've left out what it refused. Here are the details for that one.",
        ),
    ],
)
def test_a_fallen_back_reply_still_says_what_the_store_refused(
    kind: str, changed: bool, expected: str
) -> None:
    from app.schemas.language import ReplyLanguage
    from app.schemas.response import (
        CartFactsView,
        CartRefusalFactView,
        ResponseGroundingView,
        ResponseOutcomeKind,
    )
    from app.services.response_generator import _fallback

    view = ResponseGroundingView(
        kind=ResponseOutcomeKind(kind),
        cart=CartFactsView(
            item_count=3,
            changed=changed,
            refused=(CartRefusalFactView(name="Rug", reason="not_in_store", kept=0),),
        ),
    )

    assert _fallback(view, ReplyLanguage.EN).message == expected
    assert _fallback(view.model_copy(update={"cart": None}), ReplyLanguage.EN).message != expected
