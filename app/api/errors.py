"""Maps internal failures onto safe responses.

Callers receive a stable code, a message we wrote, and the trace id. Driver
messages, SQL, stack traces and validation internals stay in the logs
(CLAUDE.md 20.5, 21).
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import InvalidRequestError, ZoryError
from app.core.logging import get_logger
from app.schemas.errors import ErrorBody, ErrorResponse

logger = get_logger(__name__)


def _trace_id(request: Request) -> str | None:
    value = getattr(request.state, "trace_id", None)
    return value if isinstance(value, str) else None


def _trace_header(request: Request) -> str | None:
    value = getattr(request.state, "trace_header", None)
    return value if isinstance(value, str) else None


def _response(request: Request, *, status: int, code: str, message: str) -> JSONResponse:
    trace_id = _trace_id(request)
    body = ErrorResponse(error=ErrorBody(code=code, message=message, trace_id=trace_id))
    response = JSONResponse(status_code=status, content=body.model_dump())
    header = _trace_header(request)
    if header and trace_id:
        response.headers[header] = trace_id
    return response


async def handle_zory_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ZoryError)
    log = logger.bind(
        trace_id=_trace_id(request),
        error_code=exc.code,
        path=request.url.path,
        method=request.method,
        **exc.context,
    )
    if exc.http_status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        log.error("request_failed", exc_info=exc)
    else:
        log.info("request_rejected")
    return _response(
        request, status=exc.http_status, code=exc.code, message=exc.public_message
    )


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Refuse malformed input without echoing the payload back.

    FastAPI's default handler returns the offending values, which would reflect
    untrusted input — and anything a caller smuggled into it — straight back out.
    """
    logger.info(
        "request_validation_failed",
        trace_id=_trace_id(request),
        path=request.url.path,
        method=request.method,
        error_count=len(exc.errors()) if isinstance(exc, RequestValidationError) else 0,
    )
    fallback = InvalidRequestError()
    return _response(
        request,
        status=fallback.http_status,
        code=fallback.code,
        message=fallback.public_message,
    )


async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    status = HTTPStatus(exc.status_code)
    return _response(
        request,
        status=exc.status_code,
        code=status.name.lower(),
        message=status.phrase,
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Last resort. The detail is logged; the caller gets none of it."""
    logger.error(
        "unhandled_exception",
        trace_id=_trace_id(request),
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
        exc_info=exc,
    )
    generic = ZoryError()
    return _response(
        request,
        status=generic.http_status,
        code=generic.code,
        message=generic.public_message,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ZoryError, handle_zory_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected_error)
