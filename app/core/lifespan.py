"""Application lifecycle for long-lived resources.

The engine, pool and Redis client are created once per process here and torn
down on shutdown. Nothing constructs a client per request (CLAUDE.md 24).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from app.agents.team import AgentTeam
from app.core.config import Settings, get_settings
from app.core.exceptions import ResourceNotInitialisedError
from app.core.logging import configure_logging, get_logger
from app.core.vocabulary import VocabularyRefresher, team_models
from app.db.catalog_schema import (
    verify_commerce_schema,
    verify_product_data_schema,
    verify_support_schema,
)
from app.integrations.embeddings import OpenAIQueryEmbedder
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
from app.integrations.salla_storefront import SallaStorefrontClient
from app.integrations.speech_to_text import ElevenLabsTranscriber
from app.integrations.store_knowledge import PineconeKnowledgeIndex
from app.integrations.zory_finder import ZoryFinderClient
from app.orchestration.graph import NODE_ORDER, ChatGraphRunner
from app.repositories.vocabulary_copy import VocabularyCopy
from app.services.history_summary import HistorySummarizer
from app.services.service_health import OutsideHealthCache
from app.services.store_knowledge import StoreKnowledgeService
from app.taxonomy.attributes import CatalogAttributes
from app.taxonomy.compare_groups import CompareGroups
from app.taxonomy.complements import Complements
from app.taxonomy.dimensions import DimensionSemantics
from app.taxonomy.registry import CommerceTaxonomy
from app.taxonomy.rooms import RoomPieces
from app.taxonomy.seating import SeatingSemantics

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
    seating: SeatingSemantics
    chat_graph: ChatGraphRunner
    rooms: RoomPieces
    # The supervisor and the four agents, compiled once. None until every one
    # of them has a model: a team missing an agent cannot take every turn.
    agent_team: AgentTeam | None = None
    # Both None when semantic ranking is not configured. Discovery still
    # works; results come back in deterministic order. Defaulted so a
    # deployment without them constructs exactly as it did before.
    embedder: OpenAIQueryEmbedder | None = None
    semantic_index: SemanticIndex | None = None
    # None when store knowledge is not configured: the agent then never looks
    # in the store's documents and answers exactly as before.
    store_knowledge: StoreKnowledgeService | None = None
    # None when Furniture Finder is not configured: the main platform's own
    # finder answers it, so one client is the whole capability.
    zory_finder: ZoryFinderClient | None = None
    # Salla's public storefront API, asked before a Salla store's cart takes
    # a product; None when that check is switched off (CLAUDE.md 36).
    salla: SallaStorefrontClient | None = None
    # Both None when room visualisation is not configured.
    render_generator: ImageGenerator | None = None
    render_photos: ProductImageFetcher | None = None
    # The customer's own room photo: a vision model that says whether it is a
    # room, and the image models that empty it. Both None when room photos
    # are not configured.
    room_checker: OpenAIStructuredClient | None = None
    empty_room_generator: ImageGenerator | None = None
    # The reviewed pairings - what goes with what. Defaulted so a deployment
    # built without them constructs as before: products open without
    # cross-sell, and nothing else changes.
    complements: Complements | None = None
    # Which types compare with which. Defaulted: without it the comparison
    # pop-up is refused rather than comparing anything with anything.
    compare_groups: CompareGroups | None = None
    # Folds a conversation's oldest messages into its rolling summary. None
    # when summaries are off: the oldest messages are then dropped.
    history_summarizer: HistorySummarizer | None = None
    # The public health check's last outside-probe results, kept per process
    # so a polled URL never becomes a stream of provider calls.
    outside_health: OutsideHealthCache = field(default_factory=OutsideHealthCache)
    # None when voice input is not configured: the microphone route refuses.
    speech_to_text: ElevenLabsTranscriber | None = None


def get_resources(app: FastAPI) -> AppResources:
    resources = getattr(app.state, "resources", None)
    if not isinstance(resources, AppResources):
        raise ResourceNotInitialisedError(resource="app.state.resources")
    return resources


async def _check_catalog_schema(database: Database, *, support: bool = False) -> None:
    """Verify the live catalog has the commerce columns this service requires,
    and the store's support columns where the support agent reads them.

    A reachable catalog missing a required column is a deployment error and
    raises, failing startup. An *unreachable* catalog is not treated as a schema
    failure: that is transient and the health endpoint already reports it.
    """
    try:
        async with database.engine.connect() as connection:
            await verify_commerce_schema(connection)
            await verify_product_data_schema(connection)
            if support:
                await verify_support_schema(connection)
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

    # The vocabulary comes from the database - the one source - with every
    # reviewed file checked against it, and is checked again every few minutes
    # while the service runs (CLAUDE.md 14.1). It never stops the service: an
    # unreadable database is answered from the last good copy.
    database = Database.create(settings.db)
    redis_client = RedisClient.create(settings.redis)
    vocabulary = VocabularyRefresher(
        database,
        VocabularyCopy(redis_client.client, settings.session.operation_timeout_s),
        settings,
        team_models(settings),
    )
    words = await vocabulary.at_startup()
    # Compiled once for the process. The per-request runtime travels with each
    # invocation, so one graph serves every customer (CLAUDE.md 24).
    chat_graph = ChatGraphRunner()
    llm_client = OpenAIStructuredClient(settings.llm)
    summary_client: OpenAIStructuredClient | None = None
    history_summarizer: HistorySummarizer | None = None
    if settings.session.summary.enabled:
        # Same provider and key; its own model, effort and timeout - it runs
        # beside a turn and must never be what the customer waits for.
        summary = settings.session.summary
        summary_client = OpenAIStructuredClient(
            settings.llm.model_copy(
                update={
                    "model": summary.model or settings.llm.model,
                    "reasoning_effort": (
                        None if summary.reasoning_effort == "none" else summary.reasoning_effort
                    ),
                    "temperature": None,
                    "timeout_s": summary.timeout_s,
                }
            )
        )
        history_summarizer = HistorySummarizer(summary_client, settings.session)
    embedder: OpenAIQueryEmbedder | None = None
    semantic_index: SemanticIndex | None = None
    if settings.pinecone is not None and settings.llm.embedding_model:
        embedder = OpenAIQueryEmbedder(settings.llm)
        semantic_index = PineconeSemanticIndex(settings.pinecone)
    store_knowledge: StoreKnowledgeService | None = None
    knowledge_embedder: OpenAIQueryEmbedder | None = None
    if settings.store_knowledge is not None and settings.llm.embedding_model:
        # The same embedding model the platform indexed the passages with;
        # its own client only when semantic ranking has none to share.
        shared = embedder
        if shared is None:
            shared = knowledge_embedder = OpenAIQueryEmbedder(settings.llm)
        store_knowledge = StoreKnowledgeService(
            shared,
            PineconeKnowledgeIndex(settings.store_knowledge),
            settings.store_knowledge,
        )
    zory_finder: ZoryFinderClient | None = None
    if settings.furniture_finder is not None:
        zory_finder = ZoryFinderClient(settings.furniture_finder)
    salla = SallaStorefrontClient(settings.salla) if settings.salla.check_cart else None
    speech_to_text = (
        ElevenLabsTranscriber(settings.speech_to_text) if settings.speech_to_text else None
    )
    render_generator: ImageGenerator | None = None
    render_photos: ProductImageFetcher | None = None
    room_checker: OpenAIStructuredClient | None = None
    empty_room_generator: ImageGenerator | None = None
    closables: list[OpenAIImageGenerator | GeminiImageGenerator | ProductImageFetcher] = []
    visualization = settings.visualization
    if visualization is not None:
        generators: dict[str, OpenAIImageGenerator | GeminiImageGenerator] = {}
        if visualization.openai_model is not None:
            generators["openai"] = OpenAIImageGenerator(
                visualization,
                api_key=settings.llm.api_key.get_secret_value(),
                base_url=settings.llm.base_url,
            )
        if visualization.gemini_model is not None:
            generators["gemini"] = GeminiImageGenerator(visualization)
        primary = generators[visualization.primary]
        fallback = next(
            (g for name, g in generators.items() if name != visualization.primary), None
        )
        render_generator = FallbackImageGenerator(primary, fallback)
        if visualization.room_check_model is not None:
            # Same provider and key as every other model client; its own
            # model, effort and timeout - the customer is waiting on it.
            room_checker = OpenAIStructuredClient(
                settings.llm.model_copy(
                    update={
                        "model": visualization.room_check_model,
                        "reasoning_effort": visualization.room_check_reasoning_effort,
                        "temperature": None,
                        "timeout_s": visualization.room_check_timeout_s,
                    }
                )
            )
            # The same two image models, in the order that keeps a photo
            # closest to itself; whichever is configured when only one is.
            emptier = generators.get(visualization.empty_room_primary, primary)
            empty_room_generator = FallbackImageGenerator(
                emptier, next((g for g in generators.values() if g is not emptier), None)
            )
        render_photos = ProductImageFetcher(
            timeout_s=visualization.reference_timeout_s,
            max_bytes=visualization.reference_max_bytes,
        )
        closables.extend([*generators.values(), render_photos])
    logger.info("chat_graph_compiled", nodes=len(NODE_ORDER), agents=words.agent_team is not None)
    logger.info(
        "semantic_ranking_configured",
        enabled=semantic_index is not None,
        embedding_model=settings.llm.embedding_model,
        index=settings.pinecone.index_name if settings.pinecone else None,
    )
    logger.info(
        "store_knowledge_configured",
        enabled=store_knowledge is not None,
        index=settings.store_knowledge.index_name if settings.store_knowledge else None,
    )
    logger.info(
        "furniture_finder_configured",
        enabled=zory_finder is not None,
        upstream=settings.furniture_finder.zory_base_url if settings.furniture_finder else None,
    )
    logger.info(
        "visualization_configured",
        enabled=render_generator is not None,
        room_photos_enabled=room_checker is not None,
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
        store_knowledge=store_knowledge,
        chat_graph=chat_graph,
        **words.fields(),
        zory_finder=zory_finder,
        salla=salla,
        render_generator=render_generator,
        render_photos=render_photos,
        room_checker=room_checker,
        empty_room_generator=empty_room_generator,
        history_summarizer=history_summarizer,
        speech_to_text=speech_to_text,
    )

    await _check_catalog_schema(database, support=settings.customer_agent.support_agent)
    refresh = asyncio.create_task(vocabulary.run(app), name="vocabulary_refresh")
    logger.info("service_started", vocabulary_version=vocabulary.version)
    try:
        yield
    finally:
        refresh.cancel()
        with suppress(asyncio.CancelledError):
            await refresh
        if embedder is not None:
            await embedder.close()
        if knowledge_embedder is not None:
            await knowledge_embedder.close()
        if zory_finder is not None:
            await zory_finder.close()
        if salla is not None:
            await salla.close()
        if speech_to_text is not None:
            await speech_to_text.close()
        if room_checker is not None:
            await room_checker.close()
        if summary_client is not None:
            await summary_client.close()
        for closable in closables:
            await closable.close()
        await llm_client.close()
        await redis_client.close()
        await database.dispose()
        logger.info("service_stopped")
