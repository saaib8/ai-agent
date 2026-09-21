"""Provider failures become typed application errors, and leak nothing.

The distinction that matters: a provider that cannot serve us right now is a
temporary outage, while a provider that rejected what we sent is our defect.
Reporting the second as the first hides a permanent misconfiguration behind a
"try again later" (CLAUDE.md 21).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any, ClassVar

import httpx
import pytest
import pytest_asyncio
from app.api.errors import register_exception_handlers
from app.api.middleware import TraceContextMiddleware
from app.core.config import LLMSettings
from app.core.exceptions import (
    LLMRequestError,
    LLMResponseInvalidError,
    LLMUnavailableError,
)
from app.integrations.llm import OpenAIStructuredClient
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    OpenAIError,
    RateLimitError,
    UnprocessableEntityError,
)
from pydantic import BaseModel, SecretStr, ValidationError, model_validator

# Text a provider might return that must never reach a caller.
PROVIDER_DETAIL = "Unsupported parameter: 'temperature' at sk-live-abc123"


class _Shape(BaseModel):
    value: str | None = None


def _client() -> OpenAIStructuredClient:
    return OpenAIStructuredClient(
        LLMSettings(api_key=SecretStr("test-key-not-real"), model="test-model")
    )


def _status_error(kind: type[APIStatusError], status: int) -> APIStatusError:
    request = httpx.Request("POST", "https://api.openai.test/v1/responses")
    response = httpx.Response(status_code=status, request=request, text=PROVIDER_DETAIL)
    return kind(PROVIDER_DETAIL, response=response, body=None)


async def _raising(exc: Exception) -> Any:
    async def _parse(**_: Any) -> Any:
        raise exc

    return _parse


async def _call(exc: Exception) -> None:
    client = _client()
    client._client.responses.parse = await _raising(exc)  # type: ignore[method-assign]
    await client.parse(instructions="i", user_input="u", schema=_Shape)


# ── unavailable: the provider could not serve us ────────────────────────────


async def test_a_timeout_is_unavailable() -> None:
    request = httpx.Request("POST", "https://api.openai.test/v1/responses")

    with pytest.raises(LLMUnavailableError) as caught:
        await _call(APITimeoutError(request))

    assert caught.value.http_status == HTTPStatus.SERVICE_UNAVAILABLE


async def test_a_connection_failure_is_unavailable() -> None:
    request = httpx.Request("POST", "https://api.openai.test/v1/responses")

    with pytest.raises(LLMUnavailableError) as caught:
        await _call(APIConnectionError(request=request))

    assert caught.value.http_status == HTTPStatus.SERVICE_UNAVAILABLE


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (InternalServerError, 500),
        (InternalServerError, 502),
        (InternalServerError, 503),
        (RateLimitError, 429),
        (ConflictError, 409),
        (APIStatusError, 408),
    ],
)
async def test_transient_provider_statuses_are_unavailable(
    kind: type[APIStatusError], status: int
) -> None:
    """5xx plus exactly the sub-500 codes the SDK retries."""
    with pytest.raises(LLMUnavailableError) as caught:
        await _call(_status_error(kind, status))

    assert caught.value.http_status == HTTPStatus.SERVICE_UNAVAILABLE
    assert caught.value.context["status_code"] == status


async def test_an_unclassified_provider_error_is_unavailable() -> None:
    with pytest.raises(LLMUnavailableError):
        await _call(OpenAIError("something else went wrong"))


# ── request error: the provider rejected what we sent ───────────────────────


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (BadRequestError, 400),
        (AuthenticationError, 401),
        (NotFoundError, 404),
        (UnprocessableEntityError, 422),
    ],
)
async def test_rejected_requests_are_our_fault_not_an_outage(
    kind: type[APIStatusError], status: int
) -> None:
    with pytest.raises(LLMRequestError) as caught:
        await _call(_status_error(kind, status))

    assert caught.value.http_status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert caught.value.context["status_code"] == status


async def test_the_unsupported_parameter_case_is_not_reported_as_an_outage() -> None:
    """The real 400 that prompted this: a reasoning model refusing temperature."""
    with pytest.raises(LLMRequestError) as caught:
        await _call(_status_error(BadRequestError, 400))

    assert not isinstance(caught.value, LLMUnavailableError)


async def test_a_rejected_request_is_never_retried_by_us() -> None:
    """Repeating an invalid request cannot help; only the SDK retries, and it
    declines these statuses."""
    attempts = 0

    async def _parse(**_: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise _status_error(BadRequestError, 400)

    client = _client()
    client._client.responses.parse = _parse  # type: ignore[method-assign]
    with pytest.raises(LLMRequestError):
        await client.parse(instructions="i", user_input="u", schema=_Shape)

    assert attempts == 1


# ── response failures keep their existing behaviour ─────────────────────────


async def test_a_missing_structured_response_is_response_invalid() -> None:
    class _Response:
        output_parsed = None

    async def _parse(**_: Any) -> Any:
        return _Response()

    client = _client()
    client._client.responses.parse = _parse  # type: ignore[method-assign]
    with pytest.raises(LLMResponseInvalidError) as caught:
        await client.parse(instructions="i", user_input="u", schema=_Shape)

    assert caught.value.http_status == HTTPStatus.BAD_GATEWAY


async def test_a_schema_violation_is_response_invalid() -> None:
    class _Response:
        output_parsed: ClassVar[dict[str, Any]] = {"value": ["not", "a", "string"]}

    async def _parse(**_: Any) -> Any:
        return _Response()

    client = _client()
    client._client.responses.parse = _parse  # type: ignore[method-assign]
    with pytest.raises(LLMResponseInvalidError):
        await client.parse(instructions="i", user_input="u", schema=_Shape)


# ── validation failures, wherever they are raised ───────────────────────────
#
# The SDK validates the structured answer INSIDE `responses.parse`, so a model
# validator that rejects a provider-schema-valid answer raises there, not at
# the explicit `model_validate` below it. Both are the same failure and both
# must leave as `LLMResponseInvalidError`.


class _CrossChecked(BaseModel):
    """A shape whose fields are individually fine and jointly contradictory."""

    left: int = 0
    right: int = 0

    @model_validator(mode="after")
    def _must_agree(self) -> _CrossChecked:
        if self.left != self.right:
            raise ValueError("left and right must agree")
        return self


def _real_validation_error() -> ValidationError:
    """A genuine ValidationError, produced the way the SDK produces one."""
    try:
        _CrossChecked.model_validate_json('{"left": 1, "right": 2}')
    except ValidationError as exc:
        return exc
    raise AssertionError("the fixture stopped being invalid")


def test_the_public_pydantic_type_catches_what_validation_actually_raises() -> None:
    """The adapter catches `pydantic.ValidationError`, not a private type.

    Guards the coupling rule: if the public re-export ever stopped covering
    what validation raises, the adapter's except clause would silently stop
    matching and this fails instead.
    """
    import pydantic

    error = _real_validation_error()

    assert isinstance(error, pydantic.ValidationError)
    assert type(error).__module__.startswith(("pydantic", "pydantic_core"))


async def test_validation_inside_responses_parse_is_response_invalid() -> None:
    """Case A: the SDK validated and rejected the answer before returning."""
    with pytest.raises(LLMResponseInvalidError) as caught:
        await _call(_real_validation_error())

    assert caught.value.http_status == HTTPStatus.BAD_GATEWAY


async def test_validation_after_responses_parse_is_response_invalid() -> None:
    """Case B: the SDK returned a payload the explicit validation rejects."""

    class _Response:
        output_parsed: ClassVar[dict[str, Any]] = {"left": 1, "right": 2}

    async def _parse(**_: Any) -> Any:
        return _Response()

    client = _client()
    client._client.responses.parse = _parse  # type: ignore[method-assign]
    with pytest.raises(LLMResponseInvalidError) as caught:
        await client.parse(instructions="i", user_input="u", schema=_CrossChecked)

    assert caught.value.http_status == HTTPStatus.BAD_GATEWAY


@pytest.mark.parametrize("where", ["inside_parse", "after_parse"])
async def test_no_raw_validation_error_escapes_the_structured_client(
    where: str,
) -> None:
    """The caller's contract: a typed error, never the provider library's."""

    class _Response:
        output_parsed: ClassVar[dict[str, Any]] = {"left": 1, "right": 2}

    async def _parse(**_: Any) -> Any:
        if where == "inside_parse":
            raise _real_validation_error()
        return _Response()

    client = _client()
    client._client.responses.parse = _parse  # type: ignore[method-assign]
    with pytest.raises(LLMResponseInvalidError) as caught:
        await client.parse(instructions="i", user_input="u", schema=_CrossChecked)

    # The `raises` type is half the assertion; this is the other half.
    assert not isinstance(caught.value, ValidationError)
    assert isinstance(caught.value.__cause__, ValidationError)


async def test_a_validation_failure_is_never_retried() -> None:
    """One logical call. No repair prompt, no second attempt, no JSON mode."""
    attempts = 0

    async def _parse(**_: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise _real_validation_error()

    client = _client()
    client._client.responses.parse = _parse  # type: ignore[method-assign]
    with pytest.raises(LLMResponseInvalidError):
        await client.parse(instructions="i", user_input="u", schema=_CrossChecked)

    assert attempts == 1


async def test_a_validation_failure_says_nothing_about_the_payload() -> None:
    """The customer's words can reach a validation message; ours may not
    carry them out of the adapter."""
    with pytest.raises(LLMResponseInvalidError) as caught:
        await _call(_real_validation_error())

    assert caught.value.context["reason"] == "schema violation"
    assert "left" not in str(caught.value)


@pytest.mark.parametrize(
    ("exc_factory", "expected"),
    [
        (lambda: APITimeoutError(
            httpx.Request("POST", "https://api.openai.test/v1/responses")
        ), LLMUnavailableError),
        (lambda: _status_error(BadRequestError, 400), LLMRequestError),
        (lambda: _status_error(RateLimitError, 429), LLMUnavailableError),
        (lambda: OpenAIError("other"), LLMUnavailableError),
    ],
)
async def test_the_other_mappings_are_unchanged_by_the_validation_clause(
    exc_factory: Any, expected: type[Exception]
) -> None:
    """Categories stay distinct: nothing collapsed into schema violation."""
    with pytest.raises(expected) as caught:
        await _call(exc_factory())

    assert not isinstance(caught.value, LLMResponseInvalidError)


# ── nothing provider-specific escapes ───────────────────────────────────────


def test_domain_code_does_not_import_the_provider_sdk() -> None:
    """Provider exception handling stays inside the integration boundary.

    Checks imports rather than mentions: a redaction rule naming
    `openai_api_key` is exactly the sort of reference that should stay.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).parents[2] / "app"
    for module in root.rglob("*.py"):
        # The integration package IS the provider boundary; everything outside
        # it must reach a provider only through these adapters.
        if module.parent.name == "integrations":
            continue
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] != "openai", f"{module.name}: {name}"


def test_the_mapping_does_not_branch_on_model_name() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/integrations/llm.py").read_text()
    for token in ("gpt-", "sol", "o3", "claude"):
        assert token not in source, token


TRACE_HEADER = "X-Request-ID"


def _app() -> FastAPI:
    router = APIRouter()

    @router.get("/unavailable")
    async def unavailable() -> None:
        raise LLMUnavailableError(provider="openai", status_code=503)

    @router.get("/rejected")
    async def rejected() -> None:
        raise LLMRequestError(provider="openai", status_code=400, detail=PROVIDER_DETAIL)

    @router.get("/invalid")
    async def invalid() -> None:
        raise LLMResponseInvalidError(reason=PROVIDER_DETAIL)

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(TraceContextMiddleware, header=TRACE_HEADER)
    app.include_router(router)
    return app


@pytest_asyncio.fixture
async def api() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/unavailable", 503, "llm_unavailable"),
        ("/rejected", 500, "llm_request_error"),
        ("/invalid", 502, "llm_response_invalid"),
    ],
)
async def test_each_failure_maps_to_its_own_status(
    api: AsyncClient, path: str, status: int, code: str
) -> None:
    response = await api.get(path)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("path", ["/unavailable", "/rejected", "/invalid"])
async def test_no_provider_detail_reaches_the_caller(
    api: AsyncClient, path: str
) -> None:
    response = await api.get(path)
    error = response.json()["error"]
    carried = f"{error['code']} {error['message']}"

    assert "sk-live-abc123" not in response.text
    assert "temperature" not in carried
    assert "openai" not in carried.lower()
    assert "Traceback" not in response.text
    assert set(error) == {"code", "message", "trace_id"}
