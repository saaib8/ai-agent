"""The reducer: the only thing that writes state.

Pure and deterministic. No model, no database, no network, no clock. It takes
the state as it is and a typed proposal, and returns a new state built through
the real constructors so every invariant is re-checked on the way out. Nothing
is copied past a validator.

That last point is the whole safety argument. An agent proposes; this function
decides whether the result is a state that may exist. A proposal that would
lock a product outside its bundle, or focus one nobody has seen, is rejected
here rather than discovered later.

Two authorities, two entry points. :func:`apply_update` takes what an agent
proposed about the conversation. :func:`commit_search_results` records what
discovery actually executed, and is callable only by application code - which
is why it is a separate function rather than a field on the update schema.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.exceptions import InvalidRequestError
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    CustomerPreferenceState,
    DerivedCommerceState,
    ProductInteractionState,
    RoomDesignNeedState,
    RoomProjectState,
)
from app.schemas.agent_updates import (
    ActiveSearchUpdate,
    AddBundleLine,
    AgentStateUpdate,
    BundleLineSpec,
    BundleOperation,
    ClearSemanticIntent,
    CustomerPreferenceUpdate,
    DerivedCommerceUpdate,
    DesignNeedRefinement,
    ProductInteractionUpdate,
    RefineBundle,
    RemoveBundleLine,
    ReplaceBundle,
    ReplaceDesignPlan,
    RoomProjectUpdate,
    SetBundleLineAcquisition,
    SetBundleLineQuantity,
    SetBundleLineStatus,
    SetSemanticIntent,
    apply_items,
)
from app.schemas.query import ConstraintSemantics

NO_RESULTS_REVISION = 0
"""A search exists but nothing has been presented for it yet."""


def apply_update(state: AgentStateV1, update: AgentStateUpdate) -> AgentStateV1:
    """A new state. The one passed in is never touched."""
    customer = _customer(state.customer_preferences, update.customer_preferences)
    search = _search(state.active_search, update.active_search)
    interaction = _interaction(state.product_interaction, update.product_interaction)
    room = _room(state.room_project, update.room_project)
    commerce = _commerce(state.derived_commerce, update.derived_commerce)

    return AgentStateV1(
        customer_preferences=customer,
        active_search=search,
        product_interaction=interaction,
        room_project=room,
        derived_commerce=commerce,
    )


# ── the application-owned result commit ─────────────────────────────────────


def commit_search_results(state: AgentStateV1, product_ids: tuple[int, ...]) -> AgentStateV1:
    """Record the result set discovery actually returned, in its order.

    **Application-owned.** Which products were presented is a fact about an
    executed search, not a proposal about the conversation, so this is
    deliberately not reachable through :class:`AgentStateUpdate`: an agent
    cannot claim a search ran, nor choose what it returned.

    Atomic by construction. The new revision, the presented list and the
    revision that list belongs to are all produced here from one value, so the
    state can never hold a revision its presented results do not match.
    """
    search = state.active_search
    if search is None:
        raise InvalidRequestError(reason="cannot commit search results without an active search")
    revision = search.revision + 1
    interaction = state.product_interaction
    return AgentStateV1(
        customer_preferences=state.customer_preferences,
        active_search=ActiveSearchState(
            request=search.request,
            semantics=search.semantics,
            semantic_preferences=search.semantic_preferences,
            semantic_intent=search.semantic_intent,
            revision=revision,
        ),
        product_interaction=ProductInteractionState(
            presented_product_ids=product_ids,
            presented_search_revision=revision,
            # The customer has not seen these products yet, so a focus on the
            # previous result set no longer refers to anything they can see.
            focused_product_id=None,
            selected_product_ids=interaction.selected_product_ids,
        ),
        room_project=state.room_project,
        derived_commerce=state.derived_commerce,
    )


# ── per-domain transitions ──────────────────────────────────────────────────


def _customer(
    current: CustomerPreferenceState, update: CustomerPreferenceUpdate | None
) -> CustomerPreferenceState:
    if update is None:
        return current
    return CustomerPreferenceState(
        semantic_preferences=apply_items(current.semantic_preferences, update.semantic_preferences)
    )


def _search(
    current: ActiveSearchState | None, update: ActiveSearchUpdate | None
) -> ActiveSearchState | None:
    if update is None:
        return current
    if current is None:
        if update.request is None:
            raise InvalidRequestError(reason="a first active search needs a request")
        return ActiveSearchState(
            request=update.request,
            semantics=update.semantics or ConstraintSemantics(),
            semantic_preferences=apply_items((), update.semantic_preferences),
            semantic_intent=_intent(None, update.semantic_intent),
            revision=NO_RESULTS_REVISION,
        )
    return ActiveSearchState(
        request=update.request if update.request is not None else current.request,
        semantics=(update.semantics if update.semantics is not None else current.semantics),
        semantic_preferences=apply_items(current.semantic_preferences, update.semantic_preferences),
        semantic_intent=_intent(current.semantic_intent, update.semantic_intent),
        # Carried, never taken from the update: criteria changing is not a
        # search executing.
        revision=current.revision,
    )


def _intent(
    current: str | None, update: SetSemanticIntent | ClearSemanticIntent | None
) -> str | None:
    if update is None:
        return current
    if isinstance(update, ClearSemanticIntent):
        return None
    return update.value


def _interaction(
    current: ProductInteractionState, update: ProductInteractionUpdate | None
) -> ProductInteractionState:
    if update is None:
        return current
    if update.clear_focus:
        focus: int | None = None
    elif update.focused_product_id is not None:
        focus = update.focused_product_id
    else:
        focus = current.focused_product_id
    return ProductInteractionState(
        presented_product_ids=current.presented_product_ids,
        presented_search_revision=current.presented_search_revision,
        focused_product_id=focus,
        selected_product_ids=apply_items(current.selected_product_ids, update.selected_product_ids),
    )


def _room(
    current: RoomProjectState | None, update: RoomProjectUpdate | None
) -> RoomProjectState | None:
    if update is None:
        return current
    base = current or RoomProjectState()
    return RoomProjectState(
        room_type=None if update.clear_room_type else (update.room_type or base.room_type),
        geometry=None if update.clear_geometry else (update.geometry or base.geometry),
        budget=None if update.clear_budget else (update.budget or base.budget),
        regular_seating_count=(
            None
            if update.clear_regular_seating_count
            else (update.regular_seating_count or base.regular_seating_count)
        ),
        design_preferences=apply_items(base.design_preferences, update.design_preferences),
        **_bundle(base, update.bundle_operations),
    )


def _bundle(base: RoomProjectState, operations: Sequence[BundleOperation]) -> dict[str, object]:
    """The room bundle after this turn's operations, and what that cost.

    Three fields move together or not at all, which is why they are produced
    here rather than assigned independently: the lines, the id the next line
    will take, and the revision that says the content changed.

    **The revision advances on content, not on effort.** Applying an operation
    that changes nothing - locking a line that is already locked, removing a
    line that is not there - leaves it where it was, so a caller cannot make
    the bundle look edited by asking for a no-op. A whole-room commit is one
    operation and therefore advances it exactly once, whatever it replaces.

    Ids are never reused. The counter only ever rises, so a reference to a
    removed line stays dangling rather than quietly resolving to a different
    product later.
    """
    items = list(base.bundle_items)
    needs = list(base.design_needs)
    next_id = base.next_bundle_line_id
    next_need_id = base.next_design_need_id

    for operation in operations:
        match operation:
            case RefineBundle():
                needs, items, next_id = _refined(needs, items, operation, next_id)
            case ReplaceDesignPlan():
                needs, items, next_need_id, next_id = _replanned(
                    items, operation, next_need_id, next_id
                )
            case ReplaceBundle():
                items, next_id = _replaced(items, operation, next_id)
            case AddBundleLine():
                created, next_id = _allocate((operation.line,), next_id)
                items = [*items, *created]
            case RemoveBundleLine():
                items = [item for item in items if item.line_id != operation.line_id]
            case _:
                items = [_edited(item, operation) for item in items]

    lines = tuple(items)
    changed = lines != base.bundle_items
    return {
        "design_needs": tuple(needs),
        "next_design_need_id": next_need_id,
        "bundle_items": lines,
        "next_bundle_line_id": next_id,
        "bundle_revision": base.bundle_revision + 1 if changed else base.bundle_revision,
    }


def _refined(
    needs: Sequence[RoomDesignNeedState],
    items: Sequence[BundleItemState],
    operation: RefineBundle,
    next_line_id: int,
) -> tuple[list[RoomDesignNeedState], list[BundleItemState], int]:
    """One room refined: the roles that changed, and the room chosen under them.

    The plan keeps its identity - same ids, same order, same allocator - because
    refining a room is not replanning one. A removed role is gone for good and
    its id is never reissued, so a reference to it fails closed.

    Refinements and the bundle land together. A staged rejection is true only of
    a room that was chosen while excluding it, and persisting one beside a
    bundle picked without it would make state claim a reason the selection never
    satisfied.
    """
    removed = set(operation.removed_need_ids)
    changes = {entry.need_id: entry for entry in operation.refinements}
    unknown = (removed | set(changes)) - {need.need_id for need in needs}
    if unknown:
        raise InvalidRequestError(reason="a refinement names a design need the plan does not have")

    kept_needs = [
        _refined_need(need, changes.get(need.need_id))
        for need in needs
        if need.need_id not in removed
    ]
    surviving = {need.need_id for need in kept_needs}

    by_id = {item.line_id: item for item in items}
    kept_lines: list[BundleItemState] = []
    for entry in operation.preserved:
        existing = by_id.get(entry.line_id)
        if existing is None:
            raise InvalidRequestError(reason="a preserved bundle line must already exist")
        if existing.status is not BundleItemStatus.LOCKED:
            raise InvalidRequestError(reason="only a locked bundle line may be preserved")
        if existing.need_id is not None and existing.need_id not in surviving:
            # Its role is gone, so the line goes with it: an explicit removal
            # supersedes an earlier instruction to keep the piece filling it.
            continue
        kept_lines.append(existing)

    created, next_line_id = _allocate(operation.added, next_line_id)
    for line in created:
        if line.need_id is not None and line.need_id not in surviving:
            raise InvalidRequestError(
                reason="a bundle line names a design need the plan does not have"
            )
    return kept_needs, [*kept_lines, *created], next_line_id


def _refined_need(
    need: RoomDesignNeedState, change: DesignNeedRefinement | None
) -> RoomDesignNeedState:
    """One role with what the refinement changed, rebuilt through its validators."""
    if change is None:
        return need
    update: dict[str, object] = {}
    if change.rejected_product_ids is not None:
        update["rejected_product_ids"] = change.rejected_product_ids
    if change.clear_semantic_intent:
        update["semantic_intent"] = None
    elif change.semantic_intent is not None:
        update["semantic_intent"] = change.semantic_intent
    return RoomDesignNeedState.model_validate({**need.model_dump(), **update})


def _replanned(
    items: Sequence[BundleItemState],
    operation: ReplaceDesignPlan,
    next_need_id: int,
    next_line_id: int,
) -> tuple[list[RoomDesignNeedState], list[BundleItemState], int, int]:
    """A whole plan and the room chosen from it, committed together.

    The atomicity is the point. A plan and the bundle built from it are one
    proposal, so there is no ordering in which new needs could be observed
    beside a bundle chosen from different ones - the state moves once or not at
    all.

    Needs take fresh ids in the plan's own order. A line's `need_index` is a
    position in *that* list and is mapped here; a position outside it is an
    invariant violation rather than something to drop quietly, because a line
    filling a need nobody planned means the caller mapped the wrong plan.

    A preserved lock keeps its identity and **loses its need**: the role it
    belonged to no longer exists, and matching it onto one of the new roles
    would be a guess (M12E-4A 14).
    """
    needs = [
        RoomDesignNeedState(
            need_id=next_need_id + offset,
            commerce_category=spec.commerce_category,
            commerce_subcategory=spec.commerce_subcategory,
            priority=spec.priority,
            quantity=spec.quantity,
            seating_capacity=spec.seating_capacity,
            semantic_intent=spec.semantic_intent,
        )
        for offset, spec in enumerate(operation.needs)
    ]
    next_need_id += len(operation.needs)

    by_id = {item.line_id: item for item in items}
    kept: list[BundleItemState] = []
    for entry in operation.preserved:
        existing = by_id.get(entry.line_id)
        if existing is None:
            raise InvalidRequestError(reason="a preserved bundle line must already exist")
        if existing.status is not BundleItemStatus.LOCKED:
            raise InvalidRequestError(reason="only a locked bundle line may be preserved")
        kept.append(existing.model_copy(update={"need_id": None}))

    created: list[BundleItemState] = []
    for offset, spec in enumerate(operation.added):
        if spec.need_index is not None and spec.need_index >= len(needs):
            raise InvalidRequestError(
                reason="a bundle line names a design need the plan does not have"
            )
        created.append(
            BundleItemState(
                line_id=next_line_id + offset,
                product_id=spec.product_id,
                quantity=spec.quantity,
                acquisition=spec.acquisition,
                status=spec.status,
                need_id=(None if spec.need_index is None else needs[spec.need_index].need_id),
            )
        )
    return needs, [*kept, *created], next_need_id, next_line_id + len(operation.added)


def _replaced(
    items: Sequence[BundleItemState], operation: ReplaceBundle, next_id: int
) -> tuple[list[BundleItemState], int]:
    """The whole bundle, in one step: what survives, then what is new.

    A preserved line is **copied unchanged** - id, product, quantity,
    acquisition, status and need index all exactly as they already are. The
    reducer never builds one from the caller's description, because the caller
    performing a whole-room commit holds an optimiser result, and an optimiser
    has no state-line provenance: with duplicate SKUs and unit-by-unit lock
    allocation there is no honest way to say which of two identical locked
    lines its output came from. Copying removes the question.

    Only `LOCKED` lines may be preserved. A suggestion is by definition what a
    re-plan is allowed to reconsider, so asking to preserve one is a caller
    mistake rather than a quiet no-op.
    """
    by_id = {item.line_id: item for item in items}
    kept: list[BundleItemState] = []
    for entry in operation.preserved:
        existing = by_id.get(entry.line_id)
        if existing is None:
            raise InvalidRequestError(reason="a preserved bundle line must already exist")
        if existing.status is not BundleItemStatus.LOCKED:
            raise InvalidRequestError(reason="only a locked bundle line may be preserved")
        kept.append(existing)

    created, next_id = _allocate(operation.added, next_id)
    return [*kept, *created], next_id


def _allocate(specs: Sequence[BundleLineSpec], next_id: int) -> tuple[list[BundleItemState], int]:
    """Specs become lines, each taking the next id in turn."""
    created = [
        BundleItemState(
            line_id=next_id + offset,
            product_id=spec.product_id,
            quantity=spec.quantity,
            acquisition=spec.acquisition,
            status=spec.status,
            need_id=spec.need_id,
        )
        for offset, spec in enumerate(specs)
    ]
    return created, next_id + len(specs)


def _edited(item: BundleItemState, operation: BundleOperation) -> BundleItemState:
    """One line with one field changed, or the line untouched.

    Rebuilt through the real constructor rather than mutated, so every
    invariant is re-checked on every edit.
    """
    match operation:
        case SetBundleLineQuantity() if item.line_id == operation.line_id:
            return item.model_copy(update={"quantity": operation.quantity})
        case SetBundleLineAcquisition() if item.line_id == operation.line_id:
            return item.model_copy(update={"acquisition": operation.acquisition})
        case SetBundleLineStatus() if item.line_id == operation.line_id:
            return item.model_copy(update={"status": operation.status})
        case _:
            return item


def _commerce(
    current: DerivedCommerceState, update: DerivedCommerceUpdate | None
) -> DerivedCommerceState:
    if update is None:
        return current
    if update.clear_purchase_stage:
        return DerivedCommerceState(purchase_stage=None)
    return DerivedCommerceState(
        purchase_stage=update.purchase_stage
        if update.purchase_stage is not None
        else current.purchase_stage
    )
