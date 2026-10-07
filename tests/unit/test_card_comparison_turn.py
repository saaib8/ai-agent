"""Checked cards compare inside chat and remain addressable on the next turn."""

from typing import Any, cast

import pytest
from app.core.config import CustomerAgentSettings, SessionSettings
from app.core.exceptions import ComparisonRefusedError
from app.repositories.products import ProductRepository
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    ProductInteractionIntent,
    ProductInteractionOp,
)
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.card_comparison import CardRef
from app.schemas.chat import ChatRequest
from app.schemas.product_action import CompareCardsAction
from app.schemas.product_reference import ComparedOrdinal
from app.services.chat_runtime import ChatRuntime
from app.services.comparison import ProductComparisonService
from app.services.reference_resolver import ProductReferenceResolver
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from pydantic import ValidationError

from tests.unit.test_card_comparison import GROUPS, KINDS, TAXONOMY, _product, _state
from tests.unit.test_chat_api import GRAPH, FakeResponses, FakeSessionStore
from tests.unit.test_picks import SESSION, STORE, _stored
from tests.unit.test_reference_resolver import FakeRepository
from tests.unit.test_turn_coordinator import CONTEXT, FakeHydration, _coordinator


def _engine(
    decision: CustomerAgentDecision | None = None, *, maximum: int = 10
) -> tuple[Any, dict[str, Any]]:
    repository = cast(ProductRepository, FakeRepository([_product(pid) for pid in KINDS]))
    return _coordinator(
        decision or CustomerAgentDecision(action=AgentAction.ANSWER),
        references=cast(Any, ProductReferenceResolver(repository, load_catalog_attributes())),
        comparison=ProductComparisonService(
            repository,
            load_dimension_semantics(taxonomy=TAXONOMY),
            CustomerAgentSettings(comparison_max_products=maximum),
        ),
        hydration=FakeHydration(available=tuple(KINDS)),
        compare_groups=GROUPS,
    )


def _action(*cards: tuple[int, int]) -> CompareCardsAction:
    return CompareCardsAction(
        cards=tuple(CardRef(list_revision=revision, ordinal=ordinal) for revision, ordinal in cards)
    )


async def test_checked_cards_become_an_inline_persisted_comparison_and_can_be_picked() -> None:
    sessions = FakeSessionStore()
    before = await _stored(sessions, _state())
    coordinator, parts = _engine()
    responses = FakeResponses("The sectional is larger. Which do you prefer?")
    runtime = ChatRuntime(coordinator, cast(Any, responses), cast(Any, sessions), SessionSettings())
    request = ChatRequest(
        session_id=SESSION,
        store_id=STORE,
        message="Compare these two products",
        product_action=_action((1, 2), (1, 3)),
        expected_session_revision=before.session_revision,
    )

    reply = await GRAPH.run(runtime, request, CONTEXT)

    assert reply.session_revision == before.session_revision + 1
    assert reply.presentation is not None and reply.presentation.comparison is not None
    assert [p.name_english for p in reply.presentation.comparison.products] == [
        _product(102).name_english,
        _product(201).name_english,
    ]
    assert [choice.label for choice in reply.presentation.choices] == [
        "Take the first",
        "Take the second",
        "Show similar ones",
    ]
    stored = sessions.saved[(STORE, SESSION)]
    assert stored.state.product_interaction.compared_product_ids == (102, 201)
    assert stored.state.product_interaction.selected_product_ids == ()
    assert stored.state.active_search == before.state.active_search
    assert stored.state.product_interaction.presented_product_ids == (301,)
    assert [m.content for m in stored.conversation.messages[-2:]] == [
        request.message,
        responses.message,
    ]
    assert parts["decisions"].inputs == [], "a button tap must bypass decision inference"

    decision = CustomerAgentDecision(
        action=AgentAction.ANSWER,
        interaction=ProductInteractionIntent(
            op=ProductInteractionOp.SELECT, reference=ComparedOrdinal(position=2)
        ),
    )
    followup_coordinator, followup_parts = _engine(decision)
    followup = ChatRuntime(
        followup_coordinator, cast(Any, responses), cast(Any, sessions), SessionSettings()
    )
    await GRAPH.run(
        followup,
        ChatRequest(
            session_id=SESSION,
            store_id=STORE,
            message="I prefer the second product from the comparison",
            expected_session_revision=reply.session_revision,
        ),
        CONTEXT,
    )

    assert sessions.saved[(STORE, SESSION)].state.product_interaction.selected_product_ids == (201,)
    assert request.message in [
        m.content for m in followup_parts["decisions"].inputs[0].conversation.messages
    ]


@pytest.mark.parametrize("cards", [((1, 1), (1, 2)), ((1, 3), (1, 1)), ((1, 1), (1, 2), (1, 3))])
async def test_checked_cards_preserve_column_order_and_reviewed_families(
    cards: tuple[tuple[int, int], ...],
) -> None:
    coordinator, _ = _engine()
    result = await coordinator.run(
        CustomerTurnInput(
            message="Compare these", state=_state(), context=CONTEXT, product_action=_action(*cards)
        )
    )
    expected = tuple((101, 102, 201)[ordinal - 1] for _, ordinal in cards)
    assert result.state.product_interaction.compared_product_ids == expected


async def test_dissimilar_checked_cards_are_refused_without_recording_comparison() -> None:
    coordinator, _ = _engine()
    with pytest.raises(ComparisonRefusedError):
        await coordinator.run(
            CustomerTurnInput(
                message="Compare these",
                state=_state(),
                context=CONTEXT,
                product_action=_action((1, 1), (2, 1)),
            )
        )


@pytest.mark.parametrize("cards", [((9, 1), (1, 2)), ((1, 99), (1, 2))])
async def test_stale_or_out_of_range_cards_do_not_record_comparison(
    cards: tuple[tuple[int, int], ...],
) -> None:
    coordinator, _ = _engine()
    state = _state()
    result = await coordinator.run(
        CustomerTurnInput(
            message="Compare these", state=state, context=CONTEXT, product_action=_action(*cards)
        )
    )
    assert result.grounding.comparison is None
    assert result.state == state


async def test_configured_comparison_limit_is_enforced_for_checked_cards() -> None:
    coordinator, _ = _engine(maximum=2)
    result = await coordinator.run(
        CustomerTurnInput(
            message="Compare these",
            state=_state(),
            context=CONTEXT,
            product_action=_action((1, 1), (1, 2), (1, 3)),
        )
    )
    assert result.grounding.comparison is None
    assert result.state.product_interaction.compared_product_ids == ()


@pytest.mark.parametrize("cards", [((1, 1),), ((1, 1), (1, 1)), ((0, 1), (1, 2))])
def test_invalid_checked_card_actions_are_rejected(cards: tuple[tuple[int, int], ...]) -> None:
    with pytest.raises(ValidationError):
        _action(*cards)
