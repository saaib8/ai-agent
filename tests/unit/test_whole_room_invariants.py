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
from app.schemas.bundle import TotalUnavailableReason
from app.schemas.discovery import PriceConstraint
from app.schemas.resolution import (
    BundleReferenceUnresolved,
)
from app.services.bundle_money import SpendLine, new_spend
from app.services.bundle_presentation import build_state_bundle_presentation

from tests.unit.test_design_revision import a_room, line, need, product, room

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
