"""Application lifecycle for long-lived resources.

The engine, pool and Redis client are created once per process here and torn
down on shutdown. Nothing constructs a client per request (CLAUDE.md 24).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings, get_settings
from app.core.exceptions import ResourceNotInitialisedError
from app.core.logging import configure_logging, get_logger
from app.db.catalog_schema import verify_commerce_schema
from app.integrations.embeddings import OpenAIQueryEmbedder
from app.integrations.llm import OpenAIStructuredClient
from app.integrations.pinecone import PineconeSemanticIndex, SemanticIndex
from app.integrations.postgres import Database
from app.integrations.redis import RedisClient
from app.taxonomy.attributes import CatalogAttributes, load_catalog_attributes
from app.taxonomy.dimensions import DimensionSemantics, load_dimension_semantics
from app.taxonomy.registry import CommerceTaxonomy, load_taxonomy

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AppResources:
    settings: Settings
    database: Database
    redis: RedisClient
    llm: OpenAIStructuredClient
    taxonomy: CommerceTaxonomy
    attributes: CatalogAttributes
    dimensions: DimensionSemantics
    # Both None when semantic ranking is not configured. Discovery still
    # works; results come back in deterministic order. Defaulted so a
    # deployment without them constructs exactly as it did before.
    embedder: OpenAIQueryEmbedder | None = None
    semantic_index: SemanticIndex | None = None
    # None when the Customer Agent's decision step is not configured. A second
    # client rather than a second parameter: the model identifier is fixed at
    # construction, and one client per process is what CLAUDE.md 24 requires -
    # building one per request would be the thing it forbids.
    decision_llm: OpenAIStructuredClient | None = None
    # None when the response capability is not configured. A third client for
    # the same reason as the second: the model identifier is fixed at
    # construction, and one client per process is what CLAUDE.md 24 requires.
    response_llm: OpenAIStructuredClient | None = None


def get_resources(app: FastAPI) -> AppResources:
    resources = getattr(app.state, "resources", None)
    if not isinstance(resources, AppResources):
        raise ResourceNotInitialisedError(resource="app.state.resources")
    return resources


async def _check_catalog_schema(database: Database) -> None:
    """Verify the live catalog has the commerce columns this service requires.

    A reachable catalog missing a required column is a deployment error and
    raises, failing startup. An *unreachable* catalog is not treated as a schema
    failure: that is transient and the health endpoint already reports it.
    """
    try:
        async with database.engine.connect() as connection:
            await verify_commerce_schema(connection)
    except SQLAlchemyError as exc:
        logger.warning("catalog_schema_check_skipped", error_type=type(exc).__name__)
        return
    logger.info("catalog_schema_verified")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.observability)
    logger.info("service_starting", **settings.redacted())

    # Loaded before any connection is opened: a malformed vocabulary is a
    # configuration error and must fail the process immediately.
    taxonomy = load_taxonomy()
    attributes = load_catalog_attributes()
    # Validated against the commerce taxonomy: a mapping for an unapproved
    # subcategory is a configuration error, not something to discover later.
    dimensions = load_dimension_semantics(taxonomy=taxonomy)
    logger.info(
        "taxonomy_loaded",
        taxonomy_version=taxonomy.version,
        category_count=len(taxonomy.categories),
        attributes_version=attributes.version,
        color_count=len(attributes.colors),
        style_count=len(attributes.styles),
        dimension_semantics_version=dimensions.version,
        dimension_subcategories=len(dimensions.subcategories),
    )

    database = Database.create(settings.db)
    redis_client = RedisClient.create(settings.redis)
    llm_client = OpenAIStructuredClient(settings.llm)
    embedder: OpenAIQueryEmbedder | None = None
    semantic_index: SemanticIndex | None = None
    decision_llm: OpenAIStructuredClient | None = None
    response_llm: OpenAIStructuredClient | None = None
    if settings.pinecone is not None and settings.llm.embedding_model:
        embedder = OpenAIQueryEmbedder(settings.llm)
        semantic_index = PineconeSemanticIndex(settings.pinecone)
    decision_model = settings.customer_agent.decision_model
    if decision_model:
        decision_llm = OpenAIStructuredClient(
            settings.llm.model_copy(update={"model": decision_model})
        )
    response_model = settings.customer_agent.response_model
    if response_model:
        response_llm = OpenAIStructuredClient(
            settings.llm.model_copy(update={"model": response_model})
        )
    logger.info(
        "customer_agent_configured",
        decision_enabled=decision_llm is not None,
        response_enabled=response_llm is not None,
    )
    logger.info(
        "semantic_ranking_configured",
        enabled=semantic_index is not None,
        embedding_model=settings.llm.embedding_model,
        index=settings.pinecone.index_name if settings.pinecone else None,
    )
    app.state.resources = AppResources(
        settings=settings,
        database=database,
        redis=redis_client,
        llm=llm_client,
        embedder=embedder,
        semantic_index=semantic_index,
        decision_llm=decision_llm,
        response_llm=response_llm,
        taxonomy=taxonomy,
        attributes=attributes,
        dimensions=dimensions,
    )

    await _check_catalog_schema(database)
    logger.info("service_started")
    try:
        yield
    finally:
        if embedder is not None:
            await embedder.close()
        if decision_llm is not None:
            await decision_llm.close()
        if response_llm is not None:
            await response_llm.close()
        await llm_client.close()
        await redis_client.close()
        await database.dispose()
        logger.info("service_stopped")
