"""Substituting one product type for another, the one way it is done.

When the application - not the customer - puts another type in place of the one
they asked for (the stock check, the agent loop, a type that seats them, the
closest-type fallback), what carries is decided here and nowhere else
(CLAUDE.md 13.5, 14.7, 14.8): price, colour, style, sort, wishes and their words
carry; a seat count carries only within the same category; sizes never do -
they belong to the type they were given for - and are reported as dropped, not
replaced by sizes saved for the substitute.

Distinct on purpose from the composer's retype, which serves a type change the
customer made themselves ("show me sectionals instead") and so brings back the
sizes they saved for the type they chose.
"""

from __future__ import annotations

from app.schemas.composition import ComposedSearch
from app.schemas.discovery import ProductSearchRequest
from app.schemas.grounding import DroppedConstraint
from app.schemas.query import ConstraintSemantics, ResolvedSearch


def dropped_sizes(resolved: ResolvedSearch) -> tuple[DroppedConstraint, ...]:
    request = resolved.request
    dropped = tuple(DroppedConstraint(role=d.role) for d in request.dimensions)
    return (*dropped, DroppedConstraint()) if request.planar_dimensions else dropped


def as_type(resolved: ResolvedSearch, category: str, subcategory: str) -> ResolvedSearch:
    """The same request for another type, carrying only what transfers.

    Price, colour, style and sort carry: they say what the customer wants
    whatever the piece. Measurements never do - a size belongs to the type it
    was given for (CLAUDE.md 13.5) - and a seat count only within seating,
    where the substitute still seats people. The asked kind does not carry:
    the substitute is disclosed as a different type, not as that kind.
    """
    request = resolved.request
    semantics = resolved.semantics
    keeps_seats = category == request.commerce_category
    swapped = ProductSearchRequest.model_validate(
        {
            **request.model_dump(),
            "commerce_category": category,
            "commerce_subcategory": subcategory,
            "dimensions": (),
            "planar_dimensions": None,
            "seating_capacity": request.seating_capacity if keeps_seats else None,
        }
    )
    swapped_semantics = ConstraintSemantics.model_validate(
        {
            **semantics.model_dump(),
            "dimensions": (),
            "planar_dimension": None,
            "seating_min": semantics.seating_min if keeps_seats else None,
            "seating_max": semantics.seating_max if keeps_seats else None,
        }
    )
    # Constructed, not copied, so the request/semantics correspondence is
    # validated again (CLAUDE.md 13.2).
    return ResolvedSearch(
        request=swapped,
        semantics=swapped_semantics,
        semantic_preferences=resolved.semantic_preferences,
        semantic_text=resolved.semantic_text,
        unmatched_strict=resolved.unmatched_strict,
        kind_required=resolved.kind_required,
    )


def composed_as_type(composed: ComposedSearch, subcategory: str) -> ComposedSearch:
    """The same composed search for a sibling type, sizes reported as dropped.

    Retyped through :func:`as_type`, the one place that decides what carries
    from one type to another, so the loop and the stock check can never
    disagree about it. Sizes saved for the sibling are not restored: the
    customer gave a size for the type they asked for, and an older one for a
    different type would be a figure they did not state in this request.
    """
    category = composed.resolved.request.commerce_category
    resolved = as_type(composed.resolved, category, subcategory)
    candidate = composed.candidate.model_copy(
        update={"request": resolved.request, "semantics": resolved.semantics}
    )
    return composed.model_copy(
        update={
            "resolved": resolved,
            "candidate": candidate,
            "dropped_constraints": (
                *composed.dropped_constraints,
                *dropped_sizes(composed.resolved),
            ),
            "earlier_sizes_applied": False,
        }
    )
