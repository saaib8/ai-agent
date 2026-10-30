"""FastAPI dependency wiring.

Resources come from application state (created in lifespan); repositories and
services are constructed per request around a single session, so they are
trivially replaceable with fakes in tests (CLAUDE.md 24).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Annotated

from fastapi import Depends, Header, Request
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.operations.cart import CartOperations
from app.agents.operations.rooms import RoomEngine
from app.agents.operations.search import SearchRunner
from app.agents.services import AgentServices
from app.agents.writer import ReplyWriter
from app.core.config import Environment
from app.core.exceptions import ConfigurationError
from app.core.lifespan import AppResources, get_resources
from app.integrations.llm import StructuredLLMClient
from app.integrations.postgres import Database
from app.integrations.redis import RedisClient
from app.orchestration.graph import ChatGraphRunner
from app.repositories.catalog_cache import OverviewCache
from app.repositories.conversation_moves import ConversationMoves
from app.repositories.finder_photos import FinderPhotoStore
from app.repositories.products import ProductRepository
from app.repositories.room_photos import RoomPhotoStore
from app.repositories.sessions import SessionStore
from app.repositories.stores import StoreRepository
from app.repositories.support_contacts import SupportContactsRepository
from app.schemas.furniture_finder import (
    WIDGET_KEY_PATTERN,
    WIDGET_ORIGIN_PATTERN,
    WIDGET_SESSION_PATTERN,
    WIDGET_STORE_PATTERN,
    ShopperCredentials,
)
from app.services.bundle_optimizer import BundleOptimizer
from app.services.card_comparison import CardComparisonService
from app.services.cart import CartService
from app.services.catalog import CatalogService
from app.services.catalog_capability import CatalogCapabilityService
from app.services.chat_runtime import ChatRuntime
from app.services.click_tracking import ClickTrackingService
from app.services.comparison import ProductComparisonService
from app.services.controlled_search import ControlledRelaxationService
from app.services.conversation_adoption import ConversationAdoptionService
from app.services.cross_sell import CompanionSearchBuilder
from app.services.design_discovery import DesignDiscoveryService
from app.services.discovery import ProductDiscoveryService
from app.services.furniture_finder import FinderTurnRuntime, FurnitureFinderService
from app.services.health import HealthService
from app.services.hydration import ProductHydrationService
from app.services.picks import PicksRuntime
from app.services.product_display import ProductDisplayWords
from app.services.query_understanding import QueryUnderstandingService
from app.services.reference_resolver import ProductReferenceResolver
from app.services.refinement_composer import SearchRefinementComposer
from app.services.relaxation import RelaxationPlanner
from app.services.retailer_context import RetailerContextProvider
from app.services.room_photo import RoomPhotoService
from app.services.room_visualization import (
    CatalogVisualizationRuntime,
    RoomVisualizer,
    VisualizationTurnRuntime,
)
from app.services.search_pipeline import ProductSearchPipeline
from app.services.semantic_ranking import SemanticRankingService
from app.services.similar_search import SimilarSearchBuilder
from app.services.stocked_looks import StockedLooks
from app.services.store_cart_check import StoreCartCheck
from app.services.support import SupportService
from app.services.support_handover import SupportHandoverService
from app.taxonomy.attributes import CatalogAttributes
from app.taxonomy.dimensions import DimensionSemantics
from app.taxonomy.registry import CommerceTaxonomy


def resources(request: Request) -> AppResources:
    return get_resources(request.app)


ResourcesDep = Annotated[AppResources, Depends(resources)]


def database(app_resources: ResourcesDep) -> Database:
    return app_resources.database


def redis_client(app_resources: ResourcesDep) -> RedisClient:
    return app_resources.redis


def taxonomy(app_resources: ResourcesDep) -> CommerceTaxonomy:
    return app_resources.taxonomy


def catalog_attributes(app_resources: ResourcesDep) -> CatalogAttributes:
    return app_resources.attributes


def dimension_semantics(app_resources: ResourcesDep) -> DimensionSemantics:
    return app_resources.dimensions


def llm_client(app_resources: ResourcesDep) -> StructuredLLMClient:
    return app_resources.llm


DatabaseDep = Annotated[Database, Depends(database)]
RedisDep = Annotated[RedisClient, Depends(redis_client)]
TaxonomyDep = Annotated[CommerceTaxonomy, Depends(taxonomy)]
CatalogAttributesDep = Annotated[CatalogAttributes, Depends(catalog_attributes)]
DimensionSemanticsDep = Annotated[DimensionSemantics, Depends(dimension_semantics)]
LLMClientDep = Annotated[StructuredLLMClient, Depends(llm_client)]


async def db_session(db: DatabaseDep) -> AsyncIterator[AsyncSession]:
    async with db.session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(db_session)]


def product_repository(session: SessionDep) -> ProductRepository:
    return ProductRepository(session)


def store_repository(session: SessionDep) -> StoreRepository:
    return StoreRepository(session)


StoreRepositoryDep = Annotated[StoreRepository, Depends(store_repository)]


def retailer_context_provider(stores: StoreRepositoryDep) -> RetailerContextProvider:
    return RetailerContextProvider(stores)


def health_service(db: DatabaseDep, redis: RedisDep) -> HealthService:
    return HealthService(db, redis)


def product_discovery_service(
    session: SessionDep, app_resources: ResourcesDep
) -> ProductDiscoveryService:
    return ProductDiscoveryService(
        ProductRepository(session),
        app_resources.taxonomy,
        app_resources.settings.discovery,
        app_resources.attributes,
        app_resources.dimensions,
    )


ProductRepositoryDep = Annotated[ProductRepository, Depends(product_repository)]
RetailerContextProviderDep = Annotated[RetailerContextProvider, Depends(retailer_context_provider)]
HealthServiceDep = Annotated[HealthService, Depends(health_service)]


def click_tracking_service(session: SessionDep) -> ClickTrackingService:
    return ClickTrackingService(ProductRepository(session))


ClickTrackingServiceDep = Annotated[ClickTrackingService, Depends(click_tracking_service)]


def controlled_relaxation_service(
    session: SessionDep, app_resources: ResourcesDep
) -> ControlledRelaxationService:
    settings = app_resources.settings
    return ControlledRelaxationService(
        ProductDiscoveryService(
            ProductRepository(session),
            app_resources.taxonomy,
            settings.discovery,
            app_resources.attributes,
            app_resources.dimensions,
        ),
        RelaxationPlanner(settings.relaxation),
        settings.relaxation,
    )


def query_understanding_service(
    client: LLMClientDep,
    commerce_taxonomy: TaxonomyDep,
    attributes: CatalogAttributesDep,
    dimensions: DimensionSemanticsDep,
) -> QueryUnderstandingService:
    return QueryUnderstandingService(client, commerce_taxonomy, attributes, dimensions)


ProductDiscoveryServiceDep = Annotated[ProductDiscoveryService, Depends(product_discovery_service)]
QueryUnderstandingServiceDep = Annotated[
    QueryUnderstandingService, Depends(query_understanding_service)
]
ControlledRelaxationServiceDep = Annotated[
    ControlledRelaxationService, Depends(controlled_relaxation_service)
]


def semantic_ranking_service(app_resources: ResourcesDep) -> SemanticRankingService:
    """Ranking, whether or not a semantic index is configured.

    Built with None providers when it is not, so callers need no branch: the
    service returns eligible products in deterministic order instead.
    """
    return SemanticRankingService(
        app_resources.embedder, app_resources.semantic_index, app_resources.settings.size
    )


def product_hydration_service(session: SessionDep) -> ProductHydrationService:
    return ProductHydrationService(ProductRepository(session))


def product_search_pipeline(
    session: SessionDep, app_resources: ResourcesDep
) -> ProductSearchPipeline:
    """The composed search path, or a refusal to build one.

    `presentation_limit` has no default: no number has been approved, and one
    invented here would decide how many options a customer sees. So an absent
    limit means this capability is not configured, and asking for it fails
    loudly at construction rather than quietly at display time.

    Requested only where it is used. Nothing builds this at startup, so a
    deployment that has not configured it still starts and serves everything
    else.
    """
    settings = app_resources.settings
    limit = settings.customer_agent.presentation_limit
    if limit is None:
        raise ConfigurationError(
            detail=("customer_agent.presentation_limit must be set to use product search"),
            public_message="Product search is not configured.",
        )
    return ProductSearchPipeline(
        controlled_relaxation_service(session, app_resources),
        semantic_ranking_service(app_resources),
        product_hydration_service(session),
        presentation_limit=limit,
        pinecone=settings.pinecone,
    )


def catalog_capability_service(
    session: SessionDep, app_resources: ResourcesDep
) -> CatalogCapabilityService:
    """What the active retailer stocks, and how its sizes read (`size`)."""
    settings = app_resources.settings
    ttl = settings.catalog.overview_ttl_s
    cache = (
        OverviewCache(app_resources.redis.client, ttl, settings.session.operation_timeout_s)
        if ttl
        else None
    )
    return CatalogCapabilityService(
        ProductRepository(session),
        app_resources.taxonomy,
        app_resources.seating,
        settings.size,
        cache,
    )


CatalogCapabilityServiceDep = Annotated[
    CatalogCapabilityService, Depends(catalog_capability_service)
]


def design_discovery_service(
    session: SessionDep, app_resources: ResourcesDep
) -> DesignDiscoveryService:
    """A room plan's needs turned into verified products.

    Deterministic, and configured only by what the search pipeline already
    needs. It reads no limit of its own: the internal candidate pool is
    unbounded, because M8 and M9 already produce the complete eligible pool
    and truncating it would decide a room's options before the optimiser saw
    them.
    """
    return DesignDiscoveryService(
        product_search_pipeline(session, app_resources),
        app_resources.taxonomy,
        app_resources.seating,
        app_resources.attributes,
        fresh=partial(_fresh_pipeline, app_resources),
        concurrency=app_resources.settings.agents.room_search_concurrency,
        embedder=app_resources.embedder if app_resources.semantic_index is not None else None,
    )


@asynccontextmanager
async def _fresh_pipeline(app_resources: AppResources) -> AsyncIterator[ProductSearchPipeline]:
    """The search pipeline on a database session of its own, for one of a
    room's pieces searched beside the others; the session closes on exit."""
    async with app_resources.database.session() as session:
        yield product_search_pipeline(session, app_resources)


DesignDiscoveryServiceDep = Annotated[DesignDiscoveryService, Depends(design_discovery_service)]


SemanticRankingServiceDep = Annotated[SemanticRankingService, Depends(semantic_ranking_service)]
ProductSearchPipelineDep = Annotated[ProductSearchPipeline, Depends(product_search_pipeline)]
ProductHydrationServiceDep = Annotated[ProductHydrationService, Depends(product_hydration_service)]


def session_store(app_resources: ResourcesDep) -> SessionStore:
    """Session persistence over the process-wide Redis client.

    The client is created once in lifespan; this wraps it with the validated
    session policy. Nothing here opens a connection (CLAUDE.md 24).
    """
    return SessionStore(app_resources.redis.client, app_resources.settings.session)


SessionStoreDep = Annotated[SessionStore, Depends(session_store)]


def support_handover_service(
    sessions: SessionStoreDep, app_resources: ResourcesDep
) -> SupportHandoverService | None:
    """What the store's support is handed; None where the platform does not send it."""
    if not app_resources.settings.customer_agent.support_handover:
        return None
    return SupportHandoverService(sessions)


SupportHandoverServiceDep = Annotated[
    SupportHandoverService | None, Depends(support_handover_service)
]


def conversation_adoption_service(
    sessions: SessionStoreDep, app_resources: ResourcesDep
) -> ConversationAdoptionService | None:
    """A signed-in guest's chat moved to their account; None where it is off."""
    settings = app_resources.settings.session
    if not settings.guest_handover:
        return None
    return ConversationAdoptionService(
        sessions, ConversationMoves(app_resources.redis.client, settings)
    )


ConversationAdoptionServiceDep = Annotated[
    ConversationAdoptionService | None, Depends(conversation_adoption_service)
]


def store_cart_check(session: SessionDep, app_resources: ResourcesDep) -> StoreCartCheck | None:
    """The store's own cart, asked before ours takes more - None where the
    check is switched off (CLAUDE.md 36)."""
    if app_resources.salla is None:
        return None
    return StoreCartCheck(
        StoreRepository(session),
        ProductRepository(session),
        app_resources.salla,
        refuse_unconfirmed=app_resources.settings.salla.refuse_unconfirmed,
    )


def product_display_words(app_resources: AppResources) -> ProductDisplayWords:
    """Products' text ready to show - name, kind, colour, styles, seats, price,
    size - in the reply's language, from the reviewed registries. Arabic only
    while Arabic replies are on: off, no reply is ever in Arabic."""
    return ProductDisplayWords(app_resources.taxonomy, app_resources.attributes)


def picks_runtime(session: SessionDep, app_resources: ResourcesDep) -> PicksRuntime:
    """Ticks and unticks: the reference resolver, fresh reads, the session."""
    repository = ProductRepository(session)
    return PicksRuntime(
        ProductReferenceResolver(repository, app_resources.attributes),
        product_hydration_service(session),
        session_store(app_resources),
        app_resources.settings.customer_agent,
        display=product_display_words(app_resources),
        cart=CartService(repository),
        store_check=store_cart_check(session, app_resources),
    )


PicksRuntimeDep = Annotated[PicksRuntime, Depends(picks_runtime)]


def card_comparison_service(
    session: SessionDep, app_resources: ResourcesDep
) -> CardComparisonService:
    """Two checked cards in a pop-up: resolved like ticks, compared by the
    ordinary service, worded by the ordinary reply generator."""
    if app_resources.compare_groups is None:
        raise ConfigurationError(
            detail="compare groups are not loaded",
            public_message="Comparing products is not available.",
        )
    repository = ProductRepository(session)
    return CardComparisonService(
        ProductReferenceResolver(repository, app_resources.attributes),
        ProductComparisonService(
            repository, app_resources.dimensions, app_resources.settings.customer_agent
        ),
        session_store(app_resources),
        app_resources.compare_groups,
    )


CardComparisonServiceDep = Annotated[CardComparisonService, Depends(card_comparison_service)]


def agent_services(session: SessionDep, app_resources: ResourcesDep) -> AgentServices:
    """What the agents' tools run on, for one request's database session."""
    settings = app_resources.settings
    pipeline = product_search_pipeline(session, app_resources)
    repository = ProductRepository(session)
    hydration = product_hydration_service(session)
    capability = catalog_capability_service(session, app_resources)
    agent = settings.customer_agent
    return AgentServices(
        taxonomy=app_resources.taxonomy,
        attributes=app_resources.attributes,
        dimensions=app_resources.dimensions,
        query_understanding=query_understanding_service(
            app_resources.llm,
            app_resources.taxonomy,
            app_resources.attributes,
            app_resources.dimensions,
        ),
        search=SearchRunner(
            pipeline,
            SearchRefinementComposer(
                app_resources.attributes, app_resources.dimensions, app_resources.seating
            ),
            app_resources.seating if agent.mixed_types else None,
        ),
        hydration=hydration,
        references=ProductReferenceResolver(repository, app_resources.attributes),
        comparison=ProductComparisonService(repository, app_resources.dimensions, agent),
        similar_search=SimilarSearchBuilder(app_resources.taxonomy, app_resources.attributes),
        capabilities=capability,
        cart_operations=CartOperations(
            max_picks=agent.max_picks,
            max_quantity=agent.cart_max_quantity,
            max_likes=agent.max_likes,
            check=store_cart_check(session, app_resources),
            refuse_unconfirmed=settings.salla.refuse_unconfirmed,
        ),
        rooms=RoomEngine(
            design_discovery_service(session, app_resources),
            BundleOptimizer(settings.agents.share_upgrade_similarity_margin),
            pipeline,
            capability,
            hydration,
            app_resources.rooms,
            app_resources.seating,
            app_resources.dimensions,
        ),
        compare_groups=app_resources.compare_groups,
        room_pieces=app_resources.rooms,
        complements=app_resources.complements,
        companion_search=CompanionSearchBuilder(app_resources.attributes),
        cart=CartService(repository),
        store_knowledge=app_resources.store_knowledge,
        support=(
            SupportService(
                SupportContactsRepository(session, owner_fallback=agent.support_owner_fallback),
                handover=agent.support_handover,
            )
            if agent.support_agent
            else None
        ),
        display=product_display_words(app_resources),
        subcategory_questions=app_resources.subcategory_questions,
        seating=app_resources.seating,
        looks=StockedLooks(capability, app_resources.attributes),
        shelf_depth=(settings.catalog.shelf_few_max, settings.catalog.shelf_some_max),
        catalog_reset=session.rollback,
    )


def chat_runtime(session: SessionDep, app_resources: ResourcesDep) -> ChatRuntime:
    """One chat exchange, or a refusal to build one: the team needs every
    agent's model, and the search its presentation limit."""
    team = app_resources.agent_team
    if team is None:
        raise ConfigurationError(
            detail="agents.supervisor and every agent's model must be set to chat",
            public_message="The shopping assistant is not configured.",
        )
    settings = app_resources.settings
    return ChatRuntime(
        team,
        agent_services(session, app_resources),
        session_store(app_resources),
        settings.session,
        deadline_s=settings.agents.turn_deadline_s,
        room_deadline_s=settings.agents.room_turn_deadline_s,
        arabic_replies=settings.customer_agent.arabic_replies,
        summarizer=app_resources.history_summarizer,
    )


ChatRuntimeDep = Annotated[ChatRuntime, Depends(chat_runtime)]


def chat_graph(app_resources: ResourcesDep) -> ChatGraphRunner:
    """The process-wide compiled graph. Never rebuilt per request."""
    return app_resources.chat_graph


ChatGraphDep = Annotated[ChatGraphRunner, Depends(chat_graph)]


# ── Furniture Finder ────────────────────────────────────────────────────────


def furniture_finder_service(
    session: SessionDep, app_resources: ResourcesDep
) -> FurnitureFinderService:
    """The finder, or a refusal naming what is missing.

    Requested only by the finder routes. A deployment without it starts and
    serves chat exactly as before; only a photo finds the capability absent.
    """
    settings = app_resources.settings.furniture_finder
    zory = app_resources.zory_finder
    if settings is None or zory is None:
        raise ConfigurationError(
            detail="furniture_finder must be configured to use Furniture Finder",
            public_message="Furniture Finder is not configured.",
        )
    repository = ProductRepository(session)
    return FurnitureFinderService(
        zory,
        FinderPhotoStore(app_resources.redis.client, app_resources.settings.session),
        repository,
        ProductHydrationService(repository),
        StoreRepository(session),
        session_store(app_resources),
        settings,
        local_visitors=app_resources.settings.environment is Environment.LOCAL,
        arabic_replies=app_resources.settings.customer_agent.arabic_replies,
    )


FurnitureFinderServiceDep = Annotated[FurnitureFinderService, Depends(furniture_finder_service)]


def reply_writer(session: AsyncSession, app_resources: AppResources) -> ReplyWriter | None:
    """An agent to word replies outside a chat turn, when the team is configured."""
    team = app_resources.agent_team
    if team is None:
        return None
    return ReplyWriter(
        team,
        agent_services(session, app_resources),
        deadline_s=app_resources.settings.agents.turn_deadline_s,
    )


def finder_turn_runtime(session: SessionDep, app_resources: ResourcesDep) -> FinderTurnRuntime:
    """A pick as a conversation turn: the finder plus the session it commits to."""
    return FinderTurnRuntime(
        furniture_finder_service(session, app_resources),
        session_store(app_resources),
        app_resources.settings.session,
        SimilarSearchBuilder(app_resources.taxonomy, app_resources.attributes),
        SearchRefinementComposer(
            app_resources.attributes, app_resources.dimensions, app_resources.seating
        ),
        arabic_replies=app_resources.settings.customer_agent.arabic_replies,
        compare_groups=app_resources.compare_groups,
        display=product_display_words(app_resources),
        writer=reply_writer(session, app_resources),
    )


FinderTurnRuntimeDep = Annotated[FinderTurnRuntime, Depends(finder_turn_runtime)]


def shopper_credentials(
    token: Annotated[
        str | None,
        Header(alias="X-Zory-Widget-Token", max_length=4096, pattern=WIDGET_SESSION_PATTERN),
    ] = None,
    widget_key: Annotated[
        str | None, Header(alias="X-Zory-Widget-Key", pattern=WIDGET_KEY_PATTERN)
    ] = None,
    store_public_id: Annotated[
        str | None, Header(alias="X-Zory-Widget-Store-Id", pattern=WIDGET_STORE_PATTERN)
    ] = None,
    origin: Annotated[
        str | None,
        Header(alias="X-Zory-Widget-Origin", max_length=270, pattern=WIDGET_ORIGIN_PATTERN),
    ] = None,
) -> ShopperCredentials | None:
    """The shopper's widget session, as the gateway forwarded it, or None.

    Transport only: the main platform verifies the token and decides the store
    from it. A malformed header is refused before anything runs.
    """
    if token is None:
        return None
    return ShopperCredentials(
        token=SecretStr(token),
        widget_key=widget_key,
        store_public_id=store_public_id,
        origin=origin,
    )


ShopperCredentialsDep = Annotated[ShopperCredentials | None, Depends(shopper_credentials)]


# ── Room visualisation ──────────────────────────────────────────────────────


def _room_visualizer(session: AsyncSession, app_resources: AppResources) -> RoomVisualizer:
    """The render pipeline, or a refusal naming what is missing."""
    settings = app_resources.settings.visualization
    generator = app_resources.render_generator
    photos = app_resources.render_photos
    if settings is None or generator is None or photos is None:
        raise ConfigurationError(
            detail="visualization must be configured to render rooms",
            public_message="Room visualisation is not configured.",
        )
    return RoomVisualizer(ProductRepository(session), photos, generator, settings)


def visualization_turn_runtime(
    session: SessionDep, app_resources: ResourcesDep
) -> VisualizationTurnRuntime:
    """A render of the session's room package."""
    return VisualizationTurnRuntime(
        _room_visualizer(session, app_resources),
        session_store(app_resources),
        app_resources.settings.session,
        _room_photo_store(app_resources),
        arabic_replies=app_resources.settings.customer_agent.arabic_replies,
        writer=reply_writer(session, app_resources),
    )


def catalog_visualization_runtime(
    session: SessionDep, app_resources: ResourcesDep
) -> CatalogVisualizationRuntime:
    """A render of pieces picked from the catalogue."""
    return CatalogVisualizationRuntime(
        _room_visualizer(session, app_resources),
        session_store(app_resources),
        app_resources.settings.session,
        app_resources.settings.effective_catalog(),
        app_resources.attributes,
        _room_photo_store(app_resources),
        arabic_replies=app_resources.settings.customer_agent.arabic_replies,
        writer=reply_writer(session, app_resources),
    )


def _room_photo_store(app_resources: AppResources) -> RoomPhotoStore:
    return RoomPhotoStore(app_resources.redis.client, app_resources.settings.session)


def room_photo_service(app_resources: ResourcesDep) -> RoomPhotoService:
    """Checking and emptying the customer's own room photo, or a refusal
    naming what is missing."""
    settings = app_resources.settings.visualization
    checker = app_resources.room_checker
    emptier = app_resources.empty_room_generator
    if settings is None or checker is None or emptier is None:
        raise ConfigurationError(
            detail="visualization.room_check_model must be configured to use room photos",
            public_message="Room photos are not configured.",
        )
    return RoomPhotoService(checker, emptier, _room_photo_store(app_resources), settings)


RoomPhotoServiceDep = Annotated[RoomPhotoService, Depends(room_photo_service)]


def catalog_service(session: SessionDep, app_resources: ResourcesDep) -> CatalogService:
    """Browsing needs only PostgreSQL and the registries, so it always exists;
    it says whether a selection can be rendered."""
    return CatalogService(
        ProductRepository(session),
        app_resources.taxonomy,
        app_resources.attributes,
        app_resources.settings.effective_catalog(),
        render_available=app_resources.render_generator is not None,
        display=product_display_words(app_resources),
        arabic_replies=app_resources.settings.customer_agent.arabic_replies,
    )


VisualizationTurnRuntimeDep = Annotated[
    VisualizationTurnRuntime, Depends(visualization_turn_runtime)
]
CatalogVisualizationRuntimeDep = Annotated[
    CatalogVisualizationRuntime, Depends(catalog_visualization_runtime)
]
CatalogServiceDep = Annotated[CatalogService, Depends(catalog_service)]
