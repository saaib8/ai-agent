"""Who may have a room drawn is the backend gateway's answer, sent with each
request; with none, only a deployment that draws without the gateway draws
(docs/visualization-in-chat-plan.md D1)."""

from __future__ import annotations

import pytest
from app.core.config import Environment
from app.core.exceptions import VisualizationNotAllowedError
from app.schemas.chat import ChatRequest
from app.schemas.retailer import RetailerContext
from app.schemas.visualization import (
    VISUALIZATION_ALLOWED,
    VISUALIZATION_UNCONFIRMED,
    visualization_access,
)
from app.services.room_visualization import refuse_unless_allowed

from tests.conftest import build_settings

CONTEXT = RetailerContext(store_id=50)
DRAWING = {"openai_model": "image-model"}


@pytest.mark.parametrize("without_gateway", [True, False])
def test_the_gateway_answer_wins(without_gateway: bool) -> None:
    assert visualization_access("allowed", without_gateway=without_gateway) == "allowed"
    assert visualization_access("limit_reached", without_gateway=without_gateway) == "limit_reached"


def test_no_answer_follows_the_deployment() -> None:
    assert visualization_access(None, without_gateway=True) == VISUALIZATION_ALLOWED
    assert visualization_access(None, without_gateway=False) == VISUALIZATION_UNCONFIRMED


@pytest.mark.parametrize(
    ("environment", "draws"),
    [
        (Environment.LOCAL, True),
        (Environment.TEST, True),
        (Environment.STAGE, True),
        (Environment.PROD, False),
    ],
)
def test_without_an_answer_only_prod_refuses(environment: Environment, draws: bool) -> None:
    settings = build_settings(visualization=DRAWING).model_copy(update={"environment": environment})
    assert settings.draws_without_gateway is draws


@pytest.mark.parametrize("override", [True, False])
def test_the_setting_overrides_the_environment(override: bool) -> None:
    settings = build_settings(
        environment="prod", visualization={**DRAWING, "allowed_without_gateway": override}
    )
    assert settings.draws_without_gateway is override


def test_nothing_draws_where_drawing_is_not_configured() -> None:
    assert build_settings().draws_without_gateway is False


@pytest.mark.parametrize(
    ("sent", "read"),
    [
        ("allowed", "allowed"),
        (" Allowed ", "allowed"),
        ("SIGN_UP_REQUIRED", "sign_up_required"),
        ("limit_reached", "limit_reached"),
        ("not_included", "not_included"),
        ("store_unavailable", "store_unavailable"),
        ("tool_not_allowed", "unrecognised"),
        ("sign up", "unrecognised"),
        ("", "unrecognised"),
        ("x" * 500, "unrecognised"),
        (7, "unrecognised"),
        (True, "unrecognised"),
        ({"a": 1}, "unrecognised"),
    ],
)
def test_the_answer_never_rejects_the_message(sent: object, read: str) -> None:
    """One of five values; anything else is read as no picture - never a
    reason to refuse the customer's message."""
    request = ChatRequest.model_validate(
        {"session_id": "s", "store_id": 1, "message": "hi", "visualization": sent}
    )
    assert request.visualization == read


def test_an_unknown_answer_draws_nothing() -> None:
    assert visualization_access("unrecognised", without_gateway=True) == "unrecognised"
    with pytest.raises(VisualizationNotAllowedError):
        refuse_unless_allowed("unrecognised", True, CONTEXT)


def test_a_picture_endpoint_refuses_before_anything_is_loaded() -> None:
    with pytest.raises(VisualizationNotAllowedError) as refused:
        refuse_unless_allowed("sign_up_required", False, CONTEXT)
    assert refused.value.context == {"reason": "sign_up_required"}
    with pytest.raises(VisualizationNotAllowedError):
        refuse_unless_allowed(None, False, CONTEXT)
    refuse_unless_allowed("allowed", False, CONTEXT)
    refuse_unless_allowed(None, True, CONTEXT)


@pytest.mark.parametrize(
    ("sent", "without_gateway", "access"),
    [
        ("allowed", False, "allowed"),
        ("not_included", True, "not_included"),
        (None, True, "allowed"),
        (None, False, "not_confirmed"),
    ],
)
def test_a_chat_turn_reads_the_answer_once(
    sent: str | None, without_gateway: bool, access: str
) -> None:
    from typing import Any, cast

    from app.core.config import SessionSettings
    from app.services.chat_runtime import ChatRuntime

    runtime = ChatRuntime(
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        SessionSettings(),
        draws_without_gateway=without_gateway,
    )
    request = ChatRequest(session_id="s", store_id=1, message="hi", visualization=sent)
    assert runtime.visualization(request) == access


def test_a_picture_s_pieces_can_be_opened_after_it() -> None:
    """Its pieces are listed under the picture: they count as seen, so "the
    second piece in that picture" can be put in focus like any card - the
    live crash of 2026-11-15, where a room piece neither on screen nor in the
    cart could not be focused."""
    from app.schemas.agent_state import AgentStateV1, RenderedPiece, RenderRecord
    from app.schemas.agent_updates import AgentStateUpdate, ProductInteractionUpdate
    from app.schemas.visualization import RenderSource
    from app.services.agent_state import apply_update, record_render

    state = record_render(
        AgentStateV1(),
        RenderRecord(
            source=RenderSource.PACKAGE,
            pieces=(RenderedPiece(product_id=10), RenderedPiece(product_id=11, quantity=2)),
        ),
    )

    assert {10, 11} <= set(state.product_interaction.seen_product_ids)
    focused = apply_update(
        state,
        AgentStateUpdate(product_interaction=ProductInteractionUpdate(focused_product_id=11)),
    )
    assert focused.product_interaction.focused_product_id == 11
    assert focused.last_render == state.last_render
