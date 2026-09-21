"""Integration fixtures: a real PostgreSQL, or a skip.

The catalog tables are created from the read-only projection in
``app.db.tables``, so the fixture and the queries under test cannot drift: if
the projection stops matching a real ``core_product``, these tests stop
building it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import DatabaseSettings
from app.db.tables import metadata
from app.integrations.postgres import Database
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import INTEGRATION_DSN

pytestmark = pytest.mark.integration


async def _reachable(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect():
            return True
    except (SQLAlchemyError, OSError):
        return False


@pytest_asyncio.fixture
async def writable_engine() -> AsyncIterator[AsyncEngine]:
    """A writable engine used only to build and seed the fixture schema."""
    engine = create_async_engine(INTEGRATION_DSN, poolclass=None)
    if not await _reachable(engine):
        await engine.dispose()
        pytest.skip("PostgreSQL is not running (see docker-compose.yml)")

    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.run_sync(metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def database(writable_engine: AsyncEngine) -> AsyncIterator[Database]:
    """The service's own read-only database handle."""
    settings = DatabaseSettings(dsn=SecretStr(INTEGRATION_DSN), read_only=True)
    instance = Database.create(settings)
    try:
        yield instance
    finally:
        await instance.dispose()
