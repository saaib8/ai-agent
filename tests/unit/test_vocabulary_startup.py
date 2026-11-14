"""The vocabulary at runtime (CLAUDE.md 14.1): built from the tables as they
are, read at startup and checked every few minutes, swapped whole on a change -
and never a reason for the service to stop while there is a copy to serve."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.core import vocabulary as runtime
from app.core.exceptions import CatalogUnavailableError, TaxonomyConfigurationError
from app.core.vocabulary import VocabularyRefresher, team_models
from app.integrations.postgres import Database
from app.repositories import vocabulary as repository
from app.repositories.vocabulary_copy import VocabularyCopy
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.tables import (
    FamilyRow,
    KindRow,
    ValueRow,
    VocabularyTables,
    WaitingRow,
    build_vocabulary,
)
from fastapi import FastAPI

from tests.conftest import build_settings
from tests.fixtures.vocabulary import snapshot

SETTINGS = build_settings(vocabulary={"startup_wait_s": 0.05, "retry_every_s": 0.01})


# ── the tables, trusted ─────────────────────────────────────────────────────


def _with(**rows: Any) -> VocabularyTables:
    tables = snapshot()
    return tables.model_copy(
        update={name: (*getattr(tables, name), *extra) for name, extra in rows.items()}
    )


def test_rows_the_old_checks_refused_are_taken_as_they_are() -> None:
    tables = _with(
        kinds=(KindRow(name="Floor Lamp", family="lighting", arabic=" "),),
        values=(
            ValueRow(kind="colour", name="light_grey", arabic="رمادي"),
            ValueRow(kind="colour", name="Mauve", arabic=""),
        ),
    )

    built = build_vocabulary(tables)

    assert built.taxonomy.is_pair("lighting", "Floor Lamp")
    assert built.taxonomy.arabic("Floor Lamp") is None, "read in English"
    assert "Mauve" in built.attributes.colors
    assert built.attributes.arabic(AttributeFamily.COLOR, "Mauve") is None
    assert "light_grey" not in built.attributes.colors, "a spelling twin keeps the first"
    assert built.attributes.canonical(AttributeFamily.COLOR, "light grey") == "Light Grey"


def test_a_row_that_cannot_be_used_is_left_out_not_refused() -> None:
    tables = _with(
        kinds=(KindRow(name="orphan-kind", family=None),),
        values=(ValueRow(kind="style", name="Mid Century", arabic="منتصف القرن"),),
    )

    built = build_vocabulary(tables)

    assert not built.taxonomy.is_subcategory("orphan-kind")
    assert "Mid Century" not in built.attributes.styles
    assert len(built.skipped) == 2


def test_a_waiting_value_is_live_in_english() -> None:
    tables = _with(
        waiting=(
            WaitingRow(kind="type", name="bar-stool", family="seating"),
            WaitingRow(kind="colour", name="Mauve"),
            WaitingRow(kind="type", name="lost-kind", family="no-such-family"),
        )
    )

    built = build_vocabulary(tables)

    assert built.taxonomy.is_pair("seating", "bar-stool")
    assert built.taxonomy.arabic("bar-stool") is None
    assert "Mauve" in built.attributes.colors
    assert not built.taxonomy.is_subcategory("lost-kind")
    assert "bar-stool" in built.in_english["type"]


def test_empty_tables_cannot_serve() -> None:
    with pytest.raises(CatalogUnavailableError):
        build_vocabulary(VocabularyTables(families=(FamilyRow(name="seating"),)))


def test_the_version_follows_the_content() -> None:
    changed = _with(values=(ValueRow(kind="colour", name="Mauve", arabic="موف"),))

    assert snapshot().version.startswith("db-")
    assert changed.version != snapshot().version
    assert VocabularyTables.model_validate_json(changed.model_dump_json()).version == (
        changed.version
    )


# ── startup and the check ───────────────────────────────────────────────────


class _Database:
    """Serves `tables` - or refuses, as an unreachable database does."""

    def __init__(self, tables: VocabularyTables | None) -> None:
        self.tables = tables
        self.reads = 0

    @asynccontextmanager
    async def session(self) -> Any:
        self.reads += 1
        if self.tables is None:
            raise CatalogUnavailableError(detail="refused")
        yield SimpleNamespace(tables=self.tables)


class _Redis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


@pytest.fixture(autouse=True)
def _served_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    async def tables(self: Any) -> VocabularyTables:
        served: VocabularyTables = self._session.tables
        return served

    monkeypatch.setattr(repository.VocabularyRepository, "tables", tables)


def _refresher(
    database: _Database, redis: _Redis | None = None, models: Any = None
) -> VocabularyRefresher:
    copy = VocabularyCopy(cast(Any, redis or _Redis()), timeout_s=1)
    return VocabularyRefresher(cast(Database, database), copy, SETTINGS, models)


async def test_startup_reads_the_database_and_saves_the_copy() -> None:
    redis = _Redis()
    refresher = _refresher(_Database(snapshot()), redis)

    build = await refresher.at_startup()

    assert build.taxonomy.is_pair("lighting", "ceiling-lighting")
    assert refresher.version == snapshot().version
    assert await VocabularyCopy(cast(Any, redis), 1).load() == snapshot()


async def test_a_database_down_at_startup_starts_from_the_copy() -> None:
    redis = _Redis()
    await VocabularyCopy(cast(Any, redis), 1).save(snapshot())
    database = _Database(None)

    build = await _refresher(database, redis).at_startup()

    assert build.attributes.arabic(AttributeFamily.COLOR, "Beige")
    assert database.reads == 1, "the copy is used at once, not after a wait"


async def test_a_database_coming_up_is_waited_for_when_there_is_no_copy() -> None:
    database = _Database(None)
    refresher = _refresher(database)

    original = runtime.VocabularyRefresher._read
    calls = 0

    async def read(self: VocabularyRefresher) -> VocabularyTables:
        nonlocal calls
        calls += 1
        if calls == 3:
            database.tables = snapshot()
        return await original(self)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime.VocabularyRefresher, "_read", read)
        build = await refresher.at_startup()

    assert build.taxonomy.is_category("seating")
    assert calls == 3


async def test_no_database_and_no_copy_fails_startup_after_retries() -> None:
    database = _Database(None)

    with pytest.raises(CatalogUnavailableError):
        await _refresher(database).at_startup()
    assert database.reads > 1, "it was retried before giving up"


async def test_tables_that_clash_with_a_reviewed_file_start_from_the_copy() -> None:
    redis = _Redis()
    await VocabularyCopy(cast(Any, redis), 1).save(snapshot())
    clashing = snapshot().model_copy(
        update={"kinds": tuple(k for k in snapshot().kinds if k.name != "sofa")}
    )

    build = await _refresher(_Database(clashing), redis).at_startup()

    assert build.taxonomy.is_subcategory("sofa")


async def test_tables_that_clash_and_no_copy_fail_startup() -> None:
    clashing = snapshot().model_copy(
        update={"kinds": tuple(k for k in snapshot().kinds if k.name != "sofa")}
    )

    with pytest.raises(TaxonomyConfigurationError):
        await _refresher(_Database(clashing)).at_startup()


def _app(refresher: VocabularyRefresher, build: runtime.VocabularyBuild) -> FastAPI:
    # The build stands in for the app's resources: both are dataclasses
    # holding the same vocabulary fields.
    app = FastAPI()
    app.state.resources = build
    return app


async def test_a_check_with_no_change_keeps_what_is_in_use() -> None:
    refresher = _refresher(_Database(snapshot()))
    app = _app(refresher, await refresher.at_startup())
    before = app.state.resources

    assert await refresher.check(app) is False
    assert app.state.resources is before


async def test_a_new_type_is_swapped_in_and_saved() -> None:
    database, redis = _Database(snapshot()), _Redis()
    refresher = _refresher(database, redis)
    app = _app(refresher, await refresher.at_startup())
    before = app.state.resources
    database.tables = _with(kinds=(KindRow(name="bar-stool", family="seating", arabic="بار"),))

    assert await refresher.check(app) is True

    assert app.state.resources.taxonomy.is_pair("seating", "bar-stool")
    assert not before.taxonomy.is_subcategory("bar-stool"), "a turn under way keeps its own"
    assert refresher.version == database.tables.version
    copy = await VocabularyCopy(cast(Any, redis), 1).load()
    assert copy is not None and copy.version == database.tables.version


async def test_a_database_down_during_a_check_keeps_what_is_in_use() -> None:
    database = _Database(snapshot())
    refresher = _refresher(database)
    app = _app(refresher, await refresher.at_startup())
    before = app.state.resources
    database.tables = None

    assert await refresher.check(app) is False
    assert app.state.resources is before


async def test_a_change_that_clashes_is_not_applied() -> None:
    database = _Database(snapshot())
    refresher = _refresher(database)
    app = _app(refresher, await refresher.at_startup())
    before = app.state.resources
    database.tables = snapshot().model_copy(
        update={"kinds": tuple(k for k in snapshot().kinds if k.name != "sofa")}
    )

    assert await refresher.check(app) is False
    assert app.state.resources is before
    assert refresher.version == snapshot().version


async def test_a_change_rebuilds_the_tools_and_keeps_the_model_clients() -> None:
    roles = {"model": "test-model"}
    settings = build_settings(
        agents=dict.fromkeys(("supervisor", "sara", "nora", "salman", "faysal"), roles)
    )
    models = team_models(settings)
    assert models is not None
    database = _Database(snapshot())
    refresher = VocabularyRefresher(
        cast(Database, database),
        VocabularyCopy(cast(Any, _Redis()), 1),
        settings,
        models,
    )
    app = _app(refresher, await refresher.at_startup())
    first = app.state.resources.agent_team
    database.tables = _with(kinds=(KindRow(name="bar-stool", family="seating", arabic="بار"),))

    await refresher.check(app)

    second = app.state.resources.agent_team
    assert second is not first
    assert second._agents == first._agents, "the same model clients"
