"""The picks tray: ticking, unticking, and naming a pick.

A tick changes the session without a chat turn, so the checks here are the
ones a silent write must pass: it records exactly what a typed "I'll take the
second one" records, it leaves the conversation untouched, it refuses a card
that is no longer on screen, and it never trusts an id from the client.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from app.api.dependencies import picks_runtime, retailer_context_provider
from app.api.errors import register_exception_handlers
from app.api.routes.picks import router as picks_router
from app.core.config import CustomerAgentSettings
from app.core.exceptions import PickLimitError, PickUnavailableError, SessionConflictError
from app.repositories.products import ProductRepository
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    ProductInteractionState,
)
from app.schemas.agent_turn import CustomerTurnResult, TurnGrounding
from app.schemas.chat import ChatRequest, ReplyChoice
from app.schemas.conversation import ConversationContext, ConversationMessage, ConversationRole
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import ProductSearchRequest
from app.schemas.picks import DeselectPickAction, PicksRequest, SelectPickAction
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.product_action import (
    CompanionAction,
    CompanionOffer,
    ComparePicksAction,
    GoesWithPickAction,
)
from app.schemas.product_reference import PickedOrdinal
from app.schemas.resolution import ReferenceFailureReason, ReferenceUnresolved
from app.schemas.retailer import RetailerContext
from app.schemas.session import SessionEnvelope, new_session
from app.services.chat_runtime import ChatRuntime
from app.services.grounding_builder import to_grounded_product
from app.services.picks import PicksRuntime
from app.services.product_interaction import build_picks
from app.services.reference_resolver import ProductReferenceResolver
from app.taxonomy.attributes import load_catalog_attributes
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from tests.unit.test_chat_api import FakeRetailers, FakeSessionStore
from tests.unit.test_reference_resolver import FakeRepository, _row

STORE = 50
SESSION = "sess-1"
CONTEXT = RetailerContext(store_id=STORE)
ON_SCREEN = (101, 102, 103)


def _candidate(product_id: int) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Sofa {product_id}",
        name_arabic="كنبة",
        price_amount=Decimal("2000"),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/p/{product_id}",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


class Hydration:
    def __init__(self, gone: tuple[int, ...] = ()) -> None:
        self.gone = gone

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[ProductCandidate, ...]:
        return tuple(_candidate(p) for p in product_ids if p not in self.gone)


def _state(
    *, picks: tuple[int, ...] = (), focus: int | None = None, presented: tuple[int, ...] = ON_SCREEN
) -> AgentStateV1:
    return AgentStateV1(
        active_search=ActiveSearchState(
            request=ProductSearchRequest(commerce_category="seating"), revision=1
        ),
        product_interaction=ProductInteractionState(
            presented_product_ids=presented,
            presented_search_revision=1 if presented else None,
            selected_product_ids=picks,
            focused_product_id=focus,
        ),
    )


HISTORY = ConversationContext(
    messages=(
        ConversationMessage(role=ConversationRole.USER, content="show me sofas"),
        ConversationMessage(role=ConversationRole.ASSISTANT, content="Here are three."),
    )
)


async def _stored(sessions: FakeSessionStore, state: AgentStateV1) -> SessionEnvelope:
    fresh = new_session()
    envelope = fresh.advanced(state=state, conversation=HISTORY)
    assert await sessions.save_if_revision(STORE, SESSION, expected_revision=0, envelope=envelope)
    return envelope


def _runtime(
    sessions: FakeSessionStore,
    *,
    rows: tuple[int, ...] = (*ON_SCREEN, 104),
    gone: tuple[int, ...] = (),
    max_picks: int = 10,
) -> PicksRuntime:
    repository = FakeRepository([_row(pid) for pid in rows if pid not in gone])
    return PicksRuntime(
        ProductReferenceResolver(cast(ProductRepository, repository), load_catalog_attributes()),
        Hydration(gone),  # type: ignore[arg-type]
        sessions,  # type: ignore[arg-type]
        CustomerAgentSettings(max_picks=max_picks),
    )


def _tick(ordinal: int, **kwargs: Any) -> PicksRequest:
    return PicksRequest(
        session_id=SESSION, store_id=STORE, action=SelectPickAction(ordinal=ordinal), **kwargs
    )


def _untick(pick: int) -> PicksRequest:
    return PicksRequest(session_id=SESSION, store_id=STORE, action=DeselectPickAction(pick=pick))


# ── ticking ═════════════════════════════════════════════════════════════════


async def test_a_tick_picks_the_card_at_that_position_and_focuses_it() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())

    reply = await _runtime(sessions).apply(_tick(2), CONTEXT)

    saved = sessions.saved[(STORE, SESSION)]
    assert saved.state.product_interaction.selected_product_ids == (102,)
    assert saved.state.product_interaction.focused_product_id == 102
    assert reply.session_revision == saved.session_revision == 2
    assert [(p.pick, p.name_english, p.presented_ordinal, p.focused) for p in reply.picks] == [
        (1, "Sofa 102", 2, True)
    ]


async def test_a_tick_is_silent_the_conversation_is_left_as_it_was() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())

    await _runtime(sessions).apply(_tick(1), CONTEXT)

    assert sessions.saved[(STORE, SESSION)].conversation == HISTORY


async def test_picks_keep_the_order_they_were_picked_in() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())
    runtime = _runtime(sessions)

    await runtime.apply(_tick(3), CONTEXT)
    reply = await runtime.apply(_tick(1), CONTEXT)

    assert [p.name_english for p in reply.picks] == ["Sofa 103", "Sofa 101"]
    assert [p.pick for p in reply.picks] == [1, 2]


async def test_ticking_a_card_already_picked_changes_nothing_and_writes_nothing() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=(102,)))
    writes = sessions.saves

    reply = await _runtime(sessions).apply(_tick(2), CONTEXT)

    assert sessions.saves == writes
    assert reply.session_revision == 1
    assert [p.name_english for p in reply.picks] == ["Sofa 102"]


async def test_a_full_tray_refuses_another_pick() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=(101, 102)))

    with pytest.raises(PickLimitError) as raised:
        await _runtime(sessions, max_picks=2).apply(_tick(3), CONTEXT)

    assert "up to 2 picks" in raised.value.public_message


async def test_a_card_beyond_the_results_on_screen_is_refused() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())

    with pytest.raises(PickUnavailableError) as raised:
        await _runtime(sessions).apply(_tick(9), CONTEXT)

    assert raised.value.context["reason"] == str(ReferenceFailureReason.ORDINAL_OUT_OF_RANGE)
    assert sessions.saved[(STORE, SESSION)].session_revision == 1


async def test_a_product_that_left_the_catalog_cannot_be_picked() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())

    with pytest.raises(PickUnavailableError) as raised:
        await _runtime(sessions, gone=(102,)).apply(_tick(2), CONTEXT)

    assert "no longer available" in raised.value.public_message


async def test_a_stale_screen_is_refused_before_anything_changes() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())

    with pytest.raises(SessionConflictError):
        await _runtime(sessions).apply(_tick(1, expected_session_revision=5), CONTEXT)

    assert sessions.saved[(STORE, SESSION)].state.product_interaction.selected_product_ids == ()


# ── unticking ═══════════════════════════════════════════════════════════════


async def test_an_untick_removes_that_pick() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=(101, 103)))

    reply = await _runtime(sessions).apply(_untick(1), CONTEXT)

    assert sessions.saved[(STORE, SESSION)].state.product_interaction.selected_product_ids == (103,)
    assert [(p.pick, p.name_english) for p in reply.picks] == [(1, "Sofa 103")]


async def test_unticking_the_focused_pick_off_screen_clears_the_focus_with_it() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=(104,), focus=104))

    await _runtime(sessions).apply(_untick(1), CONTEXT)

    interaction = sessions.saved[(STORE, SESSION)].state.product_interaction
    assert interaction.selected_product_ids == ()
    assert interaction.focused_product_id is None


async def test_a_pick_that_left_the_catalog_can_still_be_removed() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=(104, 101)))

    reply = await _runtime(sessions, gone=(104,)).apply(_untick(1), CONTEXT)

    assert [p.name_english for p in reply.picks] == ["Sofa 101"]


async def test_unticking_a_pick_that_is_not_there_is_refused() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state(picks=(101,)))

    with pytest.raises(PickUnavailableError):
        await _runtime(sessions).apply(_untick(3), CONTEXT)


# ── the tray's numbering ════════════════════════════════════════════════════


def test_a_gone_pick_leaves_a_gap_and_the_rest_keep_their_numbers() -> None:
    state = _state(picks=(101, 104, 103), focus=103)

    picks = build_picks(state, [_candidate(101), _candidate(103)])

    assert [(p.pick, p.presented_ordinal, p.focused) for p in picks] == [
        (1, 1, False),
        (3, 3, True),
    ]


def test_with_no_committed_results_no_pick_claims_a_card_number() -> None:
    state = AgentStateV1(product_interaction=ProductInteractionState(selected_product_ids=(101,)))

    (pick,) = build_picks(state, [_candidate(101)])

    assert pick.presented_ordinal is None


# ── naming a pick ═══════════════════════════════════════════════════════════


def _resolver(*rows: int) -> ProductReferenceResolver:
    repository = FakeRepository([_row(pid) for pid in rows])
    return ProductReferenceResolver(cast(ProductRepository, repository), load_catalog_attributes())


async def test_a_picked_ordinal_counts_in_the_picks_not_the_results() -> None:
    state = _state(picks=(103, 101))

    outcome = await _resolver(101, 103).resolve(PickedOrdinal(position=1), state, CONTEXT)

    assert not isinstance(outcome, ReferenceUnresolved)
    assert outcome.product_id == 103


async def test_a_pick_from_an_earlier_search_still_resolves() -> None:
    state = _state(picks=(999,), presented=(101, 102))

    outcome = await _resolver(999).resolve(PickedOrdinal(position=1), state, CONTEXT)

    assert not isinstance(outcome, ReferenceUnresolved)
    assert outcome.product_id == 999


@pytest.mark.parametrize(
    ("picks", "position", "reason"),
    [
        ((), 1, ReferenceFailureReason.NO_SELECTED_PRODUCT),
        ((101,), 2, ReferenceFailureReason.PICKED_ORDINAL_OUT_OF_RANGE),
        ((555,), 1, ReferenceFailureReason.PRODUCT_UNAVAILABLE),
    ],
)
async def test_a_pick_that_cannot_be_named_is_refused(
    picks: tuple[int, ...], position: int, reason: ReferenceFailureReason
) -> None:
    outcome = await _resolver(101).resolve(
        PickedOrdinal(position=position), _state(picks=picks), CONTEXT
    )

    assert isinstance(outcome, ReferenceUnresolved)
    assert outcome.reason is reason


async def test_another_retailers_product_is_not_a_pick() -> None:
    repository = FakeRepository([_row(101, store_id=60)])
    resolver = ProductReferenceResolver(
        cast(ProductRepository, repository), load_catalog_attributes()
    )

    outcome = await resolver.resolve(PickedOrdinal(position=1), _state(picks=(101,)), CONTEXT)

    assert isinstance(outcome, ReferenceUnresolved)


# ── the contracts ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("picks", [(1, 1), (0, 2)])
def test_a_comparison_names_two_different_picks(picks: tuple[int, int]) -> None:
    with pytest.raises(ValidationError):
        ComparePicksAction(picks=picks)


def test_a_turn_carries_one_structured_action_at_most() -> None:
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(
            {
                "session_id": SESSION,
                "store_id": STORE,
                "message": "compare",
                "product_action": {"kind": "compare", "picks": [1, 2]},
                "search_action": {"kind": "more_options"},
            }
        )


def test_a_product_action_arrives_by_kind() -> None:
    request = ChatRequest.model_validate(
        {
            "session_id": SESSION,
            "store_id": STORE,
            "message": "Tell me about it",
            "product_action": {"kind": "goes_with", "pick": 2},
        }
    )
    assert request.product_action == GoesWithPickAction(pick=2)


def test_no_action_names_a_product_id() -> None:
    """Positions and reviewed types only - the authority argument (20.2)."""
    for model in (
        GoesWithPickAction,
        ComparePicksAction,
        CompanionAction,
        SelectPickAction,
        DeselectPickAction,
    ):
        names = set(model.model_fields)
        assert "id" not in names
        assert not {name for name in names if name.endswith(("_id", "_ids"))}, model


# ── what the client draws ═══════════════════════════════════════════════════


def _result(**fields: Any) -> CustomerTurnResult:
    from app.schemas.agent_decision import AgentAction, CustomerAgentDecision

    return CustomerTurnResult(
        state=AgentStateV1(),
        decision=CustomerAgentDecision(action=AgentAction.ANSWER),
        grounding=fields.pop("grounding", TurnGrounding()),
        **fields,
    )


def test_an_opened_pick_is_drawn_above_its_companions_with_chips() -> None:
    focus = to_grounded_product(
        _candidate(7), grounding_ref=1, presented_ordinal=None, relaxation_depth=None
    )
    presentation = ChatRuntime.presentation(
        _result(
            grounding=TurnGrounding(product_detail=focus),
            focus=None,
            companions=(CompanionOffer(category="decor", subcategory="carpet", label="rugs"),),
        )
    )

    assert presentation is not None
    assert presentation.product_source == "detail"
    (chip,) = presentation.choices
    assert chip == ReplyChoice(
        label="Rugs",
        value="Show me rugs to go with it",
        product_action=CompanionAction(category="decor", subcategory="carpet"),
    )


def test_a_pick_offered_what_goes_with_it_can_also_say_no() -> None:
    """Their pick above, the kinds as chips, nothing searched - and a way to
    decline beside them."""
    focus = to_grounded_product(
        _candidate(7), grounding_ref=1, presented_ordinal=None, relaxation_depth=None
    )
    presentation = ChatRuntime.presentation(
        _result(
            focus=focus,
            companions=(CompanionOffer(category="decor", subcategory="carpet", label="rugs"),),
        )
    )

    assert presentation is not None
    assert presentation.focus == focus and presentation.products == ()
    assert [c.label for c in presentation.choices] == ["Rugs", "No thanks"]
    assert presentation.choices[-1].product_action is None


def test_the_focus_card_alone_is_something_to_draw() -> None:
    focus = to_grounded_product(
        _candidate(7), grounding_ref=1, presented_ordinal=None, relaxation_depth=None
    )
    presentation = ChatRuntime.presentation(_result(focus=focus))

    assert presentation is not None and presentation.focus == focus
    assert presentation.product_source is None, "no list, so nothing to count into"


def test_chips_stop_at_four() -> None:
    offers = tuple(
        CompanionOffer(category="decor", subcategory=f"type-{n}", label=f"things {n}")
        for n in range(6)
    )
    presentation = ChatRuntime.presentation(_result(companions=offers))

    assert presentation is not None
    assert len(presentation.choices) == 4


# ── the route ═══════════════════════════════════════════════════════════════


def _app(runtime: PicksRuntime) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(picks_router, prefix="/v1")
    app.dependency_overrides[picks_runtime] = lambda: runtime
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()
    return app


async def _post(app: FastAPI, body: dict[str, Any]) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post("/v1/picks", json=body)


async def test_the_route_answers_with_the_picks_and_no_ids() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())

    reply = await _post(
        _app(_runtime(sessions)),
        {"session_id": SESSION, "store_id": STORE, "action": {"kind": "select", "ordinal": 1}},
    )

    assert reply.status_code == 200
    body = reply.json()
    assert body["session_revision"] == 2
    assert body["picks"][0]["pick"] == 1
    assert "product_id" not in reply.text


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "select", "product_id": 101},
        {"kind": "select", "ordinal": 0},
        {"kind": "delete_everything"},
    ],
)
async def test_a_malformed_tick_is_refused_at_the_boundary(action: dict[str, Any]) -> None:
    sessions = FakeSessionStore()
    reply = await _post(
        _app(_runtime(sessions)), {"session_id": SESSION, "store_id": STORE, "action": action}
    )
    assert reply.status_code == 422


async def test_a_card_no_longer_on_screen_answers_with_our_words() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())

    reply = await _post(
        _app(_runtime(sessions)),
        {"session_id": SESSION, "store_id": STORE, "action": {"kind": "select", "ordinal": 8}},
    )

    assert reply.status_code == 409
    assert reply.json()["error"]["code"] == "pick_unavailable"
