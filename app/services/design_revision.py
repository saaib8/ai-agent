"""Pure helpers for recomposing a room the customer already has.

Deterministic and synchronous: no repository, no provider, no state mutation.
Resolving which roles the customer named lives on the reference resolver, which
already owns the taxonomy; what remains here is the projection into specialist
input, the normalisation of what was resolved, and the contradiction check.

Two decisions are worth naming.

**Breadth is preserved exactly as resolved.** A bare category and one of its
subcategories are different constraints - one rules out a whole family, the
other one member of it - and collapsing either into the other would silently
widen or narrow what the customer ruled out.

**A contradiction is reported, never resolved.** Keeping a piece whose role is
also being removed is two clear instructions that cannot both hold, and
choosing one would discard the other without saying so.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.schemas.agent_state import RoomDesignNeedState, RoomProjectState
from app.schemas.design import CurrentDesignNeed, ExcludedDesignRole
from app.schemas.product import ProductCandidate


def project_current_plan(
    room: RoomProjectState | None,
) -> tuple[CurrentDesignNeed, ...]:
    """The durable plan as design meaning, in its own order.

    Identity is dropped rather than transformed: `need_id` and
    `rejected_product_ids` have no counterpart on the specialist side, which is
    the point of projecting into a separate type at all.
    """
    needs: Iterable[RoomDesignNeedState] = room.design_needs if room else ()
    return tuple(
        CurrentDesignNeed(
            commerce_category=need.commerce_category,
            commerce_subcategory=need.commerce_subcategory,
            priority=need.priority,
            quantity=need.quantity,
            seating_capacity=need.seating_capacity,
            semantic_intent=need.semantic_intent,
        )
        for need in needs
    )


def deduplicated_exclusions(
    roles: Sequence[ExcludedDesignRole],
) -> tuple[ExcludedDesignRole, ...]:
    """One constraint per exact pair, in first-seen order.

    Exact, so `seating/None` and `seating/sofa` both survive: one rules out all
    seating and the other rules out sofas, and neither implies the other was
    meant. Sending the same hard constraint twice says nothing the first did
    not.
    """
    seen: dict[tuple[str, str | None], ExcludedDesignRole] = {}
    for role in roles:
        seen.setdefault((role.commerce_category, role.commerce_subcategory), role)
    return tuple(seen.values())


def conflicting_role(
    excluded: Sequence[ExcludedDesignRole], preserved: Sequence[ProductCandidate]
) -> ExcludedDesignRole | None:
    """The first exclusion that forbids a piece they asked to keep.

    Compared against **freshly read** commerce facts, because a remembered
    classification could be stale and this decides whether the turn proceeds at
    all.
    """
    for role in excluded:
        for product in preserved:
            if role.excludes(product.commerce.category, product.commerce.subcategory):
                return role
    return None
