"""Structured logging with request-scoped trace context.

One configuration entry point (:func:`configure_logging`) wires structlog over
the stdlib root logger so third-party libraries emit through the same pipeline.
Every event carries the current ``trace_id``/``session_id``/``store_id`` when
one is bound, which is what makes a request diagnosable after the fact
(CLAUDE.md 22).

A redaction processor runs last-but-one: secrets must never reach a log sink,
even if a caller passes one by accident.
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Any

import structlog
from pydantic import SecretStr

from app.core.config import ObservabilitySettings

REDACTED = "***"

# Substrings that mark a key as secret-bearing. Matched case-insensitively
# against the whole key, so "db_dsn" and "OPENAI_API_KEY" are both caught.
_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "dsn",
    "access_key",
    "private_key",
)


# Loggers that ship with their own handlers and `propagate = False`.
_LIBRARIES_WITH_OWN_HANDLERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
)


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_sensitive(str(k)) else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(item) for item in value)
    return value


def redact_processor(
    _logger: Any, _method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Drop secret-bearing values before an event reaches any sink."""
    return {
        key: (REDACTED if _is_sensitive(str(key)) else _redact_value(value))
        for key, value in event_dict.items()
    }


def configure_logging(settings: ObservabilitySettings) -> None:
    """Configure structlog on top of stdlib logging. Safe to call repeatedly.

    Everything — this service's events and third-party ones from uvicorn,
    SQLAlchemy and asyncpg — is rendered by a single handler, so the output
    stream stays uniformly machine-parseable.
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_processor,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
            foreign_pre_chain=shared,
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # These libraries install their own handlers and stop propagating, which
    # would leave unstructured lines interleaved in the output stream.
    for name in _LIBRARIES_WITH_OWN_HANDLERS:
        library_logger = logging.getLogger(name)
        library_logger.handlers.clear()
        library_logger.propagate = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def new_trace_id() -> str:
    return uuid.uuid4().hex


def bind_request_context(
    *, trace_id: str, store_id: int | None = None, session_id: str | None = None
) -> None:
    """Bind request-scoped fields onto every subsequent log event."""
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    if store_id is not None:
        structlog.contextvars.bind_contextvars(store_id=store_id)
    if session_id is not None:
        structlog.contextvars.bind_contextvars(session_id=session_id)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()


def current_trace_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("trace_id")
    return value if isinstance(value, str) else None
