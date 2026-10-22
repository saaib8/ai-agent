"""The room engine: every whole-room operation Nora's tools call.

Deterministic all the way down. A model decides *that* a room should be
planned, revised or edited; this module decides nothing about the catalog. It
asks the design specialist how the pieces should feel (or, for a room the
registry does not know, which pieces it needs), finds real products through
Product Discovery, chooses the room with the bundle optimiser, and commits the
result through the state reducers - nowhere else.

The rules it keeps are the ones that are facts, not conversation:

* **Scope** comes only from the `RetailerContext` passed in; nothing a model
  writes can name a store or a product id (CLAUDE.md 8, 20.2).
* **Products** come only from services, re-read before they are trusted.
* **Money** is summed in code: the optimiser's totals and Decimal arithmetic
  here, never a model's figure.
* **Locked pieces are preserved**: they enter every optimisation as hard locks,
  and an operation that cannot verify one stops rather than dropping it.

Every expected failure is returned as a code with the state unchanged; only a
defect propagates. Ordinals ("piece 2") are the card order the room package
shows (`group_bundle_cards`), the same order `present_room_bundle` renders.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agents.facts import design_notes
from app.core.exceptions import (
    IntegrationUnavailableError,
    LLMResponseInvalidError,
    TaxonomyValidationError,
)
from app.core.logging import get_logger
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_state import (
    MAX_FINISHED_ROOMS,
    MAX_ROOM_ANCHORS,
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    RoomDesignNeedState,
    RoomProjectState,
    RoomSwapState,
)
from app.schemas.agent_updates import (
    ActiveSearchUpdate,
    AddBundleLine,
    AgentStateUpdate,
    BundleLineSpec,
    BundleOperation,
    ClearSemanticIntent,
    DesignNeedRefinement,
    DesignNeedSpec,
    PlannedBundleLineSpec,
    PreservedBundleLine,
    RefineBundle,
    RemoveBundleLine,
    ReplaceDesignPlan,
    ReplaceItems,
    RoomProjectUpdate,
    SetBundleLineAcquisition,
    SetBundleLineStatus,
    SetSemanticIntent,
)
from app.schemas.bundle import (
    BundleOptimizationOutcome,
    BundleOptimizationRequest,
    BundleStatus,
    BundleUnavailable,
    LockedBundleProduct,
    RoomBundle,
)
from app.schemas.bundle_presentation import GroundedBundlePresentation
from app.schemas.design import (
    MAX_REGULAR_SEATING_COUNT,
    DesignCategoryNeed,
    DesignPriority,
    DesignRevisionContext,
    DesignTask,
    InteriorDesignRequest,
    InteriorDesignResult,
)
from app.schemas.design_discovery import (
    DesignDiscoveryResult,
    DesignNeedCandidates,
    DesignNeedSkipReason,
)
from app.schemas.design_override import DesignNeedSearchOverride
from app.schemas.discovery import (
    MAX_EXCLUDED_PRODUCT_IDS,
    PriceConstraint,
    ProductSearchRequest,
    SeatingCapacityConstraint,
)
from app.schemas.geometry import RoomGeometry
from app.schemas.grounding import GroundedProduct
from app.schemas.product import ProductCandidate
from app.schemas.query import ConstraintStrength, ResolvedSearch, SemanticPreference
from app.schemas.resolution import CandidatePoolResult, ProductSearchExecutionResult
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerContext
from app.schemas.seating_solution import SeatingArrangement, SeatingRequirements
from app.services.agent_state import apply_update, commit_search_results, remember_next_sizes
from app.services.bundle_cards import BundleCardGroup, group_bundle_cards
from app.services.bundle_presentation import (
    build_state_bundle_presentation,
    present_room_bundle,
)
from app.services.design_facts import project_anchors
from app.services.design_revision import project_current_plan
from app.services.room_cart import finished
from app.services.room_composition import (
    PRIORITY_FOR_TIER,
    composed_needs,
    default_pieces,
    finishing_offers,
    offered,
    piece_for,
    seating_piece,
)
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.dimensions import DimensionSemantics
from app.taxonomy.rooms import RoomPieces, RoomTemplate
from app.taxonomy.seating import SEATING_CATEGORY, SeatingSemantics
from app.taxonomy.words import customer_words

logger = get_logger(__name__)

_DESIGN_FAILURES = (IntegrationUnavailableError, LLMResponseInvalidError, TaxonomyValidationError)
"""The specialist could not give a plan now: unreachable, unreadable, or a
type outside the vocabulary. A fact about us, never about the store."""

_CATALOG_FAILURES = (IntegrationUnavailableError, LLMResponseInvalidError)
"""A catalog, index or provider that could not be reached now. Ranking
integrity and taxonomy errors are defects and are deliberately not here."""

MAX_ROLE_WORDS = 60

PieceStatus = Literal["locked", "unlocked", "owned"]


# ── collaborators (structural, so tests can hand in fakes) ──────────────────


class _Design(Protocol):
    async def plan(self, request: InteriorDesignRequest) -> InteriorDesignResult: ...


class _DesignDiscovery(Protocol):
    async def discover(
        self,
        request: InteriorDesignRequest,
        result: InteriorDesignResult,
        context: RetailerContext,
        *,
        overrides: Mapping[int, DesignNeedSearchOverride] | None = None,
    ) -> DesignDiscoveryResult: ...

    def resolve_need(
        self, need: DesignCategoryNeed, request: InteriorDesignRequest
    ) -> ResolvedSearch: ...


class _Optimizer(Protocol):
    def optimize(self, request: BundleOptimizationRequest) -> BundleOptimizationOutcome: ...


class _SeatingPlanner(Protocol):
    async def arrangements(
        self,
        *,
        target_seats: int,
        budget_amount: Decimal | None,
        currency: str,
        context: RetailerContext,
        types: frozenset[str],
        requirements: SeatingRequirements | None = None,
    ) -> tuple[SeatingArrangement, ...]: ...


class _Pipeline(Protocol):
    async def execute(
        self,
        resolved: ResolvedSearch,
        context: RetailerContext,
        *,
        presentation_limit: int | None = None,
    ) -> ProductSearchExecutionResult: ...

    async def execute_forced_pool(
        self, product_id: int, context: RetailerContext
    ) -> CandidatePoolResult: ...


class _Capabilities(Protocol):
    async def capabilities(self, context: RetailerContext) -> RetailerCatalogCapabilities: ...


class _Hydration(Protocol):
    async def hydrate_ids(
        self, product_ids: Sequence[int], context: RetailerContext
    ) -> Sequence[ProductCandidate]: ...


# ── contracts ────────────────────────────────────────────────────────────────


class RoomPlanInputs(BaseModel):
    """Everything a room is planned from. Approved values only; the tool layer
    has already read the customer's words into them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_kind: str | None = Field(default=None, max_length=40)
    """A room registry key (`living_room`, `bedroom`), or None for any other
    room. A key the registry does not hold is treated as None."""
    room_type: str = Field(min_length=1, max_length=60)
    """The room in words, for the designer: "living room", "home office"."""
    budget: PriceConstraint | None = None
    geometry: RoomGeometry | None = None
    """None keeps the measurements already on record for the same room."""
    pieces: tuple[str, ...] | None = None
    """Room registry piece keys. None: the registry's essential and recommended
    pieces for a known room, or the designer's choice for any other."""
    seats: int | None = Field(default=None, ge=1, le=MAX_REGULAR_SEATING_COUNT)
    """How many people the room's seating must seat (a living room)."""
    colours: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    around_picks: bool = False
    """Lock the customer's picks that belong in this room into it."""
    same_room: bool | None = None
    """The agent's word on whether this is the room already on record
    (replanned, keeping its locked pieces) or a new one. None: the same
    registry kind, or the same words, is the same room."""


@dataclass(frozen=True)
class RoomOutcome:
    state: AgentStateV1
    """The staged state after the change; the input state when nothing committed."""
    bundle: RoomBundle | None
    """The optimiser's room, when one was chosen (committed or proposed)."""
    code: str
    committed: bool
    facts: dict[str, object] = field(default_factory=dict)
    """Model-facing and id-free: the pieces by card ordinal, money summed in code."""
    view: GroundedBundlePresentation | None = None

    def presentation(self) -> GroundedBundlePresentation | None:
        return self.view


@dataclass(frozen=True)
class PieceOptions:
    state: AgentStateV1
    code: str
    facts: dict[str, object] = field(default_factory=dict)
    products: tuple[GroundedProduct, ...] = ()


# ── the engine ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Built:
    plan: InteriorDesignResult
    outcome: BundleOptimizationOutcome


class RoomEngine:
    """Plans, revises and edits the customer's room against live catalog facts."""

    def __init__(
        self,
        design: _Design | None,
        design_discovery: _DesignDiscovery,
        optimizer: _Optimizer,
        seating_planner: _SeatingPlanner,
        pipeline: _Pipeline,
        capabilities: _Capabilities,
        hydration: _Hydration,
        rooms: RoomPieces | None,
        seating: SeatingSemantics | None,
        *,
        dimensions: DimensionSemantics | None = None,
        attributes: CatalogAttributes | None = None,
    ) -> None:
        self._design = design
        self._design_discovery = design_discovery
        self._optimizer = optimizer
        self._seating_planner = seating_planner
        self._pipeline = pipeline
        self._capabilities = capabilities
        self._hydration = hydration
        self._rooms = rooms
        self._seating = seating
        self._dimensions = dimensions
        """Needed to tell the designer the sizes of locked pieces; without it
        the designer is told nothing about them rather than a guess."""
        self._attributes = attributes
        """When given, colours and styles are kept only as approved values."""

    # ── planning ────────────────────────────────────────────────────────────

    async def plan(
        self, state: AgentStateV1, context: RetailerContext, inputs: RoomPlanInputs
    ) -> RoomOutcome:
        """Plan a room from its inputs and commit it.

        The same room (same kind, or the same words) is replanned with its
        locked pieces kept; a different room starts fresh.
        """
        if self._design is None:
            return _refused(state, "design_unavailable")
        template = self._template(inputs.room_kind)
        kind = template.kind if template is not None else None
        current = state.room_project
        # Whether this is the room on record is the agent's call when it says;
        # unsaid, the same registry kind or the same words.
        same = current is not None and (
            inputs.same_room
            if inputs.same_room is not None
            else _same_room(current, kind, inputs.room_type)
        )
        if same and current is not None and current.in_cart:
            return _refused(state, "room_in_cart")
        # A new room while one is in the cart: that room is finished first, so
        # its cart lines keep its name.
        done = finished(state) if not same else None
        rooms_done = (
            (*state.finished_rooms, done)[-MAX_FINISHED_ROOMS:]
            if done is not None
            else state.finished_rooms
        )
        base = (
            state
            if same
            else state.model_copy(
                update={
                    "room_project": None,
                    "room_swap": None,
                    "swap_budget_offer": None,
                    "finished_rooms": rooms_done,
                }
            )
        )
        pieces: tuple[str, ...] | None = None
        if template is not None:
            known = tuple(
                key for key in dict.fromkeys(inputs.pieces or ()) if template.piece(key) is not None
            )
            kept = current.pieces if same and current is not None else None
            pieces = known or kept
        # Replanning the same room keeps what this call does not restate: a
        # budget, a head count or a taste given earlier is still theirs.
        restated_taste = bool(inputs.colours or inputs.styles)
        staged = apply_update(
            base,
            AgentStateUpdate(
                room_project=RoomProjectUpdate(
                    # The same room keeps its name and kind, so its walls and
                    # doorways are not taken for another room's.
                    room_type=current.room_type or inputs.room_type
                    if same and current is not None
                    else inputs.room_type,
                    room_kind=(current.room_kind or kind) if same and current is not None else kind,
                    budget=inputs.budget,
                    clear_budget=inputs.budget is None and not same,
                    geometry=inputs.geometry,
                    regular_seating_count=inputs.seats,
                    clear_regular_seating_count=inputs.seats is None and not same,
                    pieces=pieces,
                    design_preferences=(
                        ReplaceItems(items=self._preferences(inputs.colours, inputs.styles))
                        if restated_taste or not same
                        else None
                    ),
                )
            ),
        )
        if template is not None and pieces is None:
            # Which pieces the room holds is the designer's choice with the
            # customer, never a default: keep what this call said, say what the
            # room could hold, and build nothing yet.
            return await self._pieces_to_choose(
                staged, state, context, template, around_picks=inputs.around_picks
            )
        # The picks saved while the pieces were chosen are locked into the
        # room's first build - those picks, not whatever the cart holds now.
        anchors = (
            current.anchor_product_ids
            if same and current is not None and not current.bundle_items
            else ()
        )
        if inputs.around_picks or anchors:
            try:
                staged = await self._lock_picks(
                    staged, state, context, template, only=None if inputs.around_picks else anchors
                )
            except _CATALOG_FAILURES:
                logger.warning("room_engine_picks_unreadable", store_id=context.store_id)
                return _refused(state, "catalog_unavailable")
        return await self._build_and_commit(state, staged, context, replanning=same)

    async def _pieces_to_choose(
        self,
        staged: AgentStateV1,
        original: AgentStateV1,
        context: RetailerContext,
        template: RoomTemplate,
        *,
        around_picks: bool,
    ) -> RoomOutcome:
        """The pieces this room could hold that the store can supply, each with
        how much a room of this kind needs it - facts for choosing, nothing
        built. What the call said of the room (its budget, size, seats, taste,
        and the picks it is built around) is kept, so the answer to "which
        pieces?" builds the room they described."""
        try:
            capabilities = await self._capabilities.capabilities(context)
            pieces = offered(template, capabilities)
            picks = original.product_interaction.selected_product_ids
            held = await self._hydration.hydrate_ids(picks, context) if around_picks else ()
        except _CATALOG_FAILURES:
            return _refused(original, "catalog_unavailable")
        filled = _pieces_in(template, held)
        built = original.room_project
        planned = staged.room_project
        if (
            built is not None
            and built.bundle_items
            and planned is not None
            and not _same_room(built, planned.room_kind, planned.room_type or "")
        ):
            # A designed room is not given up for one whose pieces are still
            # being chosen: nothing is kept until the new one is built.
            staged = original
        kept = staged is not original
        if around_picks and kept:
            anchors = tuple(
                p.product_id
                for p in held
                if piece_for(template, p.commerce.category, p.commerce.subcategory) is not None
            )[:MAX_ROOM_ANCHORS]
            staged = apply_update(
                staged,
                AgentStateUpdate(room_project=RoomProjectUpdate(anchor_product_ids=anchors)),
            )
        return RoomOutcome(
            state=staged,
            bundle=None,
            code="choose_pieces",
            committed=kept,
            facts={
                "code": "choose_pieces",
                # Kept: plan with the pieces alone and the rest stands. Not
                # kept (another room is designed): say it all again then.
                "inputs_kept": kept,
                "room_could_hold": [
                    {
                        "piece": piece.key,
                        "name": piece.label,
                        **({"name_ar": piece.label_ar} if piece.label_ar else {}),
                        "tier": str(piece.tier),
                        **({"quantity": piece.quantity} if piece.quantity > 1 else {}),
                        **({"seating": True} if piece.is_seating else {}),
                        **({"their_pick_fills_it": True} if piece.key in filled else {}),
                    }
                    for piece in pieces
                ],
            },
        )

    async def revise(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        *,
        budget: PriceConstraint | None = None,
        geometry: RoomGeometry | None = None,
        seats: int | None = None,
        colours: tuple[str, ...] | None = None,
        styles: tuple[str, ...] | None = None,
    ) -> RoomOutcome:
        """Plan the same room again with what changed; locked pieces stay."""
        room = state.room_project
        if room is None or not (room.design_needs or room.bundle_items):
            return _refused(state, "no_room")
        if room.in_cart:
            return _refused(state, "room_in_cart")
        if self._design is None:
            return _refused(state, "design_unavailable")
        preferences: ReplaceItems[SemanticPreference] | None = None
        if colours is not None or styles is not None:
            kept_colours = colours if colours is not None else _values(room, AttributeFamily.COLOR)
            kept_styles = styles if styles is not None else _values(room, AttributeFamily.STYLE)
            preferences = ReplaceItems(items=self._preferences(kept_colours, kept_styles))
        staged = apply_update(
            state,
            AgentStateUpdate(
                room_project=RoomProjectUpdate(
                    budget=budget,
                    geometry=geometry,
                    regular_seating_count=seats,
                    design_preferences=preferences,
                )
            ),
        )
        return await self._build_and_commit(state, staged, context, replanning=True)

    async def _build_and_commit(
        self,
        original: AgentStateV1,
        staged: AgentStateV1,
        context: RetailerContext,
        *,
        replanning: bool,
    ) -> RoomOutcome:
        try:
            locks = await self._verify_locks(staged, context)
        except _CATALOG_FAILURES:
            return _refused(original, "catalog_unavailable")
        if locks is None:
            return _refused(original, "locked_unavailable")
        try:
            capabilities = await self._capabilities.capabilities(context)
        except _CATALOG_FAILURES:
            return _refused(original, "catalog_unavailable")

        room = staged.room_project
        assert room is not None, "a room was staged"
        template = self._template(room.room_kind)
        request = self._design_request(
            staged, capabilities, locks, revise=replanning and template is None
        )
        assert self._design is not None
        try:
            designed = await self._design.plan(request)
        except _DESIGN_FAILURES:
            logger.warning("room_engine_design_unavailable", store_id=context.store_id)
            return _refused(original, "design_unavailable")

        locked = tuple(product for _, product in locks)
        try:
            if template is not None:
                built = await self._composed_room(
                    template, room, context, request, designed, capabilities, locked
                )
            else:
                discovery = await self._design_discovery.discover(request, designed, context)
                built = _Built(
                    plan=designed,
                    outcome=self._optimizer.optimize(
                        BundleOptimizationRequest(
                            discovery=discovery, budget=room.budget, locked=locked
                        )
                    ),
                )
        except _CATALOG_FAILURES:
            logger.warning("room_engine_discovery_unavailable", store_id=context.store_id)
            return _refused(original, "catalog_unavailable")

        outcome = built.outcome
        guidance = [g.summary for g in designed.guidance]
        if not isinstance(outcome, RoomBundle):
            return _unavailable(original, outcome)
        if outcome.status is BundleStatus.INFEASIBLE:
            proposed = self._proposal(original, outcome, "infeasible", room.budget, guidance)
            proposed.facts.update(design_notes(designed))
            return proposed
        committed = _commit_plan(staged, built.plan, outcome)
        logger.info(
            "room_engine_planned",
            store_id=context.store_id,
            room_kind=room.room_kind,
            need_count=len(built.plan.needs),
            line_count=len(outcome.lines),
            status=str(outcome.status),
        )
        planned = self._committed(
            remember_next_sizes(committed, designed.next_sizes), outcome, guidance=guidance
        )
        planned.facts.update(design_notes(designed))
        return planned

    # ── one piece's options, and swapping one in ───────────────────────────

    async def piece_options(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        piece: int,
        *,
        cheaper: bool = False,
        limit: int = 5,
    ) -> PieceOptions:
        """Products that could take the piece at card ordinal `piece`.

        They are committed as the search on screen, and the piece they are for
        is remembered (`room_swap`), so `swap(piece, option)` resolves option
        ordinals against exactly this list.
        """
        room = state.room_project
        if room is None or not room.bundle_items:
            return PieceOptions(state=state, code="no_room")
        if room.in_cart:
            return PieceOptions(state=state, code="room_in_cart")
        card = _card(room, piece)
        if card is None:
            return PieceOptions(state=state, code="piece_not_found")
        need_id = card.need_ids[0] if len(card.need_ids) == 1 else None
        need = next((n for n in room.design_needs if n.need_id == need_id), None)
        if need is None:
            return PieceOptions(state=state, code="piece_not_swappable")
        try:
            verified = await self._verify_bundle_products(room, context)
            if verified is None:
                return PieceOptions(state=state, code="locked_unavailable")
            capabilities = await self._capabilities.capabilities(context)
            current = next((p for p in verified if p.product_id == card.product_id), None)
            resolved = self._design_discovery.resolve_need(
                DesignCategoryNeed(
                    commerce_category=need.commerce_category,
                    commerce_subcategory=need.commerce_subcategory,
                    priority=need.priority,
                    quantity=need.quantity,
                    seating_capacity=need.seating_capacity,
                    semantic_intent=need.semantic_intent,
                ),
                _room_request(room, capabilities),
            )
            price: PriceConstraint | None = None
            if cheaper:
                if current is None or current.price_amount <= 0 or not current.price_unit.strip():
                    return PieceOptions(state=state, code="price_unusable")
                price = PriceConstraint.below(current.price_amount, current.price_unit)
            in_room = tuple(dict.fromkeys(line.product_id for line in room.bundle_items))
            request = resolved.request.model_copy(
                update={
                    "single_type": True,
                    "alongside_subcategories": (),
                    "exclude_product_ids": tuple(
                        dict.fromkeys((*need.rejected_product_ids, *in_room))
                    )[-MAX_EXCLUDED_PRODUCT_IDS:],
                    **({"price": price} if price is not None else {}),
                }
            )
            resolved = resolved.model_copy(update={"request": request})
            executed = await self._pipeline.execute(resolved, context, presentation_limit=limit)
            view = await self._current_view(room, context)
        except _CATALOG_FAILURES:
            logger.warning("room_engine_options_unavailable", store_id=context.store_id)
            return PieceOptions(state=state, code="catalog_unavailable")

        products = executed.grounding.products
        if not executed.presented_product_ids:
            return PieceOptions(
                state=state,
                code="no_options",
                facts={"piece": piece, "kind": _need_words(need), "cheaper": cheaper},
            )
        role = _need_words(need)[:MAX_ROLE_WORDS]
        staged = apply_update(
            state,
            AgentStateUpdate(
                active_search=ActiveSearchUpdate(
                    request=request,
                    semantics=resolved.semantics,
                    semantic_preferences=ReplaceItems(items=resolved.semantic_preferences),
                    semantic_intent=(
                        SetSemanticIntent(value=resolved.semantic_text)
                        if resolved.semantic_text
                        else ClearSemanticIntent()
                    ),
                )
            ),
        )
        staged = commit_search_results(staged, executed.presented_product_ids).model_copy(
            update={
                "room_swap": RoomSwapState(
                    bundle_ordinal=piece,
                    role=role,
                    bundle_revision=room.bundle_revision,
                    request=request,
                )
            }
        )
        return PieceOptions(
            state=staged,
            code="ok",
            facts=_options_facts(piece, role, card, view, products, room.budget),
            products=products,
        )

    async def swap(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        piece: int,
        option: int,
        *,
        allow_over_budget: bool,
    ) -> RoomOutcome:
        """Put option `option` of the last `piece_options(piece)` into the room.

        Every other piece keeps its product. Over budget and not allowed, the
        room is only proposed (`over_budget`); allowed, it is committed and the
        budget raised to the room's new total.
        """
        room = state.room_project
        if room is None or not room.bundle_items:
            return _refused(state, "no_room")
        if room.in_cart:
            return _refused(state, "room_in_cart")
        card = _card(room, piece)
        if card is None:
            return _refused(state, "piece_not_found")
        need_id = card.need_ids[0] if len(card.need_ids) == 1 else None
        need = next((n for n in room.design_needs if n.need_id == need_id), None)
        if need is None or need_id is None:
            return _refused(state, "piece_not_swappable")
        chosen = _option(state, piece, option)
        if chosen is None:
            return _refused(state, "options_expired")
        try:
            products = await self._hydration.hydrate_ids((chosen,), context)
            product = products[0] if products else None
            if product is None:
                return _refused(state, "option_unavailable")
            if product.commerce.category != need.commerce_category or (
                need.commerce_subcategory is not None
                and product.commerce.subcategory != need.commerce_subcategory
            ):
                return _refused(state, "option_not_for_piece")
            outcome = await self._pinned_outcome(
                state, context, released_need_id=need_id, forced={need_id: chosen}
            )
        except _CATALOG_FAILURES:
            return _refused(state, "catalog_unavailable")
        if isinstance(outcome, str):
            return _refused(state, outcome)
        if not isinstance(outcome, RoomBundle):
            return _unavailable(state, outcome)
        if not any(line.product.product_id == chosen for line in outcome.lines):
            return _refused(state, "option_unavailable")

        overage = _over_budget(outcome, room.budget)
        if overage is not None and not allow_over_budget:
            return self._proposal(state, outcome, "over_budget", room.budget, [])
        override = DesignNeedSearchOverride(
            need_id=need_id,
            exclude_product_ids=_with_rejection(room, need_id, card.product_id),
            forced_product_id=chosen,
        )
        committed = _commit_refinement(state, outcome, override)
        raised_from = None
        if overage is not None:
            committed, raised_from = _raise_budget(committed, outcome)
        committed = committed.model_copy(update={"room_swap": None, "swap_budget_offer": None})
        logger.info(
            "room_engine_swapped", store_id=context.store_id, over_budget=overage is not None
        )
        return self._committed(committed, outcome, budget_raised_from=raised_from)

    # ── removing a piece ────────────────────────────────────────────────────

    async def remove(
        self, state: AgentStateV1, context: RetailerContext, piece: int
    ) -> RoomOutcome:
        """Take one piece out. Every other piece stays the product it is."""
        room = state.room_project
        if room is None or not room.bundle_items:
            return _refused(state, "no_room")
        if room.in_cart:
            return _refused(state, "room_in_cart")
        card = _card(room, piece)
        if card is None:
            return _refused(state, "piece_not_found")
        if len(card.need_ids) > 1:
            return _refused(state, "piece_spans_several_roles")

        operations: tuple[BundleOperation, ...]
        if card.need_ids:
            # The role leaves the plan; the room's locks are named so they
            # survive the refinement (a lock in the removed role goes with it).
            operations = (
                RefineBundle(
                    removed_need_ids=card.need_ids,
                    preserved=tuple(
                        PreservedBundleLine(line_id=line.line_id)
                        for line in room.bundle_items
                        if line.status is BundleItemStatus.LOCKED
                    ),
                ),
            )
        else:
            operations = tuple(RemoveBundleLine(line_id=line_id) for line_id in card.line_ids)
        staged = apply_update(
            state, AgentStateUpdate(room_project=RoomProjectUpdate(bundle_operations=operations))
        )
        staged_room = staged.room_project
        assert staged_room is not None
        if not staged_room.design_needs:
            # Nothing left to choose: the room is what remains.
            return await self._rendered(staged, context, code="ok", committed=True)
        try:
            outcome = await self._pinned_outcome(
                staged, context, pinned_from=room, budget=staged_room.budget
            )
        except _CATALOG_FAILURES:
            return _refused(state, "catalog_unavailable")
        if isinstance(outcome, str):
            return _refused(state, outcome)
        if not isinstance(outcome, RoomBundle):
            return _unavailable(state, outcome)
        if outcome.status is BundleStatus.INFEASIBLE:
            return self._proposal(state, outcome, "infeasible", room.budget, [])
        committed = _commit_refinement(staged, outcome, None)
        logger.info("room_engine_removed", store_id=context.store_id)
        return self._committed(committed, outcome)

    # ── keeping, releasing, owning ──────────────────────────────────────────

    async def set_piece_status(
        self, state: AgentStateV1, context: RetailerContext, piece: int, status: PieceStatus
    ) -> RoomOutcome:
        """Lock or release a piece, or record that the customer already owns it.

        Locking and releasing change nothing about the room's contents. Owning
        a piece locks it and, with a budget, re-costs the rest of the room.
        """
        room = state.room_project
        if room is None or not room.bundle_items:
            return _refused(state, "no_room")
        if room.in_cart:
            return _refused(state, "room_in_cart")
        card = _card(room, piece)
        if card is None:
            return _refused(state, "piece_not_found")
        operations: list[BundleOperation] = []
        if status == "owned":
            operations.extend(
                SetBundleLineAcquisition(
                    line_id=line_id, acquisition=BundleAcquisition.ALREADY_OWNED
                )
                for line_id in card.line_ids
            )
        line_status = (
            BundleItemStatus.SUGGESTED if status == "unlocked" else BundleItemStatus.LOCKED
        )
        operations.extend(
            SetBundleLineStatus(line_id=line_id, status=line_status) for line_id in card.line_ids
        )
        updated = apply_update(
            state,
            AgentStateUpdate(room_project=RoomProjectUpdate(bundle_operations=tuple(operations))),
        )
        updated_room = updated.room_project
        assert updated_room is not None
        if status != "owned" or updated_room.budget is None or not updated_room.design_needs:
            return await self._rendered(updated, context, code="ok", committed=True)
        try:
            outcome = await self._reoptimise(updated, context)
        except _CATALOG_FAILURES:
            outcome = "catalog_unavailable"
        if isinstance(outcome, str) or not isinstance(outcome, RoomBundle):
            # What they own is true whatever the re-costing could do.
            rendered = await self._rendered(updated, context, code="ok", committed=True)
            return _with_facts(rendered, recosted=False)
        if outcome.status is BundleStatus.INFEASIBLE:
            rendered = await self._rendered(updated, context, code="ok", committed=True)
            return _with_facts(rendered, recosted=False)
        return self._committed(_commit_refinement(updated, outcome, None), outcome)

    # ── finishing touches ───────────────────────────────────────────────────

    async def pieces_it_could_take(
        self, state: AgentStateV1, context: RetailerContext
    ) -> tuple[str, ...]:
        """Registry piece keys the store stocks that the room does not hold yet."""
        room = state.room_project
        template = self._template(room.room_kind) if room is not None else None
        if room is None or template is None:
            return ()
        try:
            capabilities = await self._capabilities.capabilities(context)
            held = await self._hydration.hydrate_ids(
                tuple(dict.fromkeys(line.product_id for line in room.bundle_items)), context
            )
        except _CATALOG_FAILURES:
            return ()
        offers = finishing_offers(template, capabilities, _pieces_in(template, held))
        return tuple(offer.key for offer in offers)

    async def add_product(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        product_id: int,
        *,
        allow_over_budget: bool,
    ) -> RoomOutcome:
        """Add one product to the room as a finishing touch.

        Every other piece is pinned to the product it is, and the room is
        costed with no ceiling so nothing is dropped to make room for it.
        """
        room = state.room_project
        if room is None or not room.bundle_items:
            return _refused(state, "no_room")
        if room.in_cart:
            return _refused(state, "room_in_cart")
        if any(line.product_id == product_id for line in room.bundle_items):
            return _refused(state, "already_in_room")
        template = self._template(room.room_kind)
        try:
            found = await self._hydration.hydrate_ids(
                (product_id, *dict.fromkeys(line.product_id for line in room.bundle_items)),
                context,
            )
            product = next((p for p in found if p.product_id == product_id), None)
            if product is None:
                return _refused(state, "option_unavailable")
            held = [p for p in found if p.product_id != product_id]
            if template is not None:
                piece = piece_for(template, product.commerce.category, product.commerce.subcategory)
                capabilities = await self._capabilities.capabilities(context)
                offers = finishing_offers(template, capabilities, _pieces_in(template, held))
                takes = {offer.key for offer in offers}
                if piece is None or piece.key not in takes:
                    return _refused(state, "not_a_piece_it_could_take")
                need = DesignCategoryNeed(
                    commerce_category=piece.commerce_category,
                    commerce_subcategory=piece.commerce_subcategory,
                    priority=DesignPriority.OPTIONAL,
                    quantity=piece.quantity,
                )
            else:
                need = DesignCategoryNeed(
                    commerce_category=product.commerce.category,
                    commerce_subcategory=product.commerce.subcategory,
                    priority=DesignPriority.OPTIONAL,
                )
            plan = InteriorDesignResult(needs=(*_plan_from(room).needs, need))
            outcome = await self._pinned_outcome(state, context, added=(need, product_id))
        except _CATALOG_FAILURES:
            return _refused(state, "catalog_unavailable")
        if isinstance(outcome, str):
            return _refused(state, outcome)
        if not isinstance(outcome, RoomBundle):
            return _unavailable(state, outcome)
        if not any(line.product.product_id == product_id for line in outcome.lines):
            return _refused(state, "option_unavailable")
        overage = _over_budget(outcome, room.budget)
        if overage is not None and not allow_over_budget:
            return self._proposal(state, outcome, "over_budget", room.budget, [])
        committed = _commit_plan(state, plan, outcome)
        raised_from = None
        if overage is not None:
            committed, raised_from = _raise_budget(committed, outcome)
        logger.info("room_engine_added", store_id=context.store_id, over_budget=overage is not None)
        return self._committed(
            committed,
            outcome,
            budget_raised_from=raised_from,
            added=customer_words(need.commerce_subcategory or need.commerce_category),
        )

    # ── the room as it stands ───────────────────────────────────────────────

    async def current(self, state: AgentStateV1, context: RetailerContext) -> RoomOutcome | None:
        """The committed room, rendered from state and a fresh read, or None."""
        room = state.room_project
        if room is None or not room.bundle_items:
            return None
        try:
            return await self._rendered(state, context, code="ok", committed=False)
        except _CATALOG_FAILURES:
            return _refused(state, "catalog_unavailable")

    # ── internals: building ─────────────────────────────────────────────────

    def _template(self, kind: str | None) -> RoomTemplate | None:
        return self._rooms.template(kind) if self._rooms is not None else None

    def _preferences(
        self, colours: Sequence[str], styles: Sequence[str]
    ) -> tuple[SemanticPreference, ...]:
        """The room's taste as ranking preferences, never filters (CLAUDE.md 12.4)."""
        chosen: list[SemanticPreference] = []
        for family, values in ((AttributeFamily.COLOR, colours), (AttributeFamily.STYLE, styles)):
            for value in dict.fromkeys(values):
                canonical = (
                    self._attributes.canonical(family, value)
                    if self._attributes is not None
                    else value
                )
                if canonical is None:
                    continue
                chosen.append(
                    SemanticPreference(
                        family=family,
                        raw_value=value,
                        canonical_value=canonical,
                        strength=ConstraintStrength.PREFERRED,
                    )
                )
        return tuple(chosen)

    async def _lock_picks(
        self,
        staged: AgentStateV1,
        original: AgentStateV1,
        context: RetailerContext,
        template: RoomTemplate | None,
        *,
        only: tuple[int, ...] | None = None,
    ) -> AgentStateV1:
        """The customer's picks that belong in this room, as locked lines -
        of `only`, when given, those still in the cart."""
        picks = original.product_interaction.selected_product_ids
        if only is not None:
            picks = tuple(p for p in picks if p in only)
        room = staged.room_project
        if not picks or room is None:
            return staged
        products = await self._hydration.hydrate_ids(picks, context)
        belongs = {
            p.product_id
            for p in products
            if template is None
            or piece_for(template, p.commerce.category, p.commerce.subcategory) is not None
        }
        kept = [pid for pid in picks if pid in belongs][:MAX_ROOM_ANCHORS]
        existing = {line.product_id: line for line in room.bundle_items}
        operations: list[BundleOperation] = []
        for product_id in kept:
            line = existing.get(product_id)
            if line is not None:
                operations.append(
                    SetBundleLineStatus(line_id=line.line_id, status=BundleItemStatus.LOCKED)
                )
            else:
                operations.append(
                    AddBundleLine(
                        line=BundleLineSpec(
                            product_id=product_id,
                            quantity=1,
                            acquisition=BundleAcquisition.TO_BUY,
                            status=BundleItemStatus.LOCKED,
                        )
                    )
                )
        if not operations:
            return staged
        return apply_update(
            staged,
            AgentStateUpdate(room_project=RoomProjectUpdate(bundle_operations=tuple(operations))),
        )

    def _design_request(
        self,
        state: AgentStateV1,
        capabilities: RetailerCatalogCapabilities,
        locks: Sequence[tuple[BundleItemState, LockedBundleProduct]],
        *,
        revise: bool,
    ) -> InteriorDesignRequest:
        """The room and what is already in it. No retailer, no ids, no prices."""
        room = state.room_project
        assert room is not None
        quantities: dict[int, int] = {}
        for line, _ in locks:
            quantities[line.product_id] = quantities.get(line.product_id, 0) + line.quantity
        unique: dict[int, ProductCandidate] = {}
        for _, lock in locks:
            unique.setdefault(lock.product.product_id, lock.product)
        anchors = (
            project_anchors(
                list(unique.values()),
                dimensions=self._dimensions,
                locked_product_ids=list(unique),
                quantities=quantities,
            )
            if self._dimensions is not None
            else ()
        )
        current = project_current_plan(room) if revise else ()
        return InteriorDesignRequest(
            task=DesignTask.ROOM_PLAN,
            room_type=room.room_type,
            geometry=room.geometry,
            layout=room.layout,
            budget=room.budget,
            design_preferences=room.design_preferences,
            regular_seating_count=room.regular_seating_count,
            catalog_capabilities=capabilities,
            anchors=anchors,
            revision=DesignRevisionContext(current_needs=current) if current else None,
        )

    async def _composed_room(
        self,
        template: RoomTemplate,
        room: RoomProjectState,
        context: RetailerContext,
        request: InteriorDesignRequest,
        designed: InteriorDesignResult,
        capabilities: RetailerCatalogCapabilities,
        locked: tuple[LockedBundleProduct, ...],
    ) -> _Built:
        """The room built from exactly the pieces chosen (CLAUDE.md 10.3).

        Every piece but the seating is one need. The seating is sized to the
        head count: each way the store can seat them within budget is tried
        with the rest of the room, and the room that keeps every seat and the
        most of its pieces wins. Seats a locked piece provides count first.
        """
        keys = room.pieces if room.pieces is not None else default_pieces(template)
        others = InteriorDesignResult(
            guidance=designed.guidance,
            needs=tuple(composed_needs(template, keys, capabilities, designed)),
        )
        found = await self._design_discovery.discover(request, others, context)
        seating = seating_piece(template, keys, capabilities)
        budget = room.budget
        covered = self._locked_seats(locked)

        def optimise(seats: Sequence[tuple[DesignCategoryNeed, CandidatePoolResult]]) -> _Built:
            plan, discovery = _with_seating(others, found, seats)
            outcome = self._optimizer.optimize(
                BundleOptimizationRequest(
                    discovery=discovery,
                    budget=budget,
                    locked=locked,
                    fresh_needs=frozenset(range(len(seats))) if covered else frozenset(),
                )
            )
            return _Built(plan=plan, outcome=outcome)

        if seating is None:
            return optimise(())
        priority = PRIORITY_FOR_TIER[seating.tier]
        count = room.regular_seating_count
        if covered > 0 and (count is None or covered >= count):
            return optimise(())
        if count is not None:
            count -= covered
        arrangements: tuple[SeatingArrangement, ...] = ()
        if count is not None:
            arrangements = await self._seating_planner.arrangements(
                target_seats=count,
                budget_amount=budget.max_amount if budget else None,
                currency=budget.currency if budget else "",
                context=context,
                types=frozenset(seating.seating_types),
                requirements=_seating_wishes(room),
            )
        if not arrangements:
            need = DesignCategoryNeed(
                commerce_category=seating.commerce_category,
                commerce_subcategory=seating.seating_types[0],
                priority=priority,
                seating_capacity=(
                    SeatingCapacityConstraint(min_capacity=count) if count is not None else None
                ),
            )
            pool = await self._design_discovery.discover(
                request, InteriorDesignResult(needs=(need,)), context
            )
            entry = pool.needs[0] if pool.needs else None
            return optimise(
                ((need, entry.pool),) if entry is not None and entry.pool is not None else ()
            )

        pools: dict[int, CandidatePoolResult] = {}
        best: tuple[tuple[int, ...], _Built] | None = None
        for position, arrangement in enumerate(arrangements):
            seats: list[tuple[DesignCategoryNeed, CandidatePoolResult]] = []
            for line in arrangement.lines:
                if line.product_id not in pools:
                    pools[line.product_id] = await self._pipeline.execute_forced_pool(
                        line.product_id, context
                    )
                seats.append(
                    (
                        DesignCategoryNeed(
                            commerce_category=seating.commerce_category,
                            commerce_subcategory=line.commerce_subcategory,
                            priority=priority,
                            quantity=line.quantity,
                            seating_capacity=SeatingCapacityConstraint(
                                min_capacity=line.seats_each, max_capacity=line.seats_each
                            ),
                        ),
                        pools[line.product_id],
                    )
                )
            tried = optimise(seats)
            score = (*_shortfall(tried.outcome, len(seats)), position)
            if best is None or score < best[0]:
                best = (score, tried)
        assert best is not None
        return best[1]

    def _locked_seats(self, locked: Sequence[LockedBundleProduct]) -> int:
        """Seats the locked pieces already provide toward the head count."""
        if self._seating is None:
            return 0
        total = 0
        for lock in locked:
            commerce = lock.product.commerce
            if commerce.category != SEATING_CATEGORY:
                continue
            seats = commerce.seating_capacity or self._seating.implied_capacity(
                commerce.subcategory
            )
            if seats is not None:
                total += seats * lock.quantity
        return total

    def _room_seats(self, bundle: RoomBundle | None) -> int | None:
        """How many the room's seating seats, or None when any count is unknown."""
        if bundle is None or self._seating is None:
            return None
        total = 0
        for line in bundle.lines:
            commerce = line.product.commerce
            if commerce.category != SEATING_CATEGORY:
                continue
            seats = commerce.seating_capacity or self._seating.implied_capacity(
                commerce.subcategory
            )
            if seats is None:
                return None
            total += seats * line.quantity
        return total or None

    # ── internals: re-costing a room that exists ────────────────────────────

    async def _reoptimise(
        self, state: AgentStateV1, context: RetailerContext
    ) -> BundleOptimizationOutcome | str:
        """Every role discovered afresh under the existing plan, locks kept."""
        room = state.room_project
        assert room is not None
        locks = await self._verify_locks(state, context)
        if locks is None:
            return "locked_unavailable"
        capabilities = await self._capabilities.capabilities(context)
        discovery = await self._design_discovery.discover(
            _room_request(room, capabilities), _plan_from(room), context
        )
        return self._optimizer.optimize(
            BundleOptimizationRequest(
                discovery=discovery,
                budget=room.budget,
                locked=tuple(product for _, product in locks),
            )
        )

    async def _pinned_outcome(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        *,
        released_need_id: int | None = None,
        forced: Mapping[int, int] | None = None,
        pinned_from: RoomProjectState | None = None,
        budget: PriceConstraint | None = None,
        added: tuple[DesignCategoryNeed, int] | None = None,
    ) -> BundleOptimizationOutcome | str:
        """The room costed with every role fixed to a product.

        Each unlocked role is pinned to the product it holds (in `pinned_from`,
        or the room itself), `forced` overrides that per role, and `added` is
        one more role fixed to one product. Locks stay locks; a role covered by
        a lock, or never filled, is not searched. Nothing is re-selected, so a
        dear change is never balanced by quietly dropping a piece.
        """
        room = state.room_project
        assert room is not None
        locks = await self._verify_locks(state, context, released_need_id)
        if locks is None:
            return "locked_unavailable"
        source = pinned_from or room
        current: dict[int, int] = {}
        for line in source.bundle_items:
            if line.need_id is not None and line.status is not BundleItemStatus.LOCKED:
                current.setdefault(line.need_id, line.product_id)
        if released_need_id is not None:
            # The released role's own lock is not a lock for this costing.
            for line in source.bundle_items:
                if line.need_id == released_need_id:
                    current.setdefault(line.need_id, line.product_id)
        current.update(forced or {})
        plan = _plan_from(room)
        entries: list[DesignNeedCandidates] = []
        for index, need in enumerate(plan.needs):
            product_id = current.get(room.design_needs[index].need_id)
            entries.append(await self._forced_entry(index, need, product_id, context))
        if added is not None:
            entries.append(await self._forced_entry(len(plan.needs), added[0], added[1], context))
        return self._optimizer.optimize(
            BundleOptimizationRequest(
                discovery=DesignDiscoveryResult(needs=tuple(entries)),
                budget=budget,
                locked=tuple(product for _, product in locks),
            )
        )

    async def _forced_entry(
        self,
        index: int,
        need: DesignCategoryNeed,
        product_id: int | None,
        context: RetailerContext,
    ) -> DesignNeedCandidates:
        if product_id is None:
            return DesignNeedCandidates(
                need_index=index, need=need, skipped=DesignNeedSkipReason.RETAILER_CANNOT_SUPPLY
            )
        pool = await self._pipeline.execute_forced_pool(product_id, context)
        return DesignNeedCandidates(need_index=index, need=need, pool=pool)

    async def _verify_locks(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        released_need_id: int | None = None,
    ) -> list[tuple[BundleItemState, LockedBundleProduct]] | None:
        """Every locked line with its product re-read, or None if one is gone.

        A lock that no longer resolves stops the operation: it cannot be
        dropped, substituted or answered from memory (CLAUDE.md 10).
        """
        room = state.room_project
        lines = [
            line
            for line in (room.bundle_items if room else ())
            if line.status is BundleItemStatus.LOCKED
            and (released_need_id is None or line.need_id != released_need_id)
        ]
        if not lines:
            return []
        wanted = tuple(dict.fromkeys(line.product_id for line in lines))
        products = await self._hydration.hydrate_ids(wanted, context)
        by_id = {product.product_id: product for product in products}
        if len(by_id) != len(wanted):
            logger.warning(
                "room_engine_locked_product_unavailable",
                store_id=context.store_id,
                expected=len(wanted),
                verified=len(by_id),
            )
            return None
        return [
            (
                line,
                LockedBundleProduct(
                    product=by_id[line.product_id],
                    acquisition=line.acquisition,
                    quantity=line.quantity,
                ),
            )
            for line in lines
        ]

    async def _verify_bundle_products(
        self, room: RoomProjectState, context: RetailerContext
    ) -> list[ProductCandidate] | None:
        """Fresh facts for the room's products; None when a locked one is gone."""
        wanted = tuple(dict.fromkeys(line.product_id for line in room.bundle_items))
        products = await self._hydration.hydrate_ids(wanted, context)
        found = {product.product_id for product in products}
        if any(
            line.status is BundleItemStatus.LOCKED and line.product_id not in found
            for line in room.bundle_items
        ):
            return None
        return list(products)

    async def _current_view(
        self, room: RoomProjectState, context: RetailerContext
    ) -> GroundedBundlePresentation | None:
        products = await self._hydration.hydrate_ids(
            tuple(dict.fromkeys(line.product_id for line in room.bundle_items)), context
        )
        by_id = {p.product_id for p in products}
        if any(line.product_id not in by_id for line in room.bundle_items):
            return None
        return build_state_bundle_presentation(room, products)

    # ── internals: outcomes ─────────────────────────────────────────────────

    def _committed(
        self,
        state: AgentStateV1,
        outcome: RoomBundle,
        *,
        guidance: Sequence[str] = (),
        budget_raised_from: Decimal | None = None,
        added: str | None = None,
    ) -> RoomOutcome:
        room = state.room_project
        view = present_room_bundle(outcome, room)
        facts = _facts(view, outcome, room, guidance)
        seats = self._room_seats(outcome)
        if seats is not None:
            facts["seats"] = seats
        if budget_raised_from is not None:
            facts["budget_raised_from"] = budget_raised_from
        if added is not None:
            facts["added"] = added
        return RoomOutcome(
            state=state, bundle=outcome, code="ok", committed=True, facts=facts, view=view
        )

    def _proposal(
        self,
        state: AgentStateV1,
        outcome: RoomBundle,
        code: str,
        budget: PriceConstraint | None,
        guidance: Sequence[str],
    ) -> RoomOutcome:
        """A room chosen but not committed: infeasible, or over budget."""
        view = present_room_bundle(outcome, None, budget=budget)
        facts = _facts(view, outcome, state.room_project, guidance)
        overage = _over_budget(outcome, budget)
        if overage is not None:
            facts["new_total"] = outcome.new_spend_total
            facts["overage"] = overage
        facts["proposal_only"] = True
        return RoomOutcome(
            state=state, bundle=outcome, code=code, committed=False, facts=facts, view=view
        )

    async def _rendered(
        self, state: AgentStateV1, context: RetailerContext, *, code: str, committed: bool
    ) -> RoomOutcome:
        """The room in state, rendered from a fresh read (no optimisation)."""
        room = state.room_project
        view = None
        if room is not None and room.bundle_items:
            view = await self._current_view(room, context)
            if view is None:
                # A piece the catalog cannot return now: the room is still
                # theirs, but it is not shown with a gap in it.
                return RoomOutcome(
                    state=state,
                    bundle=None,
                    code="piece_unavailable",
                    committed=committed,
                    facts={"code": "piece_unavailable"},
                )
        facts = _facts(view, None, room, ())
        return RoomOutcome(
            state=state, bundle=None, code=code, committed=committed, facts=facts, view=view
        )


# ── module helpers ───────────────────────────────────────────────────────────


def _refused(state: AgentStateV1, code: str) -> RoomOutcome:
    return RoomOutcome(state=state, bundle=None, code=code, committed=False, facts={"code": code})


def _unavailable(state: AgentStateV1, outcome: BundleUnavailable) -> RoomOutcome:
    """The optimiser could not do the arithmetic: a budget form or currency."""
    return RoomOutcome(
        state=state,
        bundle=None,
        code="no_room",
        committed=False,
        facts={"code": "no_room", "reason": str(outcome.reason)},
    )


def _with_facts(outcome: RoomOutcome, **extra: object) -> RoomOutcome:
    return RoomOutcome(
        state=outcome.state,
        bundle=outcome.bundle,
        code=outcome.code,
        committed=outcome.committed,
        facts={**outcome.facts, **extra},
        view=outcome.view,
    )


def _same_room(room: RoomProjectState, kind: str | None, room_type: str) -> bool:
    if kind is not None and room.room_kind is not None:
        return kind == room.room_kind
    return (room.room_type or "").casefold().strip() == room_type.casefold().strip()


def _values(room: RoomProjectState, family: AttributeFamily) -> tuple[str, ...]:
    return tuple(
        p.canonical_value
        for p in room.design_preferences
        if p.family is family and p.canonical_value is not None
    )


def _card(room: RoomProjectState, piece: int) -> BundleCardGroup | None:
    cards = group_bundle_cards(room.bundle_items)
    return cards[piece - 1] if 1 <= piece <= len(cards) else None


def _paged(request: ProductSearchRequest) -> ProductSearchRequest:
    """The request without the products paged past: the next page of a piece's
    options is still that piece's options."""
    return request.model_copy(update={"exclude_product_ids": ()})


def _option(state: AgentStateV1, piece: int, option: int) -> int | None:
    """The product behind option `option`, while the options shown for this
    piece are still the list on screen and the room has not moved."""
    swap = state.room_swap
    room = state.room_project
    search = state.active_search
    interaction = state.product_interaction
    if (
        swap is None
        or room is None
        or search is None
        or swap.bundle_ordinal != piece
        or swap.bundle_revision != room.bundle_revision
        or _paged(search.request) != _paged(swap.request)
        or interaction.presented_search_revision != search.revision
    ):
        return None
    shown = interaction.presented_product_ids
    return shown[option - 1] if 1 <= option <= len(shown) else None


def _need_words(need: DesignCategoryNeed | RoomDesignNeedState) -> str:
    return customer_words(need.commerce_subcategory or need.commerce_category)


def _room_request(
    room: RoomProjectState, capabilities: RetailerCatalogCapabilities
) -> InteriorDesignRequest:
    return InteriorDesignRequest(
        task=DesignTask.ROOM_PLAN,
        room_type=room.room_type,
        geometry=room.geometry,
        budget=room.budget,
        design_preferences=room.design_preferences,
        catalog_capabilities=capabilities,
    )


def _plan_from(room: RoomProjectState) -> InteriorDesignResult:
    """The existing plan, in the shape discovery consumes; no specialist asked."""
    return InteriorDesignResult(
        needs=tuple(
            DesignCategoryNeed(
                commerce_category=need.commerce_category,
                commerce_subcategory=need.commerce_subcategory,
                priority=need.priority,
                quantity=need.quantity,
                seating_capacity=need.seating_capacity,
                semantic_intent=need.semantic_intent,
            )
            for need in room.design_needs
        )
    )


def _pieces_in(template: RoomTemplate, products: Sequence[ProductCandidate]) -> frozenset[str]:
    return frozenset(
        piece.key
        for product in products
        if (piece := piece_for(template, product.commerce.category, product.commerce.subcategory))
        is not None
    )


def _with_seating(
    others: InteriorDesignResult,
    found: DesignDiscoveryResult,
    seats: Sequence[tuple[DesignCategoryNeed, CandidatePoolResult]],
) -> tuple[InteriorDesignResult, DesignDiscoveryResult]:
    """The plan and candidates with the seating first, the rest shifted on."""
    entries = [
        DesignNeedCandidates(need_index=position, need=need, pool=pool)
        for position, (need, pool) in enumerate(seats)
    ]
    entries.extend(
        entry.model_copy(update={"need_index": entry.need_index + len(seats)})
        for entry in found.needs
    )
    plan = InteriorDesignResult(
        guidance=others.guidance, needs=(*(need for need, _ in seats), *others.needs)
    )
    return plan, DesignDiscoveryResult(needs=tuple(entries))


_UNBUILT = 10_000


def _shortfall(outcome: BundleOptimizationOutcome, seat_needs: int) -> tuple[int, int, int, int]:
    """Seats missed first, then essential, recommended and optional pieces."""
    if not isinstance(outcome, RoomBundle) or outcome.status is BundleStatus.INFEASIBLE:
        return (_UNBUILT, _UNBUILT, _UNBUILT, _UNBUILT)
    unmet = dict.fromkeys(DesignPriority, 0)
    seats = 0
    for entry in outcome.unmet:
        unmet[entry.priority] += 1
        seats += entry.need_index < seat_needs
    return (
        seats,
        unmet[DesignPriority.REQUIRED],
        unmet[DesignPriority.RECOMMENDED],
        unmet[DesignPriority.OPTIONAL],
    )


def _seating_wishes(room: RoomProjectState) -> SeatingRequirements:
    """The room's colours and styles as wishes each seat is ranked by."""
    return SeatingRequirements(
        wished_colors=tuple(dict.fromkeys(_values(room, AttributeFamily.COLOR))),
        wished_styles=tuple(dict.fromkeys(_values(room, AttributeFamily.STYLE))),
    )


def _with_rejection(room: RoomProjectState, need_id: int, product_id: int) -> tuple[int, ...]:
    current = next(
        (need.rejected_product_ids for need in room.design_needs if need.need_id == need_id), ()
    )
    return tuple(dict.fromkeys((*current, product_id)))[-MAX_EXCLUDED_PRODUCT_IDS:]


def _over_budget(bundle: RoomBundle, budget: PriceConstraint | None) -> Decimal | None:
    """By how much the room's new spend breaks a plain ceiling, or None."""
    if budget is None or budget.max_amount is None or budget.min_amount is not None:
        return None
    total = bundle.new_spend_total
    if total is None or bundle.currency != budget.currency:
        return None
    return total - budget.max_amount if total > budget.max_amount else None


def _raise_budget(state: AgentStateV1, outcome: RoomBundle) -> tuple[AgentStateV1, Decimal | None]:
    """The ceiling lifted to what the room now costs; the old ceiling returned."""
    room = state.room_project
    if (
        room is None
        or room.budget is None
        or outcome.new_spend_total is None
        or outcome.currency is None
    ):
        return state, None
    original = room.budget.max_amount
    raised = PriceConstraint(currency=outcome.currency, max_amount=outcome.new_spend_total)
    return (
        apply_update(state, AgentStateUpdate(room_project=RoomProjectUpdate(budget=raised))),
        original,
    )


def _commit_plan(
    state: AgentStateV1, plan: InteriorDesignResult, outcome: RoomBundle
) -> AgentStateV1:
    """A new plan and the room chosen from it, in one operation; locks kept."""
    room = state.room_project
    locked_ids = [
        line.line_id
        for line in (room.bundle_items if room else ())
        if line.status is BundleItemStatus.LOCKED
    ]
    return apply_update(
        state,
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                bundle_operations=(
                    ReplaceDesignPlan(
                        needs=tuple(
                            DesignNeedSpec(
                                commerce_category=need.commerce_category,
                                commerce_subcategory=need.commerce_subcategory,
                                priority=need.priority,
                                quantity=need.quantity,
                                seating_capacity=need.seating_capacity,
                                semantic_intent=need.semantic_intent,
                            )
                            for need in plan.needs
                        ),
                        preserved=tuple(
                            PreservedBundleLine(line_id=line_id) for line_id in locked_ids
                        ),
                        added=tuple(
                            PlannedBundleLineSpec(
                                product_id=line.product.product_id,
                                quantity=line.quantity,
                                acquisition=BundleAcquisition.TO_BUY,
                                status=BundleItemStatus.SUGGESTED,
                                need_index=line.need_index,
                            )
                            for line in outcome.lines
                            if not line.locked
                        ),
                    ),
                )
            )
        ),
    )


def _commit_refinement(
    state: AgentStateV1, outcome: RoomBundle, override: DesignNeedSearchOverride | None
) -> AgentStateV1:
    """The refined room under the same plan identity, locks preserved."""
    room = state.room_project
    assert room is not None
    by_position = {position: need.need_id for position, need in enumerate(room.design_needs)}
    refinements = (
        (
            DesignNeedRefinement(
                need_id=override.need_id, rejected_product_ids=override.exclude_product_ids
            ),
        )
        if override is not None
        else ()
    )
    return apply_update(
        state,
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                bundle_operations=(
                    RefineBundle(
                        refinements=refinements,
                        preserved=tuple(
                            PreservedBundleLine(line_id=line.line_id)
                            for line in room.bundle_items
                            if line.status is BundleItemStatus.LOCKED
                            and (override is None or line.need_id != override.need_id)
                        ),
                        added=tuple(
                            BundleLineSpec(
                                product_id=line.product.product_id,
                                quantity=line.quantity,
                                acquisition=BundleAcquisition.TO_BUY,
                                status=BundleItemStatus.SUGGESTED,
                                need_id=(
                                    None
                                    if line.need_index is None
                                    else by_position.get(line.need_index)
                                ),
                            )
                            for line in outcome.lines
                            if not line.locked
                        ),
                    ),
                )
            )
        ),
    )


def _facts(
    view: GroundedBundlePresentation | None,
    bundle: RoomBundle | None,
    room: RoomProjectState | None,
    guidance: Sequence[str],
) -> dict[str, object]:
    """What the model is told about the room: id-free, money from code."""
    facts: dict[str, object] = {}
    if room is not None:
        kind = customer_words(room.room_kind) if room.room_kind else None
        facts["room"] = room.room_type or kind
    if view is not None:
        facts["status"] = str(view.status)
        facts["pieces"] = [
            {
                "piece": item.grounding_ref,
                "kind": customer_words(
                    item.commerce.subcategory or item.commerce.category or "piece"
                ),
                "name": item.name_english,
                "price": item.unit_price,
                "currency": item.price_unit,
                "quantity": item.quantity,
                "line_total": item.new_spend_line_total,
                "locked": item.locked,
                "owned": item.acquisition is BundleAcquisition.ALREADY_OWNED,
            }
            for item in view.items
        ]
        totals = view.totals
        facts["total"] = totals.new_spend_total
        facts["currency"] = totals.currency
        facts["budget"] = totals.budget_max_amount
        facts["within_budget"] = totals.within_budget
    if bundle is not None:
        facts["missing"] = [
            {
                "kind": customer_words(
                    entry.commerce_subcategory or entry.commerce_category or "piece"
                ),
                "reason": str(entry.reason),
                **({"lowest_price": entry.cheapest_price} if entry.cheapest_price else {}),
            }
            for entry in bundle.unmet
        ]
    if guidance:
        facts["guidance"] = list(guidance)
    return facts


def _options_facts(
    piece: int,
    role: str,
    card: BundleCardGroup,
    view: GroundedBundlePresentation | None,
    products: Sequence[GroundedProduct],
    budget: PriceConstraint | None,
) -> dict[str, object]:
    """Each option with what the room would cost if it were swapped in."""
    total = view.totals.new_spend_total if view is not None else None
    currency = view.totals.currency if view is not None else None
    item = view.items[piece - 1] if view is not None and len(view.items) >= piece else None
    current_line = (item.new_spend_line_total or Decimal(0)) if item is not None else None
    options: list[dict[str, object]] = []
    for number, product in enumerate(products, start=1):
        option: dict[str, object] = {
            "option": number,
            "name": product.name_english,
            "price": product.price_amount,
            "currency": product.price_unit,
            "colour": product.main_color,
            "styles": list(product.styles),
        }
        if total is not None and current_line is not None and product.price_unit == currency:
            new_total = total - current_line + product.price_amount * card.quantity
            option["total_if_swapped"] = new_total
            ceiling = budget.max_amount if budget is not None else None
            if (
                ceiling is not None
                and budget is not None
                and budget.min_amount is None
                and budget.currency == currency
                and new_total > ceiling
            ):
                option["over_budget_by"] = new_total - ceiling
        options.append(option)
    return {"piece": piece, "kind": role, "quantity": card.quantity, "options": options}
