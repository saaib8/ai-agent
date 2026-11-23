"""A step up, or down, from one product the customer pointed at
(docs/upselling-plan.md).

A step up is the same type and the same look, dearer within reach, and
different upward on at least one fact the store lists: another main material,
a noticeably longer floor side, more seats, a longer warranty, or a rating
resting on enough reviews. Whether a product qualifies is decided by the
database (`ProductSearchRequest.step_up`); this module only builds that request
from the product's own facts, and words what differs.

Nothing here says "better". Material has no up, so it is only ever "another
material"; the customer judges whether a difference is worth the price.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.core.config import UpsellSettings
from app.schemas.discovery import (
    PriceConstraint,
    ProductSearchRequest,
    ProductSort,
    SeatingCapacityConstraint,
    StepUpBasis,
)
from app.schemas.grounding import GroundedProduct
from app.schemas.product import ProductCandidate
from app.schemas.query import ConstraintSemantics, ConstraintStrength

Product = GroundedProduct | ProductCandidate


def plain(value: Decimal) -> str:
    """A figure as a person writes it: 220, 92.5 - never 2.2E+2."""
    return format(value.normalize(), "f")


class PriceStep(StrEnum):
    HIGHER = "higher"
    LOWER = "lower"


def longer_side_cm(product: Product) -> Decimal | None:
    """The piece's longer floor side, where both floor sides are listed."""
    sides = [v for v in (product.dimensions.length_cm, product.dimensions.width_cm) if v]
    return max(sides) if len(sides) == 2 else None


def step_basis(product: ProductCandidate, settings: UpsellSettings) -> StepUpBasis:
    """What another product must differ from `product` on to step up from it -
    only facts this product lists, and only where they make sense: a store's
    seat count of 0 or a rating above 5 is no fact to step up from."""
    side = longer_side_cm(product)
    listing = product.listing
    seats = product.commerce.seating_capacity
    warranty = listing.warranty_months
    return StepUpBasis(
        from_product_id=product.product_id,
        material_other_than=listing.material,
        longer_side_at_least_cm=(side * (1 + settings.bigger_share)) if side else None,
        longer_side_not_below_cm=(side * (1 - settings.bigger_share)) if side else None,
        seats_more_than=seats if seats and seats > 0 else None,
        warranty_more_than=warranty if warranty is not None and warranty >= 0 else None,
        rated_above=_own_rating(listing, settings),
        rated_at_least=settings.unrated_min_rating,
        min_reviews=settings.min_reviews,
    )


def _own_rating(listing: Any, settings: UpsellSettings) -> Decimal | None:
    """The product's rating where it rests on enough reviews and can be beaten."""
    rating = listing.rating
    if rating is None or (listing.review_count or 0) < settings.min_reviews:
        return None
    return rating if 0 < rating < 5 else None


def stepped_search(
    request: ProductSearchRequest,
    semantics: ConstraintSemantics,
    product: ProductCandidate,
    step: PriceStep,
    settings: UpsellSettings,
    *,
    budget_at_most: Decimal | None = None,
) -> tuple[ProductSearchRequest, ConstraintSemantics] | None:
    """`request` - a search for products like `product` - priced a step above
    or below it. None when no price is left between the step and their budget.

    The colour stays a requirement: to a customer it is the look. The styles
    only order the cards (they stay the search's leanings), so a lamp with three
    styles is not left with no step up for want of all three at once.

    Higher keeps the seats from falling (at least as many) and asks for a
    listed difference; lower is only cheaper, by a real step."""
    price = product.price_amount
    currency = product.price_unit
    if not currency.strip() or price <= 0:
        return None
    if step is PriceStep.HIGHER:
        floor = price * (1 + settings.min_step_share)
        ceiling = price * settings.max_multiple
        if budget_at_most is not None:
            ceiling = min(ceiling, budget_at_most)
        if ceiling < floor:
            return None
        seats = product.commerce.seating_capacity
        seats = seats if seats and seats > 0 else None
        stepped = request.model_copy(
            update={
                # The nearest step up first: the one an agent mentions is the
                # one at the top when it is shown.
                "sort": ProductSort.PRICE_ASC,
                "styles_all_of": (),
                "price": PriceConstraint.between(floor, ceiling, currency),
                "seating_capacity": (
                    SeatingCapacityConstraint.at_least(seats) if seats is not None else None
                ),
                "step_up": step_basis(product, settings),
            }
        )
        return stepped, semantics.model_copy(
            update={
                "price_min": ConstraintStrength.LOCKED,
                "price_max": ConstraintStrength.LOCKED,
                "seating_min": ConstraintStrength.LOCKED if seats is not None else None,
                "seating_max": None,
            }
        )
    cheaper = request.model_copy(
        update={
            # The nearest step down first.
            "sort": ProductSort.PRICE_DESC,
            "styles_all_of": (),
            "price": PriceConstraint.at_most(price * (1 - settings.min_step_share), currency),
        }
    )
    return cheaper, semantics.model_copy(
        update={"price_min": None, "price_max": ConstraintStrength.LOCKED}
    )


def differences(
    anchor: ProductCandidate, other: Product, settings: UpsellSettings
) -> list[dict[str, Any]]:
    """What `other` lists that differs upward from `anchor` - each as what it
    is and what it is instead of, never as better or worse."""
    found: list[dict[str, Any]] = []
    mine, theirs = other.listing, anchor.listing
    if mine.material and theirs.material and mine.material != theirs.material:
        found.append({"what": "main_material", "is": mine.material, "instead_of": theirs.material})
    side, anchor_side = longer_side_cm(other), longer_side_cm(anchor)
    if side and anchor_side and side >= anchor_side * (1 + settings.bigger_share):
        found.append(
            {"what": "longer_side_cm", "is": plain(side), "instead_of": plain(anchor_side)}
        )
    seats, anchor_seats = other.commerce.seating_capacity, anchor.commerce.seating_capacity
    if seats and anchor_seats and seats > anchor_seats:
        found.append({"what": "seats", "is": str(seats), "instead_of": str(anchor_seats)})
    warranty, anchor_warranty = mine.warranty_months, theirs.warranty_months
    if warranty and anchor_warranty is not None and warranty > anchor_warranty:
        found.append(
            {
                "what": "warranty_months",
                "is": str(warranty),
                "instead_of": str(anchor_warranty),
            }
        )
    if (
        mine.rating is not None
        and mine.review_count is not None
        and _rated_above(mine.rating, mine.review_count, theirs, settings)
    ):
        rating: dict[str, Any] = {
            "what": "rating",
            "is": plain(mine.rating),
            "reviews": str(mine.review_count),
        }
        own = _own_rating(theirs, settings)
        if own is not None:
            rating["instead_of"] = plain(own)
        found.append(rating)
    return found


def _rated_above(
    rating: Decimal | None, reviews: int | None, anchor: Any, settings: UpsellSettings
) -> bool:
    """The same rule the database applies (`_step_up_clause`)."""
    if rating is None or (reviews or 0) < settings.min_reviews:
        return False
    theirs = _own_rating(anchor, settings)
    return rating > theirs if theirs is not None else rating >= settings.unrated_min_rating


def price_gap(anchor: ProductCandidate, other: Product) -> dict[str, str]:
    """How much more, or less, `other` costs - worked out here, never by the
    model. Empty across currencies, which are never compared."""
    if other.price_unit != anchor.price_unit:
        return {}
    gap = other.price_amount - anchor.price_amount
    return {"price_more": plain(gap)} if gap >= 0 else {"price_less": plain(-gap)}


def step_facts(
    anchor: ProductCandidate, others: Sequence[Product], settings: UpsellSettings
) -> list[dict[str, Any]]:
    """Each card beside the product it was stepped from: the price gap and what
    differs, by its place on screen."""
    return [
        {
            "card": getattr(other, "presented_ordinal", None) or n,
            **price_gap(anchor, other),
            "differs": differences(anchor, other, settings),
        }
        for n, other in enumerate(others, start=1)
    ]
