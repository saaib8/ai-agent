"""The single boundary between this service and a language-model provider.

Nothing outside this module imports the provider SDK or holds its credentials
(CLAUDE.md 20.3). Services depend on :class:`StructuredLLMClient`, so the
provider can be swapped or faked without touching domain code.

Only structured output crosses this boundary. No caller ever sees provider
objects, raw response bodies, or SDK exceptions: every provider failure becomes
a typed application error whose message we wrote.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any, NoReturn, Protocol, TypeVar

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
)
from pydantic import BaseModel, ValidationError

from app.core.config import LLMSettings
from app.core.exceptions import (
    LLMRequestError,
    LLMResponseInvalidError,
    LLMUnavailableError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

StructuredT = TypeVar("StructuredT", bound=BaseModel)

# Client-error statuses that mean "not right now" rather than "not like that":
# the request is well formed and the provider is simply unable to serve it.
# These are exactly the sub-500 codes the SDK itself retries, so by the time
# one surfaces here its retries are already spent and the failure is transient.
# Every other 4xx is the SDK declining to retry because repeating an invalid
# request cannot help.
_TRANSIENT_CLIENT_STATUSES = frozenset({408, 409, 429})


class StructuredLLMClient(Protocol):
    """Turns instructions plus one untrusted user message into a typed object."""

    @property
    def model(self) -> str: ...

    async def parse(
        self,
        *,
        instructions: str,
        user_input: str,
        schema: type[StructuredT],
    ) -> StructuredT: ...


class OpenAIStructuredClient:
    """Structured-output adapter over the OpenAI Responses API.

    One client per process, created in lifespan and reused (CLAUDE.md 24).
    """

    def __init__(self, settings: LLMSettings) -> None:
        self._model = settings.model
        # Only configured parameters are sent, so one adapter serves both
        # reasoning and non-reasoning models without branching on model name.
        self._extra: dict[str, Any] = {}
        if settings.temperature is not None:
            self._extra["temperature"] = settings.temperature
        if settings.reasoning_effort is not None:
            self._extra["reasoning"] = {"effort": settings.reasoning_effort}
        self._client = AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            timeout=settings.timeout_s,
            max_retries=settings.max_retries,
        )

    @property
    def model(self) -> str:
        return self._model

    async def parse(
        self,
        *,
        instructions: str,
        user_input: str,
        schema: type[StructuredT],
    ) -> StructuredT:
        # The customer's words are untrusted DATA, carried in the user turn -
        # never merged into the instructions (CLAUDE.md 20.1).
        return await self._request(
            instructions, [{"role": "user", "content": user_input}], schema
        )

    async def parse_images(
        self,
        *,
        instructions: str,
        user_input: str,
        images: Sequence[bytes],
        schema: type[StructuredT],
    ) -> StructuredT:
        """The same contract as `parse`, with JPEG images in the user turn.

        The images are untrusted data exactly as the words are: they travel in
        the user turn, and nothing drawn in them is an instruction.
        """
        content: list[dict[str, str]] = [{"type": "input_text", "text": user_input}]
        content.extend(
            {
                "type": "input_image",
                "image_url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"),
            }
            for image in images
        )
        return await self._request(instructions, [{"role": "user", "content": content}], schema)

    async def _request(
        self,
        instructions: str,
        messages: list[dict[str, Any]],
        schema: type[StructuredT],
    ) -> StructuredT:
        try:
            response = await self._client.responses.parse(
                model=self._model,
                instructions=instructions,
                input=messages,  # type: ignore[arg-type]
                text_format=schema,
                **self._extra,
            )
        except ValidationError as exc:
            # The provider honoured the JSON Schema and the answer still does
            # not satisfy the model: a cross-field validator rejected it. The
            # SDK validates inside `parse`, so this is raised *there*, not
            # below - and it is not an OpenAIError, so without this clause it
            # would escape the adapter as a raw provider-library exception.
            self._reject_as_schema_violation(exc)
        except (APITimeoutError, APIConnectionError) as exc:
            logger.warning("llm_unreachable", error_type=type(exc).__name__)
            raise LLMUnavailableError(provider="openai", reason=type(exc).__name__) from exc
        except APIStatusError as exc:
            # The status and exception class are safe to log; the response body
            # may echo the prompt, so it never is.
            status = exc.status_code
            if status >= 500 or status in _TRANSIENT_CLIENT_STATUSES:
                logger.warning(
                    "llm_provider_unavailable",
                    status_code=status,
                    error_type=type(exc).__name__,
                )
                raise LLMUnavailableError(provider="openai", status_code=status) from exc
            # The provider is healthy and rejected what we sent.
            logger.error(
                "llm_request_rejected",
                status_code=status,
                error_type=type(exc).__name__,
            )
            raise LLMRequestError(provider="openai", status_code=status) from exc
        except OpenAIError as exc:
            logger.warning("llm_call_failed", error_type=type(exc).__name__)
            raise LLMUnavailableError(provider="openai", reason=type(exc).__name__) from exc

        parsed = response.output_parsed
        if parsed is None:
            # A refusal or an unparseable answer. We do not repair it.
            logger.warning("llm_response_unparsed", model=self._model)
            raise LLMResponseInvalidError(reason="no structured output returned")
        try:
            return schema.model_validate(parsed)
        except ValidationError as exc:
            self._reject_as_schema_violation(exc)

    def _reject_as_schema_violation(self, exc: ValidationError) -> NoReturn:
        """The one way a validation failure leaves this adapter.

        Both places that validate route here, so a caller can never receive a
        raw `ValidationError`: the error type is ours, and its message says
        nothing about which field or value the provider returned.
        """
        logger.warning("llm_response_schema_violation", model=self._model)
        raise LLMResponseInvalidError(reason="schema violation") from exc

    async def close(self) -> None:
        await self._client.close()
