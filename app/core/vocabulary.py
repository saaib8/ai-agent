"""The vocabulary at runtime: read at startup, checked every few minutes, and
swapped whole when it changes (CLAUDE.md 14.1).

Everything built from it - the taxonomy, the attributes, the reviewed files
checked against them and the agent team whose tools list the allowed values -
is one `VocabularyBuild`, and it lives on the app's resources. Every request
takes the resources once (`app/api/dependencies.py`), so a turn uses one
vocabulary from start to end; a change replaces the resources in one step and
the next request takes the new one.

What can go wrong never stops the service:

* the database cannot be read at startup: start from the last good copy;
* it cannot be read during a check: keep what is in use;
* the tables clash with a reviewed file (only a hand edit can remove a type a
  file names): keep what is in use, and log which file and type.

Only a first start with no database and no copy waits, then fails - there is
nothing to build the agents' word lists from.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from langchain_core.language_models import BaseChatModel
from sqlalchemy.exc import SQLAlchemyError

from app.agents.loop import AgentLoop
from app.agents.team import AgentTeam
from app.agents.tools.catalog import build_registry
from app.agents.tools.schemas import ToolSchemas
from app.core.config import Settings
from app.core.exceptions import IntegrationUnavailableError, TaxonomyConfigurationError
from app.core.logging import get_logger
from app.integrations.chat_models import build_chat_model, build_wording_model
from app.integrations.postgres import Database
from app.repositories.vocabulary import VocabularyRepository
from app.repositories.vocabulary_copy import VocabularyCopy
from app.schemas.agent_persona import AgentPersona
from app.schemas.query import build_constrained_interpretation
from app.taxonomy.attributes import CatalogAttributes
from app.taxonomy.compare_groups import CompareGroups, load_compare_groups
from app.taxonomy.complements import Complements, load_complements
from app.taxonomy.dimensions import DimensionSemantics, load_dimension_semantics
from app.taxonomy.registry import CommerceTaxonomy
from app.taxonomy.rooms import RoomPieces, load_room_pieces
from app.taxonomy.seating import SeatingSemantics, load_seating_semantics
from app.taxonomy.tables import VocabularyTables, build_vocabulary

logger = get_logger(__name__)

UNREADABLE = (SQLAlchemyError, OSError, IntegrationUnavailableError)
"""A database that cannot be read, or tables that hold nothing usable: the
database layer reports a refused connection as its own unavailable error."""


@dataclass(frozen=True, slots=True)
class TeamModels:
    """The supervisor's and each agent's model clients. Kept for the process:
    a vocabulary change rebuilds the team's tools, never its connections."""

    supervisor: BaseChatModel
    agents: Mapping[AgentPersona, BaseChatModel]
    wording: Mapping[AgentPersona, BaseChatModel]


def team_models(settings: Settings) -> TeamModels | None:
    """Every agent's model - or None when any of them has none, and chat then
    refuses as not configured."""
    agents = settings.agents
    use_responses = agents.use_responses_api
    supervisor = build_chat_model(settings.llm, agents.supervisor, use_responses_api=use_responses)
    models = {
        persona: build_chat_model(
            settings.llm, getattr(agents, persona.value), use_responses_api=use_responses
        )
        for persona in AgentPersona
    }
    if supervisor is None or any(model is None for model in models.values()):
        return None
    wording = {
        persona: model
        for persona in AgentPersona
        if (
            model := build_wording_model(
                settings.llm, getattr(agents, persona.value), use_responses_api=use_responses
            )
        )
        is not None
    }
    return TeamModels(
        supervisor=supervisor,
        agents={persona: model for persona, model in models.items() if model is not None},
        wording=wording,
    )


@dataclass(frozen=True, slots=True)
class VocabularyBuild:
    """Everything built from one vocabulary - the fields of the app's
    resources that a change replaces."""

    taxonomy: CommerceTaxonomy
    attributes: CatalogAttributes
    dimensions: DimensionSemantics
    seating: SeatingSemantics
    rooms: RoomPieces
    complements: Complements
    compare_groups: CompareGroups
    agent_team: AgentTeam | None

    def fields(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}


def build_from(
    tables: VocabularyTables, settings: Settings, models: TeamModels | None
) -> VocabularyBuild:
    """The vocabulary and everything checked against it. Raises
    `TaxonomyConfigurationError` when a reviewed file names a type the tables
    no longer hold, and the unavailable error when they hold none at all."""
    built = build_vocabulary(tables)
    taxonomy, attributes = built.taxonomy, built.attributes
    dimensions = load_dimension_semantics(
        taxonomy=taxonomy, by_side=settings.discovery.size_by_side
    )
    seating = load_seating_semantics(taxonomy=taxonomy)
    rooms = load_room_pieces(taxonomy=taxonomy, seating=seating)
    complements = load_complements(taxonomy=taxonomy)
    compare_groups = load_compare_groups(taxonomy=taxonomy)
    team = None
    if models is not None:
        registry = build_registry(
            ToolSchemas(
                interpretation=build_constrained_interpretation(taxonomy, attributes),
                attributes=attributes,
                room_pieces=rooms,
            )
        )
        team = AgentTeam(
            models.supervisor,
            models.agents,
            settings.agents,
            AgentLoop(registry, max_tool_calls=settings.agents.max_tool_calls),
            wording=models.wording,
        )
    if built.skipped:
        logger.warning("vocabulary_rows_skipped", rows=list(built.skipped))
    if built.in_english:
        # Live, and shown in English until the pipeline adds their Arabic.
        logger.warning("vocabulary_values_in_english", values=built.in_english)
    logger.info(
        "vocabulary_built",
        version=taxonomy.version,
        category_count=len(taxonomy.categories),
        subcategory_count=sum(len(taxonomy.subcategories(c)) for c in taxonomy.categories),
        color_count=len(attributes.colors),
        style_count=len(attributes.styles),
        material_count=len(attributes.materials),
    )
    return VocabularyBuild(
        taxonomy=taxonomy,
        attributes=attributes,
        dimensions=dimensions,
        seating=seating,
        rooms=rooms,
        complements=complements,
        compare_groups=compare_groups,
        agent_team=team,
    )


class VocabularyRefresher:
    def __init__(
        self,
        database: Database,
        copy: VocabularyCopy,
        settings: Settings,
        models: TeamModels | None,
    ) -> None:
        self._database = database
        self._copy = copy
        self._settings = settings
        self._models = models
        self._version: str | None = None

    @property
    def version(self) -> str | None:
        """The version of the tables in use."""
        return self._version

    async def _read(self) -> VocabularyTables:
        async with self._database.session() as session:
            return await VocabularyRepository(session).tables()

    def _build(self, tables: VocabularyTables) -> VocabularyBuild:
        return build_from(tables, self._settings, self._models)

    async def at_startup(self) -> VocabularyBuild:
        """The database's vocabulary; the last good copy when the database
        cannot give one; else the database, waited for."""
        try:
            tables = await self._read()
            build = self._build(tables)
        except (*UNREADABLE, TaxonomyConfigurationError) as exc:
            logger.warning("vocabulary_database_unusable", error_type=type(exc).__name__)
            from_copy = await self._from_copy()
            if from_copy is not None:
                return from_copy
            if isinstance(exc, TaxonomyConfigurationError):
                raise
            tables, build = await self._wait_for_database()
        self._version = tables.version
        await self._copy.save(tables)
        return build

    async def _from_copy(self) -> VocabularyBuild | None:
        tables = await self._copy.load()
        if tables is None:
            return None
        try:
            build = self._build(tables)
        except (*UNREADABLE, TaxonomyConfigurationError) as exc:
            logger.warning("vocabulary_copy_unusable", error_type=type(exc).__name__)
            return None
        self._version = tables.version
        logger.warning("vocabulary_started_from_copy", version=tables.version)
        return build

    async def _wait_for_database(self) -> tuple[VocabularyTables, VocabularyBuild]:
        """No copy to start from: the database is waited for, within
        `vocabulary.startup_wait_s`, then startup fails."""
        wait = self._settings.vocabulary
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait.startup_wait_s
        while True:
            await asyncio.sleep(wait.retry_every_s)
            try:
                tables = await self._read()
                return tables, self._build(tables)
            except UNREADABLE as exc:
                if loop.time() >= deadline:
                    logger.error("vocabulary_unavailable", error_type=type(exc).__name__)
                    raise
                logger.warning("vocabulary_waiting_for_database", error_type=type(exc).__name__)

    async def check(self, app: FastAPI) -> bool:
        """Read the tables; when they changed, build and swap in the new
        vocabulary. True when it was swapped."""
        try:
            tables = await self._read()
        except UNREADABLE as exc:
            logger.warning("vocabulary_check_skipped", error_type=type(exc).__name__)
            return False
        if tables.version == self._version:
            return False
        try:
            build = self._build(tables)
        except (*UNREADABLE, TaxonomyConfigurationError) as exc:
            # Kept as it is; checked again next time.
            logger.error(
                "vocabulary_change_not_applied",
                version=tables.version,
                error_type=type(exc).__name__,
                detail=getattr(exc, "context", {}).get("detail"),
            )
            return False
        app.state.resources = dataclasses.replace(app.state.resources, **build.fields())
        logger.info("vocabulary_refreshed", old_version=self._version, version=tables.version)
        self._version = tables.version
        await self._copy.save(tables)
        return True

    async def run(self, app: FastAPI) -> None:
        """Check every `vocabulary.refresh_every_s`, for the life of the
        process."""
        while True:
            await asyncio.sleep(self._settings.vocabulary.refresh_every_s)
            try:
                await self.check(app)
            except Exception:
                # A defect in one check must not end every later one; it is
                # logged in full and the vocabulary in use stays.
                logger.exception("vocabulary_check_failed")
