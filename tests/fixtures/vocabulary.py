"""The vocabulary for tests - a snapshot of the database tables.

The application reads the vocabulary only from the database
(`app/repositories/vocabulary.py`); unit tests must not depend on it, so they
build it from this snapshot the same way the application does
(`build_vocabulary`). Refresh it after the tables change:

    uv run python -m tests.fixtures.vocabulary --refresh
"""

from __future__ import annotations

import asyncio
import json
import sys
from functools import cache
from pathlib import Path

from app.taxonomy.attributes import CatalogAttributes
from app.taxonomy.registry import CommerceTaxonomy
from app.taxonomy.tables import BuiltVocabulary, VocabularyTables, WaitingRow, build_vocabulary

SNAPSHOT = Path(__file__).parent / "vocabulary.json"


@cache
def snapshot() -> VocabularyTables:
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return VocabularyTables.model_validate(document["tables"])


@cache
def _built() -> BuiltVocabulary:
    return build_vocabulary(snapshot())


def load_taxonomy() -> CommerceTaxonomy:
    return _built().taxonomy


def load_catalog_attributes() -> CatalogAttributes:
    return _built().attributes


def with_waiting(**values: list[str]) -> CatalogAttributes:
    """The snapshot's attributes with values waiting for their Arabic name -
    live, in English - by table kind: `with_waiting(colour=["Mauve"])`."""
    waiting = tuple(
        WaitingRow(kind=kind, name=name) for kind, names in values.items() for name in names
    )
    tables = snapshot().model_copy(update={"waiting": (*snapshot().waiting, *waiting)})
    return build_vocabulary(tables).attributes


async def _refresh() -> None:
    from app.core.config import get_settings
    from app.integrations.postgres import Database
    from app.repositories.vocabulary import VocabularyRepository

    database = Database.create(get_settings().db)
    try:
        async with database.session() as session:
            tables = await VocabularyRepository(session).tables()
    finally:
        await database.dispose()
    SNAPSHOT.write_text(
        json.dumps(
            {
                "note": "A snapshot of the vocabulary tables; refresh with "
                "`uv run python -m tests.fixtures.vocabulary --refresh`.",
                "tables": tables.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__" and "--refresh" in sys.argv:
    asyncio.run(_refresh())
