"""How the service behaves when a dependency is simply not there.

Release QA found that PostgreSQL outages were handled differently from Redis
ones, and worse. asyncpg raises a bare `ConnectionRefusedError` at connect
time, which SQLAlchemy does not wrap - so code catching only `SQLAlchemyError`
let it escape untranslated.

Three things went wrong because of it, and each has a test here:

* startup crashed instead of coming up and reporting itself unhealthy, so a
  database blip during a rolling deploy would crash-loop the service;
* `/health` answered 500 instead of naming the dependency that was down, which
  is the one job it exists to do;
* a chat turn answered 500 rather than the retryable 503 the Redis path gives,
  telling a client "this is a bug" when the truth was "try again".

The sockets here point at port 1, where nothing listens.
"""

from __future__ import annotations

import asyncio

import pytest
from app.core.exceptions import CatalogUnavailableError
from app.integrations.postgres import Database
from app.services.health import HealthService

from tests.conftest import build_settings

DEAD = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none"


def a_database() -> Database:
    return Database.create(build_settings(db={"dsn": DEAD}).db)


class LiveRedis:
    """Redis is fine; only PostgreSQL is away."""

    async def check_health(self) -> bool:
        return True


async def test_the_probe_reports_a_refused_connection_rather_than_raising() -> None:
    """Catching only SQLAlchemy's errors made this crash on exactly the
    condition it exists to detect."""
    database = a_database()
    try:
        assert await database.check_health() is False
    finally:
        await database.dispose()


async def test_health_names_the_dependency_that_is_down() -> None:
    database = a_database()
    try:
        report = await HealthService(database, LiveRedis()).check()  # type: ignore[arg-type]
    finally:
        await database.dispose()

    assert report.healthy is False
    assert report.dependencies["postgres"] == "down"
    assert report.dependencies["redis"] == "up"


async def test_an_unreachable_catalog_is_a_retryable_failure() -> None:
    """503, like Redis - not a 500 that tells the client to stop retrying."""
    database = a_database()
    try:
        with pytest.raises(CatalogUnavailableError) as raised:
            async with database.session() as session:
                from sqlalchemy import text

                await session.execute(text("select 1"))
    finally:
        await database.dispose()

    assert raised.value.http_status == 503


async def test_the_failure_names_no_credentials() -> None:
    """The DSN carries a password; the public message must not."""
    database = Database.create(
        build_settings(db={"dsn": "postgresql+asyncpg://someone:hunter2@127.0.0.1:1/none"}).db
    )
    try:
        with pytest.raises(CatalogUnavailableError) as raised:
            async with database.session() as session:
                from sqlalchemy import text

                await session.execute(text("select 1"))
    finally:
        await database.dispose()

    assert "hunter2" not in raised.value.public_message
    assert "someone" not in raised.value.public_message


async def test_startup_survives_a_catalog_that_is_briefly_away() -> None:
    """A blip during a rolling deploy must not crash-loop the process.

    The service comes up and reports itself unhealthy; a *reachable* catalog
    missing a required column still fails startup, which is a deployment
    mistake rather than a blip.
    """
    from app.core.lifespan import _check_catalog_schema

    database = a_database()
    try:
        await asyncio.wait_for(_check_catalog_schema(database), timeout=10)
    finally:
        await database.dispose()
