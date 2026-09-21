"""Rendering a selected room for the customer.

A pure projection: an outcome in, a payload out, no I/O and no second authority.
It calls no repository, no search, no index, no optimiser and no model. The
products it renders are the ones the optimiser already verified in this same
turn, so re-reading PostgreSQL here would only duplicate an authority read that
has already happened.

Two decisions are worth naming, because either could quietly mislead:

* **Quantity is a number on a card, not a repeated card.** Four of one chair is
  one item with a quantity of four.
* **Lines merge only when nothing distinguishing is lost.** Two lines for the
  same product combine only if they agree on acquisition *and* lock status;
  a piece being kept and a piece being suggested are two different things to
  say about the room, however identical the product.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.core.exceptions import BundlePresentationError
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_state import RoomProjectState
from app.schemas.agent_turn import CustomerTurnResult
from app.schemas.bundle import BundleLine, BundleStatus, RoomBundle, TotalUnavailableReason
from app.schemas.bundle_presentation import (
    GroundedBundleItem,
    GroundedBundlePresentation,
    GroundedBundleTotals,
)
from app.schemas.discovery import PriceConstraint
from app.schemas.product import ProductCandidate
from app.services.bundle_cards import group_bundle_cards
from app.services.bundle_money import SpendLine, new_spend


def build_bundle_presentation(
    result: CustomerTurnResult,
) -> GroundedBundlePresentation | None:
    """The room as the customer sees it, or None when no room was selected.

    Built for every real bundle - complete, partial and infeasible alike. An
    infeasible one still renders the pieces they asked to keep, which is the
    only way to explain what conflicts with what.
    """
    outcome = result.bundle_outcome
    if not isinstance(outcome, RoomBundle):
        return None

    room = result.state.room_project
    return GroundedBundlePresentation(
        status=outcome.status,
        # Ordering comes from the state this outcome was committed into; the
        # budget comes from the room either way, because it is what the
        # customer said rather than anything the commit decided.
        items=_items(outcome, _committed_room(result, outcome)),
        totals=_totals(outcome, room.budget if room else None),
    )


def _committed_room(
    result: CustomerTurnResult, outcome: RoomBundle
) -> RoomProjectState | None:
    """The state this outcome was committed into, or None if it was not.

    An infeasible package is never committed, so current state describes a
    different room and must not be used to order its explanatory rendering.
    """
    if outcome.status is BundleStatus.INFEASIBLE:
        return None
    return result.state.room_project


def _items(
    bundle: RoomBundle, room: RoomProjectState | None
) -> tuple[GroundedBundleItem, ...]:
    """One card per distinguishable piece, in the room's canonical order.

    Grouped and ordered by the shared card rule over **committed state**, so
    the sequence shown now is the sequence that can be rebuilt on a later turn.
    Before M12E-4B this grouped the outcome's own lines, whose locked entries
    the optimiser sorts by product id while the commit preserves state order -
    the two could disagree, and "the second one" would then mean two things.

    Facts come from the outcome, which verified them in this same execution;
    nothing is re-read.

    A room that was not committed - an infeasible one - has no state to group,
    so its explanatory rendering falls back to the outcome's own order.
    """
    lines = list(bundle.lines)
    if room is None or not room.bundle_items:
        return _cards(_outcome_groups(lines))

    by_key: dict[tuple[int, BundleAcquisition, bool], BundleLine] = {}
    for line in lines:
        by_key.setdefault((line.product.product_id, line.acquisition, line.locked), line)

    groups: list[tuple[BundleLine, int]] = []
    for card in group_bundle_cards(room.bundle_items):
        facts = by_key.get((card.product_id, card.acquisition, card.locked))
        if facts is None:
            # Every committed line came from this outcome. A card with no
            # verified facts behind it means the two describe different rooms.
            raise BundlePresentationError(
                detail="a committed bundle line has no verified product facts",
                public_message="I wasn't able to show that room just now.",
            )
        groups.append((facts, card.quantity))
    return _cards(groups)


def _outcome_groups(lines: Sequence[BundleLine]) -> list[tuple[BundleLine, int]]:
    """The outcome's own grouping, for a room that was never committed."""
    merged: dict[tuple[int, BundleAcquisition, bool], list[BundleLine]] = {}
    for line in lines:
        merged.setdefault(
            (line.product.product_id, line.acquisition, line.locked), []
        ).append(line)
    return [
        (members[0], sum(line.quantity for line in members))
        for members in merged.values()
    ]


def _cards(groups: Sequence[tuple[BundleLine, int]]) -> tuple[GroundedBundleItem, ...]:
    return tuple(
        GroundedBundleItem(
            grounding_ref=position,
            name_english=line.product.name_english,
            image_url=line.product.image_url,
            product_url=line.product.product_url,
            commerce=line.product.commerce,
            quantity=quantity,
            acquisition=line.acquisition,
            locked=line.locked,
            unit_price=line.product.price_amount,
            price_unit=line.product.price_unit,
            new_spend_line_total=(
                line.product.price_amount * quantity
                if line.acquisition is BundleAcquisition.TO_BUY
                else None
            ),
        )
        for position, (line, quantity) in enumerate(groups, start=1)
    )


def _totals(
    bundle: RoomBundle, budget: PriceConstraint | None
) -> GroundedBundleTotals:
    """The arithmetic, copied rather than recomputed.

    `new_spend_total` and its unavailability both come from the outcome that
    decided the package. Summing the rendered cards again would risk a figure
    that disagrees with the one the optimiser respected - and cards can merge,
    which is exactly the kind of difference that would go unnoticed.
    """
    return GroundedBundleTotals(
        new_spend_total=bundle.new_spend_total,
        currency=bundle.currency,
        total_unavailable=bundle.total_unavailable,
        budget_max_amount=budget.max_amount if budget else None,
        budget_currency=budget.currency if budget else None,
        budget_max_exclusive=budget.max_exclusive if budget else False,
        within_budget=_within(bundle, budget),
    )


def _within(bundle: RoomBundle, budget: PriceConstraint | None) -> bool | None:
    """Whether the package obeys a budget the customer actually gave.

    Read from the same comparison the optimiser made, not recomputed from the
    rendered figures: it treats the ceiling as a hard constraint, so anything
    it produced other than an infeasible package is inside.
    """
    if budget is None or budget.max_amount is None:
        return None
    return bundle.status is not BundleStatus.INFEASIBLE


# ── the room, rebuilt from state ────────────────────────────────────────────


def build_state_bundle_presentation(
    room: RoomProjectState | None, products: Sequence[ProductCandidate]
) -> GroundedBundlePresentation | None:
    """The current room, rendered without an optimisation behind it.

    What a local change needs: locking a piece produces no `RoomBundle`, and
    fabricating one to have something to render would invent a status, unmet
    needs and a feasibility claim that nobody established.

    So the pieces come from state and their facts from a fresh read, grouped
    and ordered by the same card rule the committed rendering uses - which is
    what makes this sequence the one the customer already saw.

    **All or nothing.** If any active card's product cannot be read, no room is
    returned at all. Dropping the card would show a room missing a piece the
    customer has, and would renumber everything after it - so "the third one"
    in the reply would no longer be the third one they were shown. An
    incomplete room is worse than none.

    State is never touched here: a piece the catalog cannot currently return is
    still in the customer's room, and removing it is not a rendering decision.
    """
    if room is None or not room.bundle_items:
        return None

    by_id = {product.product_id: product for product in products}
    items: list[GroundedBundleItem] = []
    spend: list[SpendLine] = []
    for card in group_bundle_cards(room.bundle_items):
        product = by_id.get(card.product_id)
        if product is None:
            raise BundlePresentationError(
                detail="a bundle line has no current product facts",
                public_message="I wasn't able to show that room just now.",
            )
        buying = card.acquisition is BundleAcquisition.TO_BUY
        items.append(
            GroundedBundleItem(
                grounding_ref=len(items) + 1,
                name_english=product.name_english,
                image_url=product.image_url,
                product_url=product.product_url,
                commerce=product.commerce,
                quantity=card.quantity,
                acquisition=card.acquisition,
                locked=card.locked,
                unit_price=product.price_amount,
                price_unit=product.price_unit,
                new_spend_line_total=(
                    product.price_amount * card.quantity if buying else None
                ),
            )
        )
        spend.append(
            SpendLine(
                unit_price=product.price_amount,
                price_unit=product.price_unit,
                quantity=card.quantity,
                acquisition=card.acquisition,
            )
        )

    total, currency, unavailable = new_spend(spend)
    return GroundedBundlePresentation(
        status=BundleStatus.COMPLETE if items else BundleStatus.PARTIAL,
        items=tuple(items),
        totals=_state_totals(total, currency, unavailable, room.budget),
    )


def _state_totals(
    total: Decimal | None,
    currency: str | None,
    unavailable: TotalUnavailableReason | None,
    budget: PriceConstraint | None,
) -> GroundedBundleTotals:
    """The room's arithmetic against the customer's own budget.

    Compliance is claimed only where it can be checked: a budget M12D does not
    support - a minimum, or a range - is rendered as the customer stated it and
    answered with "unknown" rather than with a verdict nothing computed.
    """
    supported = budget is not None and budget.max_amount is not None and budget.min_amount is None
    within: bool | None = None
    if supported and total is not None and currency is not None:
        assert budget is not None and budget.max_amount is not None
        if currency == budget.currency:
            within = (
                total < budget.max_amount
                if budget.max_exclusive
                else total <= budget.max_amount
            )
    # Only a ceiling is rendered, with its currency. A budget stated some other
    # way has no maximum to show, and a minimum shown as one would be a
    # different number from the one the customer gave.
    ceiling = budget.max_amount if budget else None
    return GroundedBundleTotals(
        new_spend_total=total,
        currency=currency,
        total_unavailable=unavailable,
        budget_max_amount=ceiling,
        budget_currency=budget.currency if budget and ceiling is not None else None,
        budget_max_exclusive=budget.max_exclusive if budget else False,
        within_budget=within,
    )
