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

from app.core.exceptions import InvalidRequestError
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    CustomerPreferenceState,
    DerivedCommerceState,
    ProductInteractionState,
    RoomProjectState,
)
from app.schemas.agent_updates import (
    ActiveSearchUpdate,
    AgentStateUpdate,
    ClearSemanticIntent,
    CustomerPreferenceUpdate,
    DerivedCommerceUpdate,
    ProductInteractionUpdate,
    RoomProjectUpdate,
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


def commit_search_results(
    state: AgentStateV1, product_ids: tuple[int, ...]
) -> AgentStateV1:
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
        raise InvalidRequestError(
            reason="cannot commit search results without an active search"
        )
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
        semantic_preferences=apply_items(
            current.semantic_preferences, update.semantic_preferences
        )
    )


def _search(
    current: ActiveSearchState | None, update: ActiveSearchUpdate | None
) -> ActiveSearchState | None:
    if update is None:
        return current
    if current is None:
        if update.request is None:
            raise InvalidRequestError(
                reason="a first active search needs a request"
            )
        return ActiveSearchState(
            request=update.request,
            semantics=update.semantics or ConstraintSemantics(),
            semantic_preferences=apply_items((), update.semantic_preferences),
            semantic_intent=_intent(None, update.semantic_intent),
            revision=NO_RESULTS_REVISION,
        )
    return ActiveSearchState(
        request=update.request if update.request is not None else current.request,
        semantics=(
            update.semantics if update.semantics is not None else current.semantics
        ),
        semantic_preferences=apply_items(
            current.semantic_preferences, update.semantic_preferences
        ),
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
        selected_product_ids=apply_items(
            current.selected_product_ids, update.selected_product_ids
        ),
    )


def _room(
    current: RoomProjectState | None, update: RoomProjectUpdate | None
) -> RoomProjectState | None:
    if update is None:
        return current
    base = current or RoomProjectState()
    return RoomProjectState(
        room_type=None if update.clear_room_type else (update.room_type or base.room_type),
        budget=None if update.clear_budget else (update.budget or base.budget),
        design_preferences=apply_items(
            base.design_preferences, update.design_preferences
        ),
        bundle_product_ids=apply_items(
            base.bundle_product_ids, update.bundle_product_ids
        ),
        locked_product_ids=apply_items(
            base.locked_product_ids, update.locked_product_ids
        ),
    )


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
