"""PostgreSQL access: one lifespan-managed engine and pool per process.

The catalog schema belongs to the Django platform. This service reads it and
nothing else, which is enforced twice over: the repositories issue only
``SELECT`` expressions, and — unless explicitly disabled in settings — every
connection opens with ``default_transaction_read_only``, so PostgreSQL itself
rejects a write (CLAUDE.md 4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import DatabaseSettings
from app.core.exceptions import CatalogUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class Database:
    """Owns the async engine and hands out sessions. Created once, in lifespan."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @classmethod
    def create(cls, settings: DatabaseSettings) -> Database:
        connect_args: dict[str, Any] = {
            "timeout": settings.connect_timeout_s,
            "command_timeout": settings.command_timeout_s,
        }
        if settings.read_only:
            connect_args["server_settings"] = {"default_transaction_read_only": "on"}

        engine = create_async_engine(
            settings.dsn.get_secret_value(),
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            pool_timeout=settings.pool_timeout_s,
            pool_recycle=settings.pool_recycle_s,
            pool_pre_ping=settings.pool_pre_ping,
            echo=settings.echo_sql,
            connect_args=connect_args,
        )
        return cls(engine)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session scoped to one unit of work.

        Read-only by design, so there is no commit: the transaction is rolled
        back on exit whatever happened, which releases locks promptly.
        """
        async with self._sessionmaker() as session:
            try:
                yield session
            except OSError as exc:
                # asyncpg raises a bare ConnectionRefusedError, TimeoutError or
                # gaierror when the server is unreachable, and SQLAlchemy does
                # not wrap those at connect time. Untranslated they escape as
                # unexpected errors and a transient outage answers 500 rather
                # than the retryable 503 the Redis path already gives
                # (CLAUDE.md 21).
                logger.warning("postgres_unreachable", error_type=type(exc).__name__)
                raise CatalogUnavailableError(dependency="postgres") from exc
            finally:
                await session.rollback()

    async def check_health(self) -> bool:
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError) as exc:
            # `OSError` too, for the same reason the session translates it: a
            # refused connection is exactly what this probe exists to report,
            # and catching only SQLAlchemy's own errors made the probe *crash*
            # on the one condition it is meant to detect - so /health answered
            # 500 instead of naming the dependency that was down.
            logger.warning("postgres_health_check_failed", error_type=type(exc).__name__)
            return False
        return True

    async def dispose(self) -> None:
        await self._engine.dispose()


async def require_healthy(database: Database) -> None:
    """Raise the public-safe catalog error when PostgreSQL is unreachable."""
    if not await database.check_health():
        raise CatalogUnavailableError(dependency="postgres")
