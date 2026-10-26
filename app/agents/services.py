"""The services behind the agents' tools, built once per request.

The tools close over these; no model ever sees them, their credentials or the
store they serve.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.agents.operations.cart import CartOperations
from app.agents.operations.rooms import RoomEngine
from app.agents.operations.search import SearchRunner
from app.services.cart import CartService
from app.services.catalog_capability import CatalogCapabilityService
from app.services.comparison import ProductComparisonService
from app.services.cross_sell import CompanionSearchBuilder
from app.services.hydration import ProductHydrationService
from app.services.product_display import ProductDisplayWords
from app.services.query_understanding import QueryUnderstandingService
from app.services.reference_resolver import ProductReferenceResolver
from app.services.similar_search import SimilarSearchBuilder
from app.services.stocked_looks import StockedLooks
from app.services.store_knowledge import StoreKnowledgeService
from app.services.support import SupportService
from app.taxonomy.attributes import CatalogAttributes
from app.taxonomy.compare_groups import CompareGroups
from app.taxonomy.complements import Complements
from app.taxonomy.dimensions import DimensionSemantics
from app.taxonomy.registry import CommerceTaxonomy
from app.taxonomy.rooms import RoomPieces
from app.taxonomy.seating import SeatingSemantics
from app.taxonomy.subcategory_questions import SubcategoryQuestions


@dataclass(frozen=True, slots=True)
class AgentServices:
    taxonomy: CommerceTaxonomy
    attributes: CatalogAttributes
    dimensions: DimensionSemantics
    query_understanding: QueryUnderstandingService
    search: SearchRunner
    hydration: ProductHydrationService
    references: ProductReferenceResolver
    comparison: ProductComparisonService
    similar_search: SimilarSearchBuilder
    capabilities: CatalogCapabilityService
    cart_operations: CartOperations
    rooms: RoomEngine
    compare_groups: CompareGroups | None = None
    room_pieces: RoomPieces | None = None
    complements: Complements | None = None
    companion_search: CompanionSearchBuilder | None = None
    cart: CartService | None = None
    store_knowledge: StoreKnowledgeService | None = None
    support: SupportService | None = None
    display: ProductDisplayWords | None = None
    looks: StockedLooks | None = None
    """What each kind comes in at this store - for a design direction."""
    seating: SeatingSemantics | None = None
    """How many each seating type seats - reviewed data."""
    catalog_reset: Callable[[], Awaitable[None]] | None = None
    """Rolls the request's catalog session back. Work stopped by the deadline
    can be cancelled mid-query, leaving the session unusable; it is reset
    before anything reads the catalog again. The agent service only reads the
    catalog, so a rollback loses nothing."""
    shelf_depth: tuple[int, int] = (3, 15)
    """Where a kind's range stops being "few" and "some" on the store shelf -
    the depth of a choice as a word, never a count (`catalog.shelf_few_max`,
    `catalog.shelf_some_max`)."""
    subcategory_questions: SubcategoryQuestions | None = None
    """The questions the store's designers ask for each kind - knowledge an
    agent chooses from, never a script."""
