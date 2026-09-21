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
from app.repositories.products import ProductRepository
from app.repositories.stores import StoreRepository
from app.services.comparison import ProductComparisonService
from app.services.controlled_search import ControlledRelaxationService
from app.services.customer_decision import CustomerAgentDecisionService
from app.services.discovery import ProductDiscoveryService
from app.services.health import HealthService
from app.services.hydration import ProductHydrationService
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
RetailerContextProviderDep = Annotated[
    RetailerContextProvider, Depends(retailer_context_provider)
]
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


ProductDiscoveryServiceDep = Annotated[
    ProductDiscoveryService, Depends(product_discovery_service)
]
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
            detail=(
                "customer_agent.presentation_limit must be set to use product search"
            ),
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
            detail=(
                "customer_agent.decision_model must be set to use the customer agent"
            ),
            public_message="The customer agent is not configured.",
        )
    return CustomerAgentDecisionService(client)


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
        SearchRefinementComposer(app_resources.attributes, app_resources.dimensions),
        resolver,
        RelativePriceResolver(resolver, repository),
        ProductComparisonService(
            repository, app_resources.dimensions, settings.customer_agent
        ),
        pipeline,
        product_hydration_service(session),
        SimilarSearchBuilder(app_resources.taxonomy, app_resources.attributes),
    )


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
            detail=(
                "customer_agent.response_model must be set to generate responses"
            ),
            public_message="The customer agent is not configured.",
        )
    return CustomerResponseGenerator(client)


CustomerResponseGeneratorDep = Annotated[
    CustomerResponseGenerator, Depends(customer_response_generator)
]
CustomerTurnCoordinatorDep = Annotated[
    CustomerTurnCoordinator, Depends(customer_turn_coordinator)
]
SemanticRankingServiceDep = Annotated[
    SemanticRankingService, Depends(semantic_ranking_service)
]
ProductSearchPipelineDep = Annotated[
    ProductSearchPipeline, Depends(product_search_pipeline)
]
ProductHydrationServiceDep = Annotated[
    ProductHydrationService, Depends(product_hydration_service)
]
