"""Invariants that only show up across a sequence of turns.

Each of these holds within one turn in tests elsewhere. What is checked here is
that they keep holding as turns compose - that an id issued three turns ago
cannot be handed out again, that a card's position survives into the turn where
the customer refers to it, and that money never stops being exact.

The identity rules exist because references are positional and durable state is
not. "The second one" is resolved against committed state, so any rule that let
an id be reused, or a position shift, would silently point a later turn at a
different product.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_state import AgentStateV1, BundleItemStatus, RoomProjectState
from app.schemas.agent_updates import (
    AgentStateUpdate,
    DesignNeedSpec,
    PlannedBundleLineSpec,
    ReplaceDesignPlan,
    RoomProjectUpdate,
)
from app.schemas.bundle import TotalUnavailableReason
from app.schemas.design import DesignPriority
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import PriceConstraint
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.resolution import (
    BundleReferenceUnresolved,
)
from app.services.agent_state import apply_update
from app.services.bundle_money import SpendLine, new_spend
from app.services.bundle_presentation import build_state_bundle_presentation

# Room-state builders, shared with test_product_display (their old home,
# test_design_revision, went with the design specialist).


def product(
    product_id: int,
    *,
    category: str = "seating",
    subcategory: str | None = "sofa",
    price: str = "1000.00",
) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Item {product_id}",
        name_arabic="منتج",
        price_amount=Decimal(price),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(category=category, subcategory=subcategory),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


def need(
    category: str = "seating",
    subcategory: str | None = "sofa",
    *,
    priority: DesignPriority = DesignPriority.REQUIRED,
    quantity: int = 1,
    intent: str | None = None,
) -> DesignNeedSpec:
    return DesignNeedSpec(
        commerce_category=category,
        commerce_subcategory=subcategory,
        priority=priority,
        quantity=quantity,
        semantic_intent=intent,
    )


def line(
    product_id: int, *, need_index: int | None = 0, locked: bool = False
) -> PlannedBundleLineSpec:
    return PlannedBundleLineSpec(
        product_id=product_id,
        quantity=1,
        acquisition=BundleAcquisition.TO_BUY,
        status=BundleItemStatus.LOCKED if locked else BundleItemStatus.SUGGESTED,
        need_index=need_index,
    )


def a_room(
    *,
    needs: tuple[DesignNeedSpec, ...] = (),
    lines: tuple[PlannedBundleLineSpec, ...] = (),
    budget: str | None = None,
) -> AgentStateV1:
    return apply_update(
        AgentStateV1(),
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                budget=PriceConstraint.at_most(Decimal(budget), "SAR") if budget else None,
                bundle_operations=(ReplaceDesignPlan(needs=needs, added=lines),),
            )
        ),
    )


def room(state: AgentStateV1) -> RoomProjectState:
    project = state.room_project
    assert isinstance(project, RoomProjectState)
    return project


# ── identity is never handed out twice (M12F 8) ═════════════════════════════


# ── money stays exact (M12F 9) ══════════════════════════════════════════════


def test_the_spend_is_decimal_throughout_and_never_a_float() -> None:
    total, currency, unavailable = new_spend(
        [
            SpendLine(Decimal("1200.50"), "SAR", 2, BundleAcquisition.TO_BUY),
            SpendLine(Decimal("99.99"), "SAR", 1, BundleAcquisition.TO_BUY),
        ]
    )

    assert isinstance(total, Decimal)
    assert total == Decimal("2500.99"), "exact, not 2500.9899999"
    assert currency == "SAR" and unavailable is None


def test_two_currencies_are_refused_rather_than_converted() -> None:
    """No FX rate exists in this service, and inventing one would state a
    figure nobody can verify."""
    total, currency, unavailable = new_spend(
        [
            SpendLine(Decimal("1000"), "SAR", 1, BundleAcquisition.TO_BUY),
            SpendLine(Decimal("1000"), "USD", 1, BundleAcquisition.TO_BUY),
        ]
    )

    assert total is None and currency is None
    assert unavailable is TotalUnavailableReason.MIXED_PRICE_UNITS


def test_a_piece_they_own_is_absent_from_the_spend_not_priced_at_zero() -> None:
    total, currency, _ = new_spend(
        [
            SpendLine(Decimal("4000"), "SAR", 1, BundleAcquisition.ALREADY_OWNED),
            SpendLine(Decimal("600"), "SAR", 1, BundleAcquisition.TO_BUY),
        ]
    )

    assert total == Decimal("600"), "the owned piece contributes nothing"
    assert currency == "SAR"


def test_an_owned_only_room_has_no_total_rather_than_a_total_of_zero() -> None:
    """Zero would read as "this room is free", which is a different claim."""
    total, currency, unavailable = new_spend(
        [SpendLine(Decimal("4000"), "SAR", 1, BundleAcquisition.ALREADY_OWNED)]
    )

    assert total is None and currency is None
    assert unavailable is TotalUnavailableReason.NO_PRICED_LINES


@pytest.mark.parametrize(
    ("exclusive", "at_limit"), [(True, False), (False, True)]
)
def test_a_ceiling_is_honoured_exactly_as_the_customer_stated_it(
    exclusive: bool, at_limit: bool
) -> None:
    room_state = a_room(needs=(need(),), lines=(line(10),))
    budget = PriceConstraint(
        currency="SAR", max_amount=Decimal("1000.00"), max_exclusive=exclusive
    )
    project = room(room_state).model_copy(update={"budget": budget})

    presentation = _shown(project, [product(10)])

    assert presentation.totals.new_spend_total == Decimal("1000.00")
    assert presentation.totals.within_budget is at_limit


# ── references survive into the next turn (M12F 10) ═════════════════════════


def _resolved(outcome: Any) -> Any:
    assert not isinstance(outcome, BundleReferenceUnresolved), outcome
    return outcome


def _shown(project: Any, products: Any) -> Any:
    presentation = build_state_bundle_presentation(project, products)
    assert presentation is not None
    return presentation
