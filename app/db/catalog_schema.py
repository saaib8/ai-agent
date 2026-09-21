"""Verifies the live catalog has the commerce columns this service requires.

Checked once at startup. A reachable catalog that is missing a required column
is a deployment error and fails startup loudly, rather than degrading into
queries that silently return nothing.

``material`` is deferred and is deliberately not checked.
"""

from __future__ import annotations

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.exceptions import CatalogSchemaError
from app.db.tables import REQUIRED_COMMERCE_COLUMNS, core_product

_COLUMN_PRESENCE = text(
    """
    SELECT column_name
    FROM information_schema.columns
    WHERE table_name = :table_name
      AND column_name IN :column_names
    """
).bindparams(bindparam("column_names", expanding=True))


async def present_commerce_columns(connection: AsyncConnection) -> frozenset[str]:
    """The subset of :data:`REQUIRED_COMMERCE_COLUMNS` the live table has."""
    result = await connection.execute(
        _COLUMN_PRESENCE,
        {
            "table_name": core_product.name,
            "column_names": sorted(REQUIRED_COMMERCE_COLUMNS),
        },
    )
    return frozenset(row[0] for row in result)


async def verify_commerce_schema(connection: AsyncConnection) -> None:
    """Raise :class:`CatalogSchemaError` if a required commerce column is absent."""
    missing = REQUIRED_COMMERCE_COLUMNS - await present_commerce_columns(connection)
    if missing:
        raise CatalogSchemaError(
            detail=(
                f"{core_product.name} is missing required column(s): "
                f"{', '.join(sorted(missing))}. The Django catalog migration "
                f"has probably not been applied to this database."
            ),
            missing_columns=sorted(missing),
        )
