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

from app.core.config import LLMSettings, Settings, get_settings
from app.core.exceptions import ResourceNotInitialisedError
from app.core.logging import configure_logging, get_logger
from app.db.catalog_schema import verify_commerce_schema, verify_support_schema
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
from app.integrations.store_knowledge import PineconeKnowledgeIndex
from app.integrations.zory_finder import ZoryFinderClient
from app.orchestration.graph import NODE_ORDER, ChatGraphRunner
from app.services.store_knowledge import StoreKnowledgeService
from app.taxonomy.attributes import CatalogAttributes, load_catalog_attributes
from app.taxonomy.briefs import Briefs, load_briefs
from app.taxonomy.compare_groups import CompareGroups, load_compare_groups
from app.taxonomy.complements import Complements, load_complements
from app.taxonomy.dimensions import DimensionSemantics, load_dimension_semantics
from app.taxonomy.registry import CommerceTaxonomy, load_taxonomy
from app.taxonomy.rooms import RoomPieces, load_room_pieces
from app.taxonomy.seating import SeatingSemantics, load_seating_semantics
from app.taxonomy.subcategory_questions import (
    SubcategoryQuestions,
    load_subcategory_questions,
)

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
    # Both None when semantic ranking is not configured. Discovery still
    # works; results come back in deterministic order. Defaulted so a
    # deployment without them constructs exactly as it did before.
    embedder: OpenAIQueryEmbedder | None = None
    semantic_index: SemanticIndex | None = None
    # None when store knowledge is not configured: the agent then never looks
    # in the store's documents and answers exactly as before.
    store_knowledge: StoreKnowledgeService | None = None
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
    # The cards of questions for a product search. Defaulted, like the
    # pairings: without them every search simply runs.
    briefs: Briefs | None = None
    # Each sub-category's own questions, asked instead of its family's card
    # when `customer_agent.subcategory_questions` is on. Defaulted like the
    # cards: without them every type is asked as before.
    subcategory_questions: SubcategoryQuestions | None = None
    # Which types compare with which. Defaulted: without it the comparison
    # pop-up is refused rather than comparing anything with anything.
    compare_groups: CompareGroups | None = None


def get_resources(app: FastAPI) -> AppResources:
    resources = getattr(app.state, "resources", None)
    if not isinstance(resources, AppResources):
        raise ResourceNotInitialisedError(resource="app.state.resources")
    return resources


def design_llm_settings(settings: Settings) -> LLMSettings | None:
    """The provider settings for the design specialist, or None when it is not
    configured: its own model, and its own reasoning effort when one is set."""
    design = settings.interior_design
    if not design.model:
        return None
    return settings.llm.model_copy(
        update={
            "model": design.model,
            "reasoning_effort": design.reasoning_effort or settings.llm.reasoning_effort,
        }
    )


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

    # Loaded before any connection is opened: a malformed vocabulary is a
    # configuration error and must fail the process immediately.
    taxonomy = load_taxonomy()
    attributes = load_catalog_attributes()
    # Validated against the commerce taxonomy: a mapping for an unapproved
    # subcategory is a configuration error, not something to discover later.
    dimensions = load_dimension_semantics(
        taxonomy=taxonomy, by_side=settings.discovery.size_by_side
    )
    seating = load_seating_semantics(taxonomy=taxonomy)
    rooms = load_room_pieces(taxonomy=taxonomy, seating=seating)
    complements = load_complements(taxonomy=taxonomy)
    briefs = load_briefs(taxonomy=taxonomy)
    subcategory_questions = load_subcategory_questions(taxonomy=taxonomy)
    compare_groups = load_compare_groups(taxonomy=taxonomy)
    logger.info(
        "taxonomy_loaded",
        taxonomy_version=taxonomy.version,
        category_count=len(taxonomy.categories),
        attributes_version=attributes.version,
        color_count=len(attributes.colors),
        style_count=len(attributes.styles),
        dimension_semantics_version=dimensions.version,
        dimension_subcategories=len(dimensions.subcategories),
        seating_version=seating.version,
        room_pieces_version=rooms.version,
        complements_version=complements.version,
        briefs_version=briefs.version,
        subcategory_questions_version=subcategory_questions.version,
        compare_groups_version=compare_groups.version,
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
    zory_finder: ZoryFinderClient | None = None
    if settings.furniture_finder is not None:
        zory_finder = ZoryFinderClient(settings.furniture_finder)
    salla = SallaStorefrontClient(settings.salla) if settings.salla.check_cart else None
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
    design_settings = design_llm_settings(settings)
    if design_settings is not None:
        design_llm = OpenAIStructuredClient(design_settings)
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
        decision_llm=decision_llm,
        response_llm=response_llm,
        design_llm=design_llm,
        taxonomy=taxonomy,
        attributes=attributes,
        dimensions=dimensions,
        seating=seating,
        chat_graph=chat_graph,
        rooms=rooms,
        complements=complements,
        briefs=briefs,
        subcategory_questions=subcategory_questions,
        compare_groups=compare_groups,
        zory_finder=zory_finder,
        salla=salla,
        render_generator=render_generator,
        render_photos=render_photos,
        room_checker=room_checker,
        empty_room_generator=empty_room_generator,
    )

    await _check_catalog_schema(database, support=settings.customer_agent.support_agent)
    logger.info("service_started")
    try:
        yield
    finally:
        if embedder is not None:
            await embedder.close()
        if knowledge_embedder is not None:
            await knowledge_embedder.close()
        if zory_finder is not None:
            await zory_finder.close()
        if salla is not None:
            await salla.close()
        if room_checker is not None:
            await room_checker.close()
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
