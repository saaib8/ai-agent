"""Redaction and trace binding — the two things logging must never get wrong."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from app.core.config import ObservabilitySettings
from app.core.logging import (
    REDACTED,
    bind_request_context,
    clear_request_context,
    configure_logging,
    current_trace_id,
    get_logger,
    new_trace_id,
    redact_processor,
)
from pydantic import SecretStr


def _process(event: dict[str, Any]) -> dict[str, Any]:
    return dict(redact_processor(None, "info", event))


def test_secret_bearing_keys_are_redacted() -> None:
    processed = _process(
        {
            "event": "connected",
            "db_dsn": "postgresql+asyncpg://user:hunter2@host/db",
            "openai_api_key": "sk-live-123",
            "authorization": "Bearer abc",
            "store_id": 42,
        }
    )

    assert processed["db_dsn"] == REDACTED
    assert processed["openai_api_key"] == REDACTED
    assert processed["authorization"] == REDACTED
    assert processed["store_id"] == 42
    assert processed["event"] == "connected"


def test_secretstr_values_never_render() -> None:
    processed = _process({"event": "boot", "value": SecretStr("hunter2")})
    assert processed["value"] == REDACTED


def test_redaction_reaches_into_nested_structures() -> None:
    processed = _process(
        {
            "event": "boot",
            "config": {"pool_size": 10, "password": "hunter2", "nested": {"token": "t"}},
            "items": [{"secret": "s"}],
        }
    )

    assert processed["config"]["pool_size"] == 10
    assert processed["config"]["password"] == REDACTED
    assert processed["config"]["nested"]["token"] == REDACTED
    assert processed["items"][0]["secret"] == REDACTED


def test_request_context_binds_and_clears() -> None:
    clear_request_context()
    assert current_trace_id() is None

    trace_id = new_trace_id()
    bind_request_context(trace_id=trace_id, store_id=7, session_id="s-1")
    assert current_trace_id() == trace_id

    clear_request_context()
    assert current_trace_id() is None


def test_trace_ids_are_unique() -> None:
    assert new_trace_id() != new_trace_id()


def test_configure_logging_actually_emits_a_json_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercises the real pipeline: a misconfigured processor chain raises here."""
    configure_logging(ObservabilitySettings(log_level="INFO", log_format="json"))
    clear_request_context()
    bind_request_context(trace_id="trace-abc", store_id=7)

    get_logger("test.logger").info("service_starting", db_dsn="postgres://u:pw@h/db")

    emitted = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert emitted["event"] == "service_starting"
    assert emitted["trace_id"] == "trace-abc"
    assert emitted["store_id"] == 7
    assert emitted["level"] == "info"
    assert emitted["logger"] == "test.logger"
    assert emitted["db_dsn"] == REDACTED
    assert "pw" not in capsys.readouterr().out

    clear_request_context()


def test_stdlib_loggers_render_through_the_same_pipeline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """uvicorn/SQLAlchemy events must not break the machine-readable stream."""
    configure_logging(ObservabilitySettings(log_level="INFO", log_format="json"))

    logging.getLogger("uvicorn.error").warning("third party said %s", "hello")

    emitted = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert emitted["event"] == "third party said hello"
    assert emitted["level"] == "warning"


def test_console_format_is_configurable(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(ObservabilitySettings(log_level="INFO", log_format="console"))
    get_logger("test.logger").info("hello_console")

    assert "hello_console" in capsys.readouterr().out


def test_library_loggers_are_rerouted_into_the_pipeline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """uvicorn installs its own handlers; unstructured lines must not leak."""
    access = logging.getLogger("uvicorn.access")
    access.addHandler(logging.StreamHandler())
    access.propagate = False

    configure_logging(ObservabilitySettings(log_level="INFO", log_format="json"))

    assert access.handlers == []
    assert access.propagate is True

    access.info("GET /health 200")
    emitted = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert emitted["event"] == "GET /health 200"
