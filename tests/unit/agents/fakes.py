"""Doubles for what the agents' services reach outside the process for.

The catalog and the planners are faked; everything
deterministic - taxonomy, query validation, the search runner's commit, the
cart reducers - is real.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from app.agents.context import ToolContext
from app.agents.loop import AgentLoop
from app.agents.operations.cart import CartOperations
from app.agents.operations.rooms import RoomEngine
from app.agents.operations.search import SearchRunner
from app.agents.services import AgentServices
from app.agents.tools.catalog import build_registry
from app.agents.tools.schemas import ToolSchemas
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    CustomerPreferenceState,
    ProductInteractionState,
    RoomProjectState,
)
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import ProductSearchRequest
from app.schemas.grounding import SearchExecutionGrounding, SearchOutcome
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    ResolvedSearch,
    SemanticPreference,
    build_constrained_interpretation,
)
from app.schemas.relaxation import StopReason
from app.schemas.resolution import ProductSearchExecutionResult, ResolvedProductReference
from app.schemas.retailer import RetailerContext
from app.services.comparison import ProductComparisonService
from app.services.grounding_builder import to_grounded_product
from app.services.query_understanding import QueryUnderstandingService
from app.services.refinement_composer import SearchRefinementComposer
from app.services.similar_search import SimilarSearchBuilder
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics

STOCKED_WITH_RANGE = 12
CONTEXT = RetailerContext(store_id=50)
SOFAS = ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa")
RECLINERS = ProductSearchRequest(commerce_category="seating", commerce_subcategory="recliner")
TABLES = ProductSearchRequest(commerce_category="tables", commerce_subcategory="console")
OFF_SCREEN = 42
"""A product the references resolve to that is on no list."""

TAXONOMY = load_taxonomy()
ATTRIBUTES = load_catalog_attributes()
DIMENSIONS = load_dimension_semantics(taxonomy=TAXONOMY)


def _product(
    product_id: int, *, subcategory: str | None = "sofa", price: str | None = None
) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Sofa {product_id}",
        name_arabic="كنبة",
        price_amount=Decimal(price or "1000"),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(category="seating", subcategory=subcategory),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


def _state(
    *,
    request: ProductSearchRequest | None = SOFAS,
    revision: int = 1,
    presented: tuple[int, ...] = (10, 11),
    selected: tuple[int, ...] = (OFF_SCREEN,),
    focus: int | None = None,
    preferences: tuple[SemanticPreference, ...] = (),
    room: RoomProjectState | None = None,
) -> AgentStateV1:
    return AgentStateV1(
        customer_preferences=CustomerPreferenceState(semantic_preferences=preferences),
        active_search=(ActiveSearchState(request=request, revision=revision) if request else None),
        product_interaction=ProductInteractionState(
            presented_product_ids=presented,
            presented_search_revision=revision if presented and request else None,
            selected_product_ids=selected,
            focused_product_id=focus,
        ),
        room_project=room,
    )


def _resolved(request: ProductSearchRequest = SOFAS, **kwargs: Any) -> ResolvedSearch:
    return ResolvedSearch(request=request, semantics=ConstraintSemantics(), **kwargs)


def _preference(value: str) -> SemanticPreference:
    return SemanticPreference(
        family=AttributeFamily.STYLE,
        raw_value=value,
        canonical_value=value,
        strength=ConstraintStrength.PREFERRED,
    )


class FakeReferences:
    """Resolves by selector type, so a turn can mix resolvable and not."""

    def __init__(self, outcomes: dict[str, Any] | None = None, default: Any = None):
        self.outcomes = outcomes or {}
        self.default = default or ResolvedProductReference(product_id=OFF_SCREEN)
        self.calls: list[tuple[Any, AgentStateV1]] = []

    async def resolve(self, selector: Any, state: AgentStateV1, context: Any) -> Any:
        self.calls.append((selector, state))
        return self.outcomes.get(type(selector).__name__, self.default)


class FakeComparison:
    def __init__(self, outcome: Any):
        self.outcome = outcome
        self.calls: list[list[int]] = []

    async def compare(self, product_ids: Any, context: Any) -> Any:
        self.calls.append(list(product_ids))
        return self.outcome


class FakePipeline:
    def __init__(self, ids: tuple[int, ...] = (), error: Exception | None = None):
        self.ids, self.error = ids, error
        self.calls: list[Any] = []
        self.contexts: list[RetailerContext] = []

    async def execute(
        self,
        resolved: Any,
        context: Any,
        *,
        dropped_constraints: Any = (),
        earlier_sizes_applied: bool = False,
        presentation_limit: int | None = None,
    ) -> ProductSearchExecutionResult:
        self.calls.append(resolved)
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return ProductSearchExecutionResult(
            presented_product_ids=self.ids,
            grounding=SearchExecutionGrounding(
                outcome=SearchOutcome.RESULTS if self.ids else SearchOutcome.ZERO_RESULTS,
                products=tuple(
                    to_grounded_product(
                        _product(pid),
                        grounding_ref=n,
                        presented_ordinal=n,
                        relaxation_depth=0,
                    )
                    for n, pid in enumerate(self.ids, start=1)
                ),
                eligible_count=len(self.ids),
                ranked_count=len(self.ids),
                selected_count=len(self.ids),
                presented_count=len(self.ids),
                exact_candidate_count=len(self.ids),
                stop_reason=StopReason.EXACT_SUFFICIENT,
            ),
        )


class FakeHydration:
    def __init__(self, available: tuple[int, ...] = (OFF_SCREEN,), price: str | None = None):
        self.available = available
        self.price = price
        self.calls: list[list[int]] = []

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[Any, ...]:
        self.calls.append(list(product_ids))
        return tuple(_product(p, price=self.price) for p in product_ids if p in self.available)


class FakeCapabilities:
    """The retailer's stocked types, or an unreachable catalog."""

    def __init__(
        self,
        pairs: tuple[tuple[str, str | None], ...] = (("seating", "sofa"),),
        error: Exception | None = None,
        currency: str | None = None,
    ) -> None:
        self.pairs = pairs
        self.error = error
        self.currency = currency
        self.calls: list[Any] = []

    async def overview(self, context: Any) -> Any:
        if self.error is not None:
            raise self.error
        from app.schemas.catalog_overview import CatalogOverview

        return CatalogOverview(store_id=context.store_id, currency=self.currency)

    async def capabilities(self, context: Any) -> Any:
        self.calls.append(context)
        if self.error is not None:
            raise self.error
        from app.schemas.retailer import (
            RetailerCatalogCapabilities,
            RetailerCatalogCapability,
        )

        return RetailerCatalogCapabilities(
            capabilities=tuple(
                RetailerCatalogCapability(
                    commerce_category=category,
                    commerce_subcategory=subcategory,
                    active_product_count=STOCKED_WITH_RANGE,
                )
                for category, subcategory in self.pairs
            )
        )


def agent_services(
    *,
    pipeline: FakePipeline | None = None,
    hydration: FakeHydration | None = None,
    references: FakeReferences | None = None,
    capabilities: FakeCapabilities | None = None,
    max_picks: int = 10,
    **optional: Any,
) -> AgentServices:
    """Real deterministic services over faked catalog reads.

    The query-understanding service is real for its validation (`resolve`),
    which calls no model; its provider client is never reached here.
    """
    pipeline = pipeline or FakePipeline(ids=(10, 11, 12))
    hydration = hydration or FakeHydration(available=pipeline.ids)
    capabilities = capabilities or FakeCapabilities()
    return AgentServices(
        taxonomy=TAXONOMY,
        attributes=ATTRIBUTES,
        dimensions=DIMENSIONS,
        query_understanding=QueryUnderstandingService(
            cast(Any, None), TAXONOMY, ATTRIBUTES, DIMENSIONS
        ),
        search=SearchRunner(
            cast(Any, pipeline), SearchRefinementComposer(ATTRIBUTES, DIMENSIONS, None), None
        ),
        hydration=cast(Any, hydration),
        references=cast(Any, references or FakeReferences()),
        comparison=cast(ProductComparisonService, FakeComparison(None)),
        similar_search=SimilarSearchBuilder(TAXONOMY, ATTRIBUTES),
        capabilities=cast(Any, capabilities),
        cart_operations=CartOperations(
            max_picks=max_picks, max_quantity=10, max_likes=20, check=None
        ),
        rooms=cast(RoomEngine, None),
        **optional,
    )


def real_loop(max_tool_calls: int = 6) -> AgentLoop:
    """The agent loop over the real tool catalogue, as the process builds it."""
    rooms = load_room_pieces(taxonomy=TAXONOMY, seating=load_seating_semantics(taxonomy=TAXONOMY))
    registry = build_registry(
        ToolSchemas(
            interpretation=build_constrained_interpretation(TAXONOMY, ATTRIBUTES),
            attributes=ATTRIBUTES,
            room_pieces=rooms,
        )
    )
    return AgentLoop(registry, max_tool_calls=max_tool_calls)


def tool_context(services: AgentServices | None = None, **extra: Any) -> ToolContext:
    return ToolContext(retailer=CONTEXT, services=services or cast(AgentServices, None), **extra)
