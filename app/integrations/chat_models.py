"""The agents' chat models: LangChain's OpenAI chat model, built once per role.

The only place outside `llm.py` that holds the provider's credentials
(CLAUDE.md 20.3). Agents receive a `BaseChatModel`, never a key, and every
provider failure leaves here as the same typed application error the
structured client raises.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI
from openai import OpenAIError

from app.core.config import AgentRoleSettings, LLMSettings
from app.core.exceptions import LLMResponseInvalidError
from app.core.logging import get_logger
from app.integrations.llm import raise_provider_error

logger = get_logger(__name__)


def build_chat_model(
    llm: LLMSettings, role: AgentRoleSettings, *, use_responses_api: bool
) -> ChatOpenAI | None:
    """One agent's model, or None when that agent has no model configured."""
    if role.model is None:
        return None
    effort = role.reasoning_effort or llm.reasoning_effort
    extra: dict[str, Any] = {}
    if effort is not None and effort != "none":
        extra["reasoning_effort"] = effort
    if role.temperature is not None:
        extra["temperature"] = role.temperature
    if use_responses_api:
        # Nothing is stored at the provider, so the reasoning travels back to
        # it with each step, encrypted, rather than being lost between steps.
        extra["include"] = ["reasoning.encrypted_content"]
    return ChatOpenAI(
        model=role.model,
        api_key=llm.api_key,
        base_url=llm.base_url,
        timeout=llm.timeout_s,
        max_retries=llm.max_retries,
        use_responses_api=use_responses_api,
        # Nothing an agent sends is kept at the provider.
        store=False,
        **extra,
    )


async def call_chat_model(model: BaseChatModel | Any, messages: Sequence[BaseMessage]) -> AIMessage:
    """One model call, with the provider's failures as ours.

    `model` is already bound to the agent's tools. A reply that is not a
    message - which no provider should send - is unusable output.
    """
    try:
        reply = await model.ainvoke(list(messages))
    except OpenAIError as exc:
        raise_provider_error(exc, attempts=1)
    except (ValueError, KeyError) as exc:
        # LangChain's own reading of a provider body it could not use; its
        # message may quote that body, so only the type is kept.
        logger.warning("chat_model_response_unreadable", error_type=type(exc).__name__)
        raise LLMResponseInvalidError(reason="provider response unreadable") from exc
    if not isinstance(reply, AIMessage):
        raise LLMResponseInvalidError(reason="no message returned")
    return reply
