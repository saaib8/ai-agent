"""Shared test configuration.

Unit tests never touch PostgreSQL, Redis or AWS. Integration tests are marked
and skip themselves when the local dependencies from docker-compose.yml are
not running.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from app.core.config import Settings

# A DSN that parses and validates but is never dialled by unit tests.
UNIT_TEST_DSN = "postgresql+asyncpg://zory:zory@localhost:55432/zory_unit"
UNIT_TEST_REDIS_URL = "redis://localhost:56379/15"

# Integration tests use a real database. Overridable so CI can point elsewhere.
INTEGRATION_DSN = os.environ.get(
    "ZORY_TEST_DB_DSN", "postgresql+asyncpg://zory:zory@localhost:55432/zory"
)
INTEGRATION_REDIS_URL = os.environ.get("ZORY_TEST_REDIS_URL", "redis://localhost:56379/15")


def build_settings(**overrides: Any) -> Settings:
    """Settings built from explicit values only.

    ``_env_file=None`` keeps a developer's local ``.env`` from leaking into the
    test run and making results machine-dependent.
    """
    values: dict[str, Any] = {
        "environment": "test",
        "db": {"dsn": UNIT_TEST_DSN},
        "redis": {"url": UNIT_TEST_REDIS_URL},
        "llm": {"api_key": "test-key-not-real", "model": "test-model"},
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings() -> Settings:
    return build_settings()
