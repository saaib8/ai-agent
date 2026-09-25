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
from app.integrations.detection import ModalObjectDetector
from app.integrations.embeddings import OpenAIQueryEmbedder
from app.integrations.finder_index import FinderIndex, PineconeFinderIndex
from app.integrations.image_generation import (
    FallbackImageGenerator,
    GeminiImageGenerator,
    ImageGenerator,
    OpenAIImageGenerator,
)
from app.integrations.llm import OpenAIStructuredClient
from app.integrations.pinecone import PineconeSemanticIndex, SemanticIndex
from app.integrations.postgres import Database
from app.integrations.product_images import ProductImageFetcher
from app.integrations.redis import RedisClient
from app.integrations.render_store import RenderStore, S3RenderStore
from app.orchestration.graph import NODE_ORDER, ChatGraphRunner
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
    chat_graph: ChatGraphRunner
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
    # None when the interior-design specialist is not configured. A fourth
    # client for the same reason as the second and third: the model identifier
    # is fixed at construction (CLAUDE.md 24).
    design_llm: OpenAIStructuredClient | None = None
    # All four None when Furniture Finder is not configured, and all four
    # present when it is: the finder is one capability with four providers,
    # and part of one is not a deployment anybody means.
    detector: ModalObjectDetector | None = None
    finder_vision: OpenAIStructuredClient | None = None
    finder_embedder: OpenAIQueryEmbedder | None = None
    finder_index: FinderIndex | None = None
    # All three None when room visualisation is not configured.
    render_generator: ImageGenerator | None = None
    render_store: RenderStore | None = None
    render_photos: ProductImageFetcher | None = None


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
    except (SQLAlchemyError, OSError) as exc:
        # `OSError` as well as SQLAlchemy's own: asyncpg raises a bare
        # ConnectionRefusedError when nothing is listening, and SQLAlchemy does
        # not wrap it at connect time. Without it here, a database that is
        # briefly away during a rolling deploy crash-loops the service instead
        # of starting and reporting itself unhealthy - which is the opposite of
        # what this function is for.
        #
        # A *reachable* catalog missing a required column still raises: that is
        # `CatalogSchemaError`, it is a deployment mistake rather than a blip,
        # and it must stop the process.
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
    # Compiled once for the process. The per-request runtime travels with each
    # invocation, so one graph serves every customer (CLAUDE.md 24).
    chat_graph = ChatGraphRunner()
    llm_client = OpenAIStructuredClient(settings.llm)
    embedder: OpenAIQueryEmbedder | None = None
    semantic_index: SemanticIndex | None = None
    decision_llm: OpenAIStructuredClient | None = None
    response_llm: OpenAIStructuredClient | None = None
    design_llm: OpenAIStructuredClient | None = None
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
    detector: ModalObjectDetector | None = None
    finder_vision: OpenAIStructuredClient | None = None
    finder_embedder: OpenAIQueryEmbedder | None = None
    finder_index: FinderIndex | None = None
    finder = settings.furniture_finder
    if finder is not None:
        detector = ModalObjectDetector(finder)
        # Same provider and key as every other model client; its own model,
        # effort and timeout, because the customer is waiting on this one.
        finder_vision = OpenAIStructuredClient(
            settings.llm.model_copy(
                update={
                    "model": finder.vision_model,
                    "reasoning_effort": finder.vision_reasoning_effort,
                    "temperature": None,
                    "timeout_s": finder.vision_timeout_s,
                }
            )
        )
        finder_embedder = OpenAIQueryEmbedder(
            settings.llm, model=finder.embedding_model, dimensions=finder.embedding_dimensions
        )
        finder_index = PineconeFinderIndex(finder)
    render_generator: ImageGenerator | None = None
    render_store: RenderStore | None = None
    render_photos: ProductImageFetcher | None = None
    closables: list[OpenAIImageGenerator | GeminiImageGenerator | ProductImageFetcher] = []
    visualization = settings.visualization
    if visualization is not None:
        generators: dict[str, OpenAIImageGenerator | GeminiImageGenerator] = {}
        if visualization.openai_model is not None:
            generators["openai"] = OpenAIImageGenerator(
                visualization, api_key=settings.llm.api_key.get_secret_value()
            )
        if visualization.gemini_model is not None:
            generators["gemini"] = GeminiImageGenerator(visualization)
        primary = generators[visualization.primary]
        fallback = next(
            (g for name, g in generators.items() if name != visualization.primary), None
        )
        render_generator = FallbackImageGenerator(primary, fallback)
        render_store = S3RenderStore(visualization)
        render_photos = ProductImageFetcher(
            timeout_s=visualization.reference_timeout_s,
            max_bytes=visualization.reference_max_bytes,
        )
        closables.extend([*generators.values(), render_photos])
    design_model = settings.interior_design.model
    if design_model:
        design_llm = OpenAIStructuredClient(settings.llm.model_copy(update={"model": design_model}))
    logger.info("chat_graph_compiled", nodes=len(NODE_ORDER))
    logger.info(
        "agents_configured",
        decision_enabled=decision_llm is not None,
        response_enabled=response_llm is not None,
        interior_design_enabled=design_llm is not None,
    )
    logger.info(
        "semantic_ranking_configured",
        enabled=semantic_index is not None,
        embedding_model=settings.llm.embedding_model,
        index=settings.pinecone.index_name if settings.pinecone else None,
    )
    logger.info(
        "furniture_finder_configured",
        enabled=detector is not None,
        index=settings.furniture_finder.index_name if settings.furniture_finder else None,
    )
    logger.info(
        "visualization_configured",
        enabled=render_generator is not None,
        primary=visualization.primary if visualization else None,
        fallback_configured=bool(
            visualization and visualization.openai_model and visualization.gemini_model
        ),
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
        design_llm=design_llm,
        taxonomy=taxonomy,
        attributes=attributes,
        dimensions=dimensions,
        chat_graph=chat_graph,
        detector=detector,
        finder_vision=finder_vision,
        finder_embedder=finder_embedder,
        finder_index=finder_index,
        render_generator=render_generator,
        render_store=render_store,
        render_photos=render_photos,
    )

    await _check_catalog_schema(database)
    logger.info("service_started")
    try:
        yield
    finally:
        if embedder is not None:
            await embedder.close()
        if detector is not None:
            await detector.close()
        if finder_vision is not None:
            await finder_vision.close()
        if finder_embedder is not None:
            await finder_embedder.close()
        for closable in closables:
            await closable.close()
        if decision_llm is not None:
            await decision_llm.close()
        if response_llm is not None:
            await response_llm.close()
        if design_llm is not None:
            await design_llm.close()
        await llm_client.close()
        await redis_client.close()
        await database.dispose()
        logger.info("service_stopped")
