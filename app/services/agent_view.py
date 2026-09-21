"""Projecting authoritative state into what a model may see.

`AgentStateV1` is the truth; `AgentStateView` is the part of it that can be put
in front of a language model without handing it something to act on unsafely.

Two things are dropped on the way through, and both matter more than they look.

* **Every product id.** Products become a count and a set of positions - enough
  to understand "the second one", never enough to name it. The whole authority
  design rests on the model not emitting an id, and the surest way to make it
  emit one is to show it some.
* **Every revision and the schema version.** Search lineage is execution
  bookkeeping. A model that could see it could start reasoning about it.

Retailer scope is not dropped here because it was never in the state to begin
with: `store_id` lives on `RetailerContext` (CLAUDE.md 8, 20.2).

Pure and total. No I/O, no clock, no reasoning - the same state always yields
the same view.
"""

from __future__ import annotations

from app.schemas.agent_state import ActiveSearchState, AgentStateV1, RoomProjectState
from app.schemas.agent_view import (
    ActiveSearchView,
    AgentStateView,
    CapacityView,
    DimensionView,
    PlanarView,
    PreferenceView,
    PresentedProductsView,
    PriceView,
    RoomProjectView,
)
from app.schemas.discovery import PriceConstraint
from app.schemas.query import ConstraintSemantics, SemanticPreference


def project_state(state: AgentStateV1) -> AgentStateView:
    """The model-safe view of one conversation's memory."""
    return AgentStateView(
        active_search=_active_search(state.active_search),
        customer_preferences=_preferences(
            state.customer_preferences.semantic_preferences
        ),
        presented=_presented(state),
        room_project=_room_project(state.room_project),
        purchase_stage=state.derived_commerce.purchase_stage,
    )


def _preferences(
    preferences: tuple[SemanticPreference, ...],
) -> tuple[PreferenceView, ...]:
    return tuple(
        PreferenceView(
            family=preference.family,
            raw_value=preference.raw_value,
            canonical_value=preference.canonical_value,
            strength=preference.strength,
        )
        for preference in preferences
    )


def _price(
    price: PriceConstraint | None, semantics: ConstraintSemantics
) -> PriceView | None:
    """The bounds, each with how firmly the customer meant it.

    Strengths are attached per bound rather than passing `ConstraintSemantics`
    through: the model needs to know a budget was a hard limit, not how the
    semantics object is shaped.
    """
    if price is None:
        return None
    return PriceView(
        currency=price.currency,
        min_amount=price.min_amount,
        max_amount=price.max_amount,
        min_exclusive=price.min_exclusive,
        max_exclusive=price.max_exclusive,
        min_strength=semantics.price_min,
        max_strength=semantics.price_max,
    )


def _active_search(search: ActiveSearchState | None) -> ActiveSearchView | None:
    if search is None:
        return None
    request, semantics = search.request, search.semantics
    strength_by_role = {d.role: d.strength for d in semantics.dimensions}
    return ActiveSearchView(
        commerce_category=request.commerce_category,
        commerce_subcategory=request.commerce_subcategory,
        price=_price(request.price, semantics),
        seating_capacity=(
            CapacityView(
                min_capacity=request.seating_capacity.min_capacity,
                max_capacity=request.seating_capacity.max_capacity,
                min_strength=semantics.seating_min,
                max_strength=semantics.seating_max,
            )
            if request.seating_capacity is not None
            else None
        ),
        dimensions=tuple(
            DimensionView(
                role=constraint.role,
                kind=constraint.kind,
                min_cm=constraint.min_cm,
                max_cm=constraint.max_cm,
                target_cm=constraint.target_cm,
                source_value=constraint.source_value,
                source_unit=constraint.source_unit,
                # Every request dimension has exactly one recorded strength,
                # an invariant the contracts enforce at construction, so this
                # lookup by role cannot miss (CLAUDE.md 13.2).
                strength=strength_by_role[constraint.role],
            )
            for constraint in request.dimensions
        ),
        planar_dimensions=(
            PlanarView(
                first_cm=request.planar_dimensions.first_cm,
                second_cm=request.planar_dimensions.second_cm,
                source_unit=request.planar_dimensions.source_unit,
                strength=semantics.planar_dimension.strength,
            )
            if request.planar_dimensions is not None
            and semantics.planar_dimension is not None
            else None
        ),
        required_colors=request.colors_any_of,
        required_styles=request.styles_all_of,
        attribute_preferences=_preferences(search.semantic_preferences),
        semantic_intent=search.semantic_intent,
        sort=request.sort,
        # `revision` and `exclude_product_ids` are deliberately not projected:
        # one is lineage bookkeeping, the other is a product id.
    )


def _presented(state: AgentStateV1) -> PresentedProductsView:
    """Counts and positions. Never an id.

    `selected_ordinals` holds only selections that are *currently on screen*.
    A product selected three turns ago has no position in this list, and
    inventing one would claim a place the customer cannot see - so this can be
    shorter than `selected_count`, and is never padded to match.
    """
    interaction = state.product_interaction
    position_of = {
        product_id: ordinal
        for ordinal, product_id in enumerate(interaction.presented_product_ids, start=1)
    }
    return PresentedProductsView(
        count=len(interaction.presented_product_ids),
        has_focused_product=interaction.focused_product_id is not None,
        selected_count=len(interaction.selected_product_ids),
        selected_ordinals=tuple(
            position_of[product_id]
            for product_id in interaction.selected_product_ids
            if product_id in position_of
        ),
    )


def _room_project(room: RoomProjectState | None) -> RoomProjectView | None:
    if room is None:
        return None
    return RoomProjectView(
        room_type=room.room_type,
        budget=_price(room.budget, ConstraintSemantics()),
        design_preferences=_preferences(room.design_preferences),
        bundle_item_count=len(room.bundle_product_ids),
        locked_item_count=len(room.locked_product_ids),
    )
