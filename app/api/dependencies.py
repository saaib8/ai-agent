"""FastAPI dependency wiring.

Resources come from application state (created in lifespan); repositories and
services are constructed per request around a single session, so they are
trivially replaceable with fakes in tests (CLAUDE.md 24).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConfigurationError
from app.core.lifespan import AppResources, get_resources
from app.integrations.llm import StructuredLLMClient
from app.integrations.postgres import Database
from app.integrations.redis import RedisClient
from app.orchestration.graph import ChatGraphRunner
from app.repositories.finder_photos import FinderPhotoStore
from app.repositories.products import ProductRepository
from app.repositories.sessions import SessionStore
from app.repositories.stores import StoreRepository
from app.services.bundle_optimizer import BundleOptimizer
from app.services.bundle_reference import BundleReferenceResolver
from app.services.catalog_capability import CatalogCapabilityService
from app.services.chat_runtime import ChatRuntime
from app.services.comparison import ProductComparisonService
from app.services.controlled_search import ControlledRelaxationService
from app.services.customer_decision import CustomerAgentDecisionService
from app.services.design_discovery import DesignDiscoveryService
from app.services.discovery import ProductDiscoveryService
from app.services.furniture_finder import FinderTurnRuntime, FurnitureFinderService
from app.services.health import HealthService
from app.services.hydration import ProductHydrationService
from app.services.interior_design import InteriorDesignAgent
from app.services.object_description import ObjectDescriber
from app.services.query_understanding import QueryUnderstandingService
from app.services.reference_resolver import ProductReferenceResolver
from app.services.refinement_composer import SearchRefinementComposer
from app.services.relative_price import RelativePriceResolver
from app.services.relaxation import RelaxationPlanner
from app.services.response_generator import CustomerResponseGenerator
from app.services.retailer_context import RetailerContextProvider
from app.services.search_pipeline import ProductSearchPipeline
from app.services.semantic_ranking import SemanticRankingService
from app.services.similar_search import SimilarSearchBuilder
from app.services.turn_coordinator import CustomerTurnCoordinator
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
    return SemanticRankingService(app_resources.embedder, app_resources.semantic_index)


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


def customer_agent_decision_service(
    app_resources: ResourcesDep,
) -> CustomerAgentDecisionService:
    """The Customer Agent's decision step, or a refusal to build one.

    `decision_model` has no default: interpreting a message and deciding a turn
    are different jobs, and quietly borrowing the query-understanding model
    would hide that. So an absent model means this capability is not
    configured, and asking for it fails here rather than at the provider.

    Requested only where it is used, so a deployment that has not configured it
    still starts and serves everything else.
    """
    client = app_resources.decision_llm
    if client is None:
        raise ConfigurationError(
            detail=("customer_agent.decision_model must be set to use the customer agent"),
            public_message="The customer agent is not configured.",
        )
    return CustomerAgentDecisionService(client, app_resources.attributes)


CustomerAgentDecisionServiceDep = Annotated[
    CustomerAgentDecisionService, Depends(customer_agent_decision_service)
]


def customer_turn_coordinator(
    session: SessionDep, app_resources: ResourcesDep
) -> CustomerTurnCoordinator:
    """The whole customer turn, or a refusal to build one.

    Everything it composes already exists; what it can lack is configuration.
    Both the decision model and the presentation limit are requested through
    their own factories, so an unconfigured deployment fails here with the
    reason that applies rather than part-way through a turn.

    Requested only where it is used. Nothing builds this at startup, so a
    deployment without customer-agent configuration still starts and serves
    everything else.
    """
    settings = app_resources.settings
    # Configuration first, so an unconfigured deployment is refused before any
    # collaborator is built rather than part-way through constructing one.
    decisions = customer_agent_decision_service(app_resources)
    pipeline = product_search_pipeline(session, app_resources)
    # Optional on purpose. A deployment that has not configured the design
    # specialist still sells furniture: searching, comparing and answering all
    # work, and only a request for a whole room finds the capability missing.
    design = optional_interior_design_agent(app_resources)

    repository = ProductRepository(session)
    resolver = ProductReferenceResolver(repository, app_resources.attributes)
    return CustomerTurnCoordinator(
        decisions,
        query_understanding_service(
            app_resources.llm,
            app_resources.taxonomy,
            app_resources.attributes,
            app_resources.dimensions,
        ),
        SearchRefinementComposer(
            app_resources.attributes, app_resources.dimensions, app_resources.seating
        ),
        resolver,
        RelativePriceResolver(resolver, repository),
        ProductComparisonService(repository, app_resources.dimensions, settings.customer_agent),
        pipeline,
        product_hydration_service(session),
        SimilarSearchBuilder(app_resources.taxonomy, app_resources.attributes),
        catalog_capability_service(session, app_resources),
        design,
        design_discovery_service(session, app_resources),
        BundleReferenceResolver(app_resources.taxonomy),
        BundleOptimizer(),
        app_resources.dimensions,
        app_resources.taxonomy,
    )


def optional_interior_design_agent(
    app_resources: ResourcesDep,
) -> InteriorDesignAgent | None:
    """The design specialist if it is configured, and None if it is not.

    `interior_design.model` has no default and never borrows another agent's:
    four agents, four prompts, four identifiers. An absent one means the
    capability does not exist here, which is an ordinary deployment rather than
    a misconfiguration - so a caller that can do without it gets None instead
    of an exception.
    """
    client = app_resources.design_llm
    if client is None:
        return None
    return InteriorDesignAgent(client, app_resources.taxonomy)


def interior_design_agent(app_resources: ResourcesDep) -> InteriorDesignAgent:
    """The design specialist, or a refusal to build one.

    For callers that *are* the design capability. Asking for it by name when it
    is unconfigured is a configuration error; a commerce turn that merely might
    reach it asks through :func:`optional_interior_design_agent` instead.
    """
    agent = optional_interior_design_agent(app_resources)
    if agent is None:
        raise ConfigurationError(
            detail="interior_design.model must be set to use the design specialist",
            public_message="Design guidance is not configured.",
        )
    return agent


InteriorDesignAgentDep = Annotated[InteriorDesignAgent, Depends(interior_design_agent)]


def catalog_capability_service(
    session: SessionDep, app_resources: ResourcesDep
) -> CatalogCapabilityService:
    """What the active retailer stocks. Deterministic, no configuration."""
    return CatalogCapabilityService(ProductRepository(session), app_resources.taxonomy)


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
        product_search_pipeline(session, app_resources), app_resources.taxonomy
    )


DesignDiscoveryServiceDep = Annotated[DesignDiscoveryService, Depends(design_discovery_service)]


def customer_response_generator(
    app_resources: ResourcesDep,
) -> CustomerResponseGenerator:
    """The response layer, or a refusal to build one.

    `response_model` has no default. Deciding a turn and writing its reply are
    different jobs with different prompts, so borrowing the decision model
    would hide that - an absent model means this capability is not configured,
    and asking for it fails here rather than at the provider.
    """
    client = app_resources.response_llm
    if client is None:
        raise ConfigurationError(
            detail=("customer_agent.response_model must be set to generate responses"),
            public_message="The customer agent is not configured.",
        )
    return CustomerResponseGenerator(client)


CustomerResponseGeneratorDep = Annotated[
    CustomerResponseGenerator, Depends(customer_response_generator)
]
CustomerTurnCoordinatorDep = Annotated[CustomerTurnCoordinator, Depends(customer_turn_coordinator)]
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


def chat_runtime(session: SessionDep, app_resources: ResourcesDep) -> ChatRuntime:
    """One chat exchange, or a refusal to build one.

    Both reasoning capabilities are requested through their own factories, so
    an unconfigured deployment is refused here with the reason that applies
    rather than part-way through a turn.
    """
    return ChatRuntime(
        customer_turn_coordinator(session, app_resources),
        customer_response_generator(app_resources),
        session_store(app_resources),
        app_resources.settings.session,
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
    detector = app_resources.detector
    vision = app_resources.finder_vision
    embedder = app_resources.finder_embedder
    index = app_resources.finder_index
    if (
        settings is None
        or detector is None
        or vision is None
        or embedder is None
        or index is None
    ):
        raise ConfigurationError(
            detail="furniture_finder must be configured to use Furniture Finder",
            public_message="Furniture Finder is not configured.",
        )
    repository = ProductRepository(session)
    return FurnitureFinderService(
        detector,
        ObjectDescriber(vision),
        embedder,
        index,
        FinderPhotoStore(app_resources.redis.client, app_resources.settings.session),
        repository,
        ProductHydrationService(repository),
        settings,
    )


FurnitureFinderServiceDep = Annotated[FurnitureFinderService, Depends(furniture_finder_service)]


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
    )


FinderTurnRuntimeDep = Annotated[FinderTurnRuntime, Depends(finder_turn_runtime)]
