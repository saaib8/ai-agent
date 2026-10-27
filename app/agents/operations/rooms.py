"""The room engine: every whole-room operation Nora's tools call.

Deterministic all the way down. Nora designs the room - its pieces, their
types, how many, their look, size, share of the budget and wall - and this
module decides nothing about it or about the catalog: it finds real products
for each piece through Product Discovery, chooses the room with the bundle
optimiser, and commits the result through the state reducers - nowhere else.

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

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agents.facts import listed_size
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
    MAX_ROOM_NOTE_CHARS,
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
    DesignTask,
    InteriorDesignRequest,
    InteriorDesignResult,
    PieceDesign,
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
)
from app.schemas.geometry import RoomGeometry
from app.schemas.grounding import GroundedProduct
from app.schemas.product import ProductCandidate
from app.schemas.query import ConstraintStrength, ResolvedSearch, SemanticPreference
from app.schemas.relaxation import StopReason
from app.schemas.resolution import CandidatePoolResult, ProductSearchExecutionResult
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerContext
from app.services.agent_state import apply_update, commit_search_results
from app.services.bundle_cards import BundleCardGroup, group_bundle_cards
from app.services.bundle_presentation import (
    build_state_bundle_presentation,
    present_room_bundle,
)
from app.services.room_cart import finished, room_words
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.rooms import RoomPieces
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

    async def execute_forced_pools(
        self, product_ids: Sequence[int], context: RetailerContext
    ) -> dict[int, CandidatePoolResult]: ...


class _Capabilities(Protocol):
    async def capabilities(self, context: RetailerContext) -> RetailerCatalogCapabilities: ...


class _Hydration(Protocol):
    async def hydrate_ids(
        self, product_ids: Sequence[int], context: RetailerContext
    ) -> Sequence[ProductCandidate]: ...


# ── contracts ────────────────────────────────────────────────────────────────


class RoomDesignInputs(BaseModel):
    """A room as Nora designed it, already checked (`app/services/room_design.py`).

    Approved values only. The plan is hers - every piece, its types, quantity,
    look, size, share and wall; code finds the products and does the sums."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    room_type: str = Field(min_length=1, max_length=60)
    """The room in words: "living room", "home office"."""
    room_kind: str | None = Field(default=None, max_length=40)
    """A room the guide knows (`living_room`), for the brief only."""
    same_room: bool
    """Her word on whether this is the room on record (designed again, keeping
    its locked pieces) or a new one."""
    budget: PriceConstraint | None = None
    geometry: RoomGeometry | None = None
    head_count: int | None = Field(default=None, ge=1, le=MAX_REGULAR_SEATING_COUNT)
    palette: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    plan: InteriorDesignResult
    pick_places: Mapping[int, int] = Field(default_factory=dict)
    """Need position -> the cart place of the pick that is that piece."""
    around_picks: bool = False
    """Lock every pick that is one of the room's pieces."""
    replace_room: bool = False
    """They said to start another room in place of the one being designed."""
    layout_idea: str | None = Field(default=None, max_length=MAX_ROOM_NOTE_CHARS)
    """How she arranged it, in her words; checked to state no figure."""


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


@dataclass(frozen=True, slots=True)
class PieceRevision:
    """One room piece, by its card's place, as Nora revised its design."""

    piece: int
    design: PieceDesign
    feel: str | None = None


class RoomEngine:
    """Plans, revises and edits the customer's room against live catalog facts."""

    def __init__(
        self,
        design_discovery: _DesignDiscovery,
        optimizer: _Optimizer,
        pipeline: _Pipeline,
        capabilities: _Capabilities,
        hydration: _Hydration,
        rooms: RoomPieces | None,
        seating: SeatingSemantics | None,
    ) -> None:
        self._design_discovery = design_discovery
        self._optimizer = optimizer
        self._pipeline = pipeline
        self._capabilities = capabilities
        self._hydration = hydration
        self._rooms = rooms
        """The room guide - read only to say what a finished room could still
        take (finishing touches)."""
        self._seating = seating

    # ── planning ────────────────────────────────────────────────────────────

    async def design(
        self, state: AgentStateV1, context: RetailerContext, inputs: RoomDesignInputs
    ) -> RoomOutcome:
        """Build the room Nora designed and commit it.

        The same room keeps its locked pieces and what this call does not
        restate (its budget, head count, palette); a new room starts fresh,
        finishing the one in the cart first so its lines keep its name.
        """
        current = state.room_project
        # A room only noted (its size, walls, head count) and not yet built is
        # the room being designed, whatever it was called.
        noted_only = current is not None and not current.bundle_items and not current.in_cart
        same = current is not None and (inputs.same_room or noted_only)
        if same and current is not None and current.in_cart:
            return _refused(state, "room_in_cart")
        if (
            not same
            and current is not None
            and current.bundle_items
            and not current.in_cart
            and not inputs.replace_room
        ):
            # A designed room is never given up for another unasked.
            return RoomOutcome(
                state=state,
                bundle=None,
                code="another_room_in_progress",
                committed=False,
                facts={"code": "another_room_in_progress", "room_on_record": room_words(current)},
            )
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
        staged = apply_update(
            base,
            AgentStateUpdate(
                room_project=RoomProjectUpdate(
                    room_type=current.room_type or inputs.room_type
                    if same and current is not None
                    else inputs.room_type,
                    room_kind=(
                        (current.room_kind or inputs.room_kind)
                        if same and current is not None
                        else inputs.room_kind
                    ),
                    budget=inputs.budget,
                    clear_budget=inputs.budget is None and not same,
                    geometry=inputs.geometry,
                    regular_seating_count=inputs.head_count,
                    clear_regular_seating_count=inputs.head_count is None and not same,
                    palette=inputs.palette if inputs.palette or not same else None,
                    palette_styles=inputs.styles if inputs.styles or not same else None,
                    layout_idea=inputs.layout_idea,
                )
            ),
        )
        picks = state.product_interaction.selected_product_ids
        by_need = {
            index: picks[place - 1]
            for index, place in inputs.pick_places.items()
            if 1 <= place <= len(picks)
        }
        if len(by_need) != len(inputs.pick_places):
            return _refused(state, "pick_not_in_cart")
        named = tuple(by_need.values())
        plan = inputs.plan
        if by_need:
            try:
                held = {p.product_id: p for p in await self._hydration.hydrate_ids(named, context)}
            except _CATALOG_FAILURES:
                return _refused(state, "catalog_unavailable")
            needs = list(plan.needs)
            for index, product_id in by_need.items():
                product = held.get(product_id)
                if product is None:
                    return _refused(state, "pick_unavailable")
                if not _is_piece(product, needs[index]):
                    return _with_facts(
                        _refused(state, "pick_not_this_piece"),
                        piece=index + 1,
                        pick_is=customer_words(
                            product.commerce.subcategory or product.commerce.category or "piece"
                        ),
                    )
                # The pick is the piece: its own seats stand, never a filter it
                # could fail for want of a recorded count.
                needs[index] = needs[index].model_copy(update={"seating_capacity": None})
            plan = plan.model_copy(update={"needs": tuple(needs)})
        if named or inputs.around_picks:
            try:
                staged = await self._lock_picks(
                    staged,
                    state,
                    context,
                    plan,
                    only=None if inputs.around_picks else named,
                    must=named,
                )
            except _CATALOG_FAILURES:
                logger.warning("room_engine_picks_unreadable", store_id=context.store_id)
                return _refused(state, "catalog_unavailable")
        return await self._build_and_commit(state, staged, context, plan)

    async def _build_and_commit(
        self,
        original: AgentStateV1,
        staged: AgentStateV1,
        context: RetailerContext,
        plan: InteriorDesignResult,
    ) -> RoomOutcome:
        try:
            locks = await self._verify_locks(staged, context)
        except _CATALOG_FAILURES:
            return _refused(original, "catalog_unavailable")
        if locks is None:
            return _refused(original, "locked_unavailable")
        room = staged.room_project
        assert room is not None, "a room was staged"
        try:
            capabilities = await self._capabilities.capabilities(context)
            discovery = await self._design_discovery.discover(
                _room_request(room, capabilities), plan, context
            )
        except _CATALOG_FAILURES:
            logger.warning("room_engine_discovery_unavailable", store_id=context.store_id)
            return _refused(original, "catalog_unavailable")
        outcome = self._optimizer.optimize(
            BundleOptimizationRequest(
                discovery=discovery,
                budget=room.budget,
                locked=tuple(product for _, product in locks),
            )
        )
        if not isinstance(outcome, RoomBundle):
            return _unavailable(original, outcome)
        if outcome.status is BundleStatus.INFEASIBLE:
            return self._proposal(original, outcome, "infeasible", room.budget, [])
        committed = _commit_plan(staged, plan, outcome)
        logger.info(
            "room_engine_designed",
            store_id=context.store_id,
            need_count=len(plan.needs),
            line_count=len(outcome.lines),
            status=str(outcome.status),
        )
        built = self._committed(committed, outcome)
        return built

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
                    design=need.design,
                ),
                _room_request(room, capabilities),
            )
            price: PriceConstraint | None = None
            if cheaper:
                if current is None or current.price_amount <= 0 or not current.price_unit.strip():
                    return PieceOptions(state=state, code="price_unusable")
                price = PriceConstraint.below(current.price_amount, current.price_unit)
            in_room = tuple(dict.fromkeys(line.product_id for line in room.bundle_items))
            # A piece's options are every type she said may fill it, and
            # only those (CLAUDE.md 10.1).
            alternatives = need.design.alternatives if need.design else ()
            request = resolved.request.model_copy(
                update={
                    "single_type": not alternatives,
                    "alongside_subcategories": alternatives,
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
            if not _is_piece(product, _plan_from(room).needs[room.design_needs.index(need)]):
                return _refused(state, "option_not_for_piece")
            outcome = await self._pinned_outcome(
                state, context, released=(need_id,), forced={need_id: chosen}
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

    # ── revising one piece ──────────────────────────────────────────────────

    async def revise_piece(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        piece: int,
        design: PieceDesign,
        *,
        feel: str | None = None,
    ) -> RoomOutcome:
        """Find one piece again as Nora revised it - its look, size, share or
        wall - with every other piece pinned to the product it is, within the
        room's budget. Nothing else is re-chosen."""
        return await self.revise_pieces(state, context, (PieceRevision(piece, design, feel),))

    async def revise_pieces(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        revisions: Sequence[PieceRevision],
    ) -> RoomOutcome:
        """Find several pieces again as Nora revised them, as one change: the
        revised pieces searched together, every other piece pinned, the room
        costed once within its budget and committed once - all of them or
        none, never half a correction."""
        room = state.room_project
        if room is None or not room.bundle_items:
            return _refused(state, "no_room")
        if room.in_cart:
            return _refused(state, "room_in_cart")
        if not revisions:
            return _refused(state, "nothing_to_change")
        plan = _plan_from(room)
        searched: dict[int, DesignCategoryNeed] = {}
        refinements: list[DesignNeedRefinement] = []
        for revision in revisions:
            card = _card(room, revision.piece)
            if card is None:
                return _refused(state, "piece_not_found")
            need_id = card.need_ids[0] if len(card.need_ids) == 1 else None
            stored = next((n for n in room.design_needs if n.need_id == need_id), None)
            if stored is None or need_id is None:
                return _refused(state, "piece_not_swappable")
            if need_id in searched:
                return _refused(state, "piece_revised_twice")
            current = plan.needs[room.design_needs.index(stored)]
            searched[need_id] = current.model_copy(
                update={
                    "design": revision.design,
                    **({"semantic_intent": revision.feel} if revision.feel else {}),
                }
            )
            refinements.append(
                DesignNeedRefinement(
                    need_id=need_id, design=revision.design, semantic_intent=revision.feel
                )
            )
        try:
            outcome = await self._pinned_outcome(
                state,
                context,
                released=tuple(searched),
                searched=searched,
                budget=room.budget,
            )
        except _CATALOG_FAILURES:
            return _refused(state, "catalog_unavailable")
        if isinstance(outcome, str):
            return _refused(state, outcome)
        if not isinstance(outcome, RoomBundle):
            return _unavailable(state, outcome)
        if outcome.status is BundleStatus.INFEASIBLE:
            return self._proposal(state, outcome, "infeasible", room.budget, [])
        committed = _commit_refinement(state, outcome, None, revised=refinements)
        logger.info(
            "room_engine_piece_revised", store_id=context.store_id, revised=len(refinements)
        )
        return self._committed(committed, outcome)

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

        Nothing about the room's contents changes. Owning a piece locks it and
        takes it off the bill; the room is re-costed with the rest as it is.
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
        # Owning a piece takes it off the bill; every other piece stays the
        # product it is - the room is re-costed, never quietly re-chosen.
        return await self._rendered(updated, context, code="ok", committed=True)

    # ── finishing touches ───────────────────────────────────────────────────

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
        try:
            found = await self._hydration.hydrate_ids((product_id,), context)
            product = next((p for p in found if p.product_id == product_id), None)
            if product is None:
                return _refused(state, "option_unavailable")
            # Any stocked kind may finish a room: the guide is Nora's hint,
            # never a list of what is allowed (CLAUDE.md 10.1).
            kind = product.commerce.subcategory or product.commerce.category or "piece"
            need = DesignCategoryNeed(
                commerce_category=product.commerce.category,
                commerce_subcategory=product.commerce.subcategory,
                priority=DesignPriority.OPTIONAL,
                design=PieceDesign(role=customer_words(kind)[:40]),
            )
            plan = InteriorDesignResult(needs=(*_plan_from(room).needs, need))
            outcome = await self._pinned_outcome(state, context, added=((need, product_id),))
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

    async def change(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        *,
        remove_pieces: Sequence[int],
        add_products: Sequence[int],
        allow_over_budget: bool,
    ) -> RoomOutcome:
        """Take pieces out and put products in, as one change.

        "These two instead of that pair" is one change with one total: every
        other piece stays the product it is, the room is costed once with no
        ceiling, and over budget the whole change is held for one yes - never
        applied piece by piece. A product put in where a piece of its kind
        came out takes that piece's role and priority.
        """
        room = state.room_project
        if room is None or not room.bundle_items:
            return _refused(state, "no_room")
        if room.in_cart:
            return _refused(state, "room_in_cart")
        if not remove_pieces and not add_products:
            return _refused(state, "nothing_to_change")
        cards = []
        for piece in dict.fromkeys(remove_pieces):
            card = _card(room, piece)
            if card is None:
                return _refused(state, "piece_not_found")
            if len(card.need_ids) > 1:
                return _refused(state, "piece_spans_several_roles")
            cards.append(card)
        leaving = {line_id for card in cards for line_id in card.line_ids}
        staying = {line.product_id for line in room.bundle_items if line.line_id not in leaving}
        wanted = tuple(dict.fromkeys(add_products))
        if any(product_id in staying for product_id in wanted):
            return _refused(state, "already_in_room")
        removed_needs = {
            need.need_id: need
            for need in room.design_needs
            if any(need.need_id in card.need_ids for card in cards)
        }
        operations: list[BundleOperation] = []
        need_ids = tuple(need_id for card in cards for need_id in card.need_ids)
        if need_ids:
            operations.append(
                RefineBundle(
                    removed_need_ids=need_ids,
                    preserved=tuple(
                        PreservedBundleLine(line_id=line.line_id)
                        for line in room.bundle_items
                        if line.status is BundleItemStatus.LOCKED and line.line_id not in leaving
                    ),
                )
            )
        operations.extend(
            RemoveBundleLine(line_id=line_id)
            for card in cards
            if not card.need_ids
            for line_id in card.line_ids
        )
        staged = (
            apply_update(
                state,
                AgentStateUpdate(
                    room_project=RoomProjectUpdate(bundle_operations=tuple(operations))
                ),
            )
            if operations
            else state
        )
        staged_room = staged.room_project
        assert staged_room is not None
        try:
            found = await self._hydration.hydrate_ids(wanted, context) if wanted else []
            by_id = {product.product_id: product for product in found}
            if any(product_id not in by_id for product_id in wanted):
                return _refused(state, "option_unavailable")
            added = tuple(
                (_replacing_need(by_id[product_id], removed_needs.values()), product_id)
                for product_id in wanted
            )
            if not staged_room.design_needs and not added:
                return await self._rendered(staged, context, code="ok", committed=True)
            outcome = await self._pinned_outcome(staged, context, pinned_from=room, added=added)
        except _CATALOG_FAILURES:
            return _refused(state, "catalog_unavailable")
        if isinstance(outcome, str):
            return _refused(state, outcome)
        if not isinstance(outcome, RoomBundle):
            return _unavailable(state, outcome)
        placed = {line.product.product_id for line in outcome.lines}
        if any(product_id not in placed for product_id in wanted):
            return _refused(state, "option_unavailable")
        overage = _over_budget(outcome, room.budget)
        if overage is not None and not allow_over_budget:
            return self._proposal(state, outcome, "over_budget", room.budget, [])
        plan = InteriorDesignResult(
            needs=(*_plan_from(staged_room).needs, *(need for need, _ in added))
        )
        committed = _commit_plan(staged, plan, outcome)
        raised_from = None
        if overage is not None:
            committed, raised_from = _raise_budget(committed, outcome)
        committed = committed.model_copy(update={"room_swap": None, "swap_budget_offer": None})
        logger.info(
            "room_engine_changed",
            store_id=context.store_id,
            removed=len(cards),
            added=len(wanted),
            over_budget=overage is not None,
        )
        return self._committed(committed, outcome, budget_raised_from=raised_from)

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

    async def _lock_picks(
        self,
        staged: AgentStateV1,
        original: AgentStateV1,
        context: RetailerContext,
        plan: InteriorDesignResult,
        *,
        only: tuple[int, ...] | None = None,
        must: tuple[int, ...] = (),
    ) -> AgentStateV1:
        """The customer's picks that are pieces of this room, as locked lines -
        of `only`, when given. A pick she named (`must`) is locked whatever
        its type; any other only when it is one of the room's pieces."""
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
            if p.product_id in must or any(_is_piece(p, need) for need in plan.needs)
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

    def _room_seats(self, bundle: RoomBundle | None) -> int | None:
        """How many the room's seating seats, or None when any count is unknown."""
        if bundle is None or self._seating is None:
            return None
        total = 0
        for line in bundle.lines:
            commerce = line.product.commerce
            if commerce.category != SEATING_CATEGORY or not self._seating.counts_as_seats(
                commerce.subcategory
            ):
                continue
            seats = commerce.seating_capacity or self._seating.implied_capacity(
                commerce.subcategory
            )
            if seats is None:
                return None
            total += seats * line.quantity
        return total or None

    # ── internals: re-costing a room that exists ────────────────────────────

    async def _pinned_outcome(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        *,
        released: Collection[int] = (),
        forced: Mapping[int, int] | None = None,
        searched: Mapping[int, DesignCategoryNeed] | None = None,
        pinned_from: RoomProjectState | None = None,
        budget: PriceConstraint | None = None,
        added: Sequence[tuple[DesignCategoryNeed, int]] = (),
    ) -> BundleOptimizationOutcome | str:
        """The room costed with every role fixed to a product.

        Each unlocked role is pinned to the product it holds (in `pinned_from`,
        or the room itself), `forced` overrides that per role, and `added` is
        more roles, each fixed to one product. Locks stay locks; a role covered by
        a lock, or never filled, is not searched. Nothing is re-selected, so a
        dear change is never balanced by quietly dropping a piece.
        """
        room = state.room_project
        assert room is not None
        locks = await self._verify_locks(state, context, released)
        if locks is None:
            return "locked_unavailable"
        source = pinned_from or room
        current: dict[int, int] = {}
        for line in source.bundle_items:
            if line.need_id is not None and line.status is not BundleItemStatus.LOCKED:
                current.setdefault(line.need_id, line.product_id)
        # A released role's own lock is not a lock for this costing.
        for line in source.bundle_items:
            if line.need_id is not None and line.need_id in released:
                current.setdefault(line.need_id, line.product_id)
        current.update(forced or {})
        plan = _plan_from(room)
        # Every pinned product re-read in one query, not one per piece.
        pinned = [
            current[need.need_id]
            for need in room.design_needs
            if need.need_id in current and not (searched and need.need_id in searched)
        ]
        pools = await self._pipeline.execute_forced_pools(
            (*pinned, *(product_id for _, product_id in added)), context
        )
        # The revised roles are found afresh together - side by side, their
        # ranking texts embedded in one request - and the rest stay pinned.
        positions = [
            index
            for index, stored in enumerate(room.design_needs)
            if searched and stored.need_id in searched
        ]
        found_again: dict[int, DesignNeedCandidates] = {}
        if searched and positions:
            capabilities = await self._capabilities.capabilities(context)
            found = await self._design_discovery.discover(
                _room_request(room, capabilities),
                InteriorDesignResult(
                    needs=tuple(searched[room.design_needs[i].need_id] for i in positions)
                ),
                context,
            )
            found_again = {
                index: entry.model_copy(update={"need_index": index})
                for index, entry in zip(positions, found.needs, strict=True)
            }
        entries: list[DesignNeedCandidates] = []
        for index, need in enumerate(plan.needs):
            if index in found_again:
                entries.append(found_again[index])
                continue
            need_id = room.design_needs[index].need_id
            entries.append(_forced_entry(index, need, current.get(need_id), pools))
        for offset, (need, product_id) in enumerate(added):
            entries.append(_forced_entry(len(plan.needs) + offset, need, product_id, pools))
        return self._optimizer.optimize(
            BundleOptimizationRequest(
                discovery=DesignDiscoveryResult(needs=tuple(entries)),
                budget=budget,
                locked=tuple(product for _, product in locks),
            )
        )

    async def _verify_locks(
        self,
        state: AgentStateV1,
        context: RetailerContext,
        released: Collection[int] = (),
    ) -> list[tuple[BundleItemState, LockedBundleProduct]] | None:
        """Every locked line with its product re-read, or None if one is gone.

        A lock that no longer resolves stops the operation: it cannot be
        dropped, substituted or answered from memory (CLAUDE.md 10).
        """
        room = state.room_project
        lines = [
            line
            for line in (room.bundle_items if room else ())
            if line.status is BundleItemStatus.LOCKED and line.need_id not in released
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
        if room is not None and view is not None:
            _review_facts(
                facts,
                room,
                {line.product.product_id: line.product for line in outcome.lines},
            )
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


def _card(room: RoomProjectState, piece: int) -> BundleCardGroup | None:
    cards = group_bundle_cards(room.bundle_items)
    return cards[piece - 1] if 1 <= piece <= len(cards) else None


def _paged(request: ProductSearchRequest) -> ProductSearchRequest:
    """The request without the products paged past: the next page of a piece's
    options is still that piece's options."""
    return request.model_copy(update={"exclude_product_ids": ()})


def open_room_options(state: AgentStateV1) -> int | None:
    """The room piece whose options are the list on screen now, or None.

    While they are, the cards are that piece's alternatives: choosing one puts
    it in the room in the piece's place, never in the cart (CLAUDE.md 10.1)."""
    swap = state.room_swap
    room = state.room_project
    search = state.active_search
    interaction = state.product_interaction
    if (
        swap is None
        or room is None
        or search is None
        or swap.bundle_revision != room.bundle_revision
        or _paged(search.request) != _paged(swap.request)
        or interaction.presented_search_revision != search.revision
    ):
        return None
    return swap.bundle_ordinal


def _option(state: AgentStateV1, piece: int, option: int) -> int | None:
    """The product behind option `option`, while the options shown for this
    piece are still the list on screen and the room has not moved."""
    if open_room_options(state) != piece:
        return None
    shown = state.product_interaction.presented_product_ids
    return shown[option - 1] if 1 <= option <= len(shown) else None


def _need_words(need: DesignCategoryNeed | RoomDesignNeedState) -> str:
    return customer_words(need.commerce_subcategory or need.commerce_category)


def _room_request(
    room: RoomProjectState, capabilities: RetailerCatalogCapabilities
) -> InteriorDesignRequest:
    palette = _palette_preferences(room)
    return InteriorDesignRequest(
        task=DesignTask.ROOM_PLAN,
        room_type=room.room_type,
        geometry=room.geometry,
        layout=room.layout,
        budget=room.budget,
        # The room's palette as Nora designed it; a room saved before has
        # only the customer's own words.
        design_preferences=palette or room.design_preferences,
        catalog_capabilities=capabilities,
    )


def _palette_preferences(room: RoomProjectState) -> tuple[SemanticPreference, ...]:
    return tuple(
        SemanticPreference(
            family=family,
            raw_value=value,
            canonical_value=value,
            strength=ConstraintStrength.PREFERRED,
        )
        for family, values in (
            (AttributeFamily.COLOR, room.palette),
            (AttributeFamily.STYLE, room.palette_styles),
        )
        for value in values
    )


def _is_piece(product: ProductCandidate, need: DesignCategoryNeed) -> bool:
    """Whether a product is of a type that may fill the need."""
    types = (need.commerce_subcategory, *(need.design.alternatives if need.design else ()))
    return product.commerce.category == need.commerce_category and (
        need.commerce_subcategory is None or product.commerce.subcategory in types
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
                design=need.design,
            )
            for need in room.design_needs
        )
    )


def _forced_entry(
    index: int,
    need: DesignCategoryNeed,
    product_id: int | None,
    pools: Mapping[int, CandidatePoolResult],
) -> DesignNeedCandidates:
    """A role fixed to one product, from the pools read together; a role with
    no product is one the store cannot supply."""
    if product_id is None:
        return DesignNeedCandidates(
            need_index=index, need=need, skipped=DesignNeedSkipReason.RETAILER_CANNOT_SUPPLY
        )
    pool = pools.get(product_id)
    if pool is None:
        # Not read with the rest: treated as the store no longer carrying it.
        pool = CandidatePoolResult(
            candidates=(),
            eligible_count=0,
            was_relaxed=False,
            stop_reason=StopReason.EXACT_SUFFICIENT,
            semantic_used=False,
        )
    return DesignNeedCandidates(need_index=index, need=need, pool=pool)


def _replacing_need(
    product: ProductCandidate, removed: Iterable[RoomDesignNeedState]
) -> DesignCategoryNeed:
    """The role a product put into the room fills: the role of a piece of its
    kind taken out in the same change, else a finishing touch of its own."""
    kind = product.commerce.subcategory or product.commerce.category or "piece"
    replaced = next(
        (need for need in removed if need.commerce_subcategory == product.commerce.subcategory),
        None,
    )
    return DesignCategoryNeed(
        commerce_category=product.commerce.category,
        commerce_subcategory=product.commerce.subcategory,
        priority=replaced.priority if replaced is not None else DesignPriority.OPTIONAL,
        design=(
            replaced.design
            if replaced is not None and replaced.design is not None
            else PieceDesign(role=customer_words(kind)[:40])
        ),
    )


def _with_rejection(room: RoomProjectState, need_id: int, product_id: int) -> tuple[int, ...]:
    current = next(
        (need.rejected_product_ids for need in room.design_needs if need.need_id == need_id), ()
    )
    return tuple(dict.fromkeys((*current, product_id)))[-MAX_EXCLUDED_PRODUCT_IDS:]


def _budget_left(total: Decimal | None, ceiling: Decimal | None) -> Decimal | None:
    """The budget the room has not spent, when it is within it."""
    if total is None or ceiling is None or total > ceiling:
        return None
    return ceiling - total


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
    locked = [
        line
        for line in (room.bundle_items if room else ())
        if line.status is BundleItemStatus.LOCKED
    ]
    locked_ids = [line.line_id for line in locked]
    # The role the optimiser filled with each lock, by its product.
    filled = {
        line.product.product_id: line.need_index
        for line in outcome.lines
        if line.locked and line.need_index is not None
    }
    bindings = tuple(
        (line.line_id, filled[line.product_id]) for line in locked if line.product_id in filled
    )
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
                                design=need.design,
                            )
                            for need in plan.needs
                        ),
                        preserved=tuple(
                            PreservedBundleLine(line_id=line_id) for line_id in locked_ids
                        ),
                        locked_needs=bindings,
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
    state: AgentStateV1,
    outcome: RoomBundle,
    override: DesignNeedSearchOverride | None,
    *,
    revised: Sequence[DesignNeedRefinement] = (),
) -> AgentStateV1:
    """The refined room under the same plan identity, locks preserved - except
    the locks of the roles re-chosen (`override` or `revised`), released into
    their new products."""
    room = state.room_project
    assert room is not None
    by_position = {position: need.need_id for position, need in enumerate(room.design_needs)}
    refinements: tuple[DesignNeedRefinement, ...] = (
        (
            DesignNeedRefinement(
                need_id=override.need_id, rejected_product_ids=override.exclude_product_ids
            ),
        )
        if override is not None
        else ()
    )
    refinements = (*refinements, *revised)
    released = {r.need_id for r in revised} | (
        {override.need_id} if override is not None else set()
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
                            and line.need_id not in released
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
        left = _budget_left(totals.new_spend_total, totals.budget_max_amount)
        if left is not None:
            # What the room has not spent, summed here so it can be said.
            facts["budget_left"] = left
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


def _review_facts(
    facts: dict[str, object],
    room: RoomProjectState,
    products: Mapping[int, ProductCandidate],
) -> None:
    """Each piece beside what it was designed to be, for Nora to judge before
    she shows the room: its role, its actual colour and styles, its listed
    size and whether that is in her range, and its share of the budget. And
    the room's colours in one line. No verdict on fit or look: whether the
    room works and carries their colours is hers to judge (CLAUDE.md 10.1)."""
    pieces = facts.get("pieces")
    if not isinstance(pieces, list):
        return
    needs = {need.need_id: need for need in room.design_needs}
    ceiling = room.budget.max_amount if room.budget is not None else None
    colours: list[str] = []
    for card, piece in zip(group_bundle_cards(room.bundle_items), pieces, strict=False):
        product = products.get(card.product_id)
        need = needs.get(card.need_ids[0]) if len(card.need_ids) == 1 else None
        design = need.design if need is not None else None
        if product is None or not isinstance(piece, dict):
            continue
        piece["colour"] = product.main_color
        piece["styles"] = list(product.styles)
        size = listed_size(product)
        piece["size"] = size or "not listed"
        role = design.role if design is not None else piece.get("kind") or "piece"
        colours.append(f"{role}: {product.main_color or 'not listed'}")
        if design is None:
            continue
        piece["role"] = design.role
        if any(
            v is not None
            for v in (
                design.longer_min_cm,
                design.longer_max_cm,
                design.shorter_min_cm,
                design.shorter_max_cm,
            )
        ):
            sides = sorted(
                v for v in (product.dimensions.length_cm, product.dimensions.width_cm) if v
            )
            longer = sides[-1] if sides else None
            shorter = sides[0] if len(sides) == 2 else None

            def within(side: Decimal | None, low: int | None, high: int | None) -> bool | None:
                if low is None and high is None:
                    return True
                if side is None:
                    return None
                return (low or 0) <= side <= (high or side)

            checks = (
                within(longer, design.longer_min_cm, design.longer_max_cm),
                within(shorter, design.shorter_min_cm, design.shorter_max_cm),
            )
            piece["size_in_range"] = (
                "not listed" if None in checks else all(c is True for c in checks)
            )
        line_total = piece.get("line_total")
        if design.share_percent is not None:
            piece["share_planned_percent"] = design.share_percent
        if ceiling and isinstance(line_total, Decimal):
            piece["share_used_percent"] = int(line_total * 100 / ceiling)
    if colours:
        facts["colours_in_room"] = colours


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
