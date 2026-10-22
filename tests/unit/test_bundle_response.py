"""The whole-room turn as the customer receives it.

Two halves, and the split is the whole design. The response model is given
counts, enums and two bools, and writes framing. The application renders the
pieces, the quantities, the verified prices and the total. Neither half can do
the other's job: the model has no figure to state, and the renderer has no
prose to write.

Most of these tests defend one of three things — that no catalog value reaches
the model, that a partial room is never described as a complete one, and that
an already-owned piece is never given a price of zero.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_state import RoomProjectState
from app.schemas.bundle import (
    BundleLine,
    BundleStatus,
    BundleUnavailable,
    BundleUnavailableReason,
    RoomBundle,
    TotalUnavailableReason,
    UnmetNeed,
    UnmetReason,
)
from app.schemas.bundle_presentation import GroundedBundleItem
from app.schemas.design import DesignPriority
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import PriceConstraint
from app.schemas.product import CommerceClassification, ProductCandidate
from app.services.bundle_presentation import present_room_bundle


def product(product_id: int = 1, *, price: str = "1200.00", unit: str = "SAR") -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Sofa {product_id}",
        name_arabic="أريكة",
        price_amount=Decimal(price),
        price_unit=unit,
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


def line(
    product_id: int = 1,
    *,
    quantity: int = 1,
    acquisition: BundleAcquisition = BundleAcquisition.TO_BUY,
    locked: bool | None = None,
    depth: int | None = 0,
    price: str = "1200.00",
    need_index: int | None = None,
) -> BundleLine:
    # Only a kept line can be already-owned: a freshly selected product is by
    # definition one the customer is buying.
    kept = locked if locked is not None else acquisition is not BundleAcquisition.TO_BUY
    return BundleLine(
        need_index=need_index,
        product=product(product_id, price=price),
        quantity=quantity,
        locked=kept,
        acquisition=acquisition,
        relaxation_depth=None if kept else depth,
    )


def unmet(
    priority: DesignPriority = DesignPriority.REQUIRED,
    reason: UnmetReason = UnmetReason.NO_CANDIDATES,
    index: int = 9,
) -> UnmetNeed:
    return UnmetNeed(need_index=index, priority=priority, shortfall=1, reason=reason)


def bundle(
    *lines: BundleLine,
    status: BundleStatus = BundleStatus.COMPLETE,
    unmet_needs: tuple[UnmetNeed, ...] = (),
    total: str | None = "1200.00",
) -> RoomBundle:
    return RoomBundle(
        lines=lines,
        status=status,
        unmet=unmet_needs,
        new_spend_total=Decimal(total) if total else None,
        currency="SAR" if total else None,
        total_unavailable=None if total else TotalUnavailableReason.NO_PRICED_LINES,
    )




# ── routing ═════════════════════════════════════════════════════════════════


# ── what the model may see ══════════════════════════════════════════════════














# ── the numeric allowance ═══════════════════════════════════════════════════














# ── the application's half ══════════════════════════════════════════════════




def presentation(outcome: Any, *, budget: str | None = None) -> Any:
    """A room outcome as the customer sees it: ordered by the room it was
    committed into - none for an infeasible one, which is never committed."""
    if not isinstance(outcome, RoomBundle):
        return None
    room = RoomProjectState(
        budget=PriceConstraint.at_most(Decimal(budget), "SAR") if budget else None
    )
    committed = None if outcome.status is BundleStatus.INFEASIBLE else room
    return present_room_bundle(outcome, committed, budget=room.budget)


def test_no_room_no_presentation() -> None:
    assert presentation(None) is None
    assert presentation(
        BundleUnavailable(reason=BundleUnavailableReason.LOCKED_PRICE_UNUSABLE)
    ) is None


def test_every_piece_is_rendered_with_its_verified_price() -> None:
    rendered = presentation(bundle(line(1, price="1200.00"), line(2, price="800.00")))

    assert [item.grounding_ref for item in rendered.items] == [1, 2]
    assert rendered.items[0].unit_price == Decimal("1200.00")
    assert rendered.items[0].price_unit == "SAR"
    assert rendered.items[0].name_english == "Sofa 1"


def test_four_of_one_product_is_one_card_with_a_quantity() -> None:
    rendered = presentation(bundle(line(1, quantity=4, price="500.00")))

    assert len(rendered.items) == 1
    assert rendered.items[0].quantity == 4
    assert rendered.items[0].new_spend_line_total == Decimal("2000.00")


def test_an_already_owned_piece_has_no_new_spend_and_no_fake_price() -> None:
    """Zero would read as a price, and the product does not cost nothing."""
    rendered = presentation(
        bundle(line(1, acquisition=BundleAcquisition.ALREADY_OWNED), total=None)
    )

    item = rendered.items[0]
    assert item.new_spend_line_total is None
    assert item.unit_price == Decimal("1200.00")


def test_one_product_bought_and_owned_stays_two_cards() -> None:
    """Merging them would say the customer is buying something they told us
    they already have."""
    rendered = presentation(
        bundle(
            line(1, acquisition=BundleAcquisition.TO_BUY),
            line(1, acquisition=BundleAcquisition.ALREADY_OWNED),
        )
    )

    assert len(rendered.items) == 2


def test_a_kept_piece_is_not_merged_with_a_suggested_one() -> None:
    rendered = presentation(bundle(line(1, locked=True), line(1, locked=False)))

    assert len(rendered.items) == 2
    assert {item.locked for item in rendered.items} == {True, False}


def test_identical_lines_merge_into_one_card() -> None:
    rendered = presentation(bundle(line(1, quantity=1), line(1, quantity=2)))

    assert len(rendered.items) == 1
    assert rendered.items[0].quantity == 3


@pytest.mark.parametrize("forbidden", ["product_id", "line_id", "store", "rank", "need"])
def test_a_rendered_card_carries_no_identity(forbidden: str) -> None:
    for field in GroundedBundleItem.model_fields:
        assert forbidden not in field, field


def test_the_totals_are_copied_not_recomputed() -> None:
    """A second summation could disagree with the one that decided the package."""
    rendered = presentation(bundle(line(1), line(2), total="9999.00"), budget="15000")

    assert rendered.totals.new_spend_total == Decimal("9999.00")
    assert rendered.totals.currency == "SAR"
    assert rendered.totals.budget_max_amount == Decimal("15000")
    assert rendered.totals.within_budget is True


def test_an_unavailable_total_is_reported_not_reinvented() -> None:
    rendered = presentation(
        bundle(line(1, acquisition=BundleAcquisition.ALREADY_OWNED), total=None)
    )

    assert rendered.totals.new_spend_total is None
    assert rendered.totals.total_unavailable is TotalUnavailableReason.NO_PRICED_LINES


def test_an_infeasible_room_still_renders_what_they_kept() -> None:
    rendered = presentation(
        bundle(line(1, locked=True), status=BundleStatus.INFEASIBLE, unmet_needs=()),
        budget="500",
    )

    assert rendered.status is BundleStatus.INFEASIBLE
    assert len(rendered.items) == 1
    assert rendered.totals.within_budget is False


def test_the_renderer_reaches_no_other_authority() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/services/bundle_presentation.py").read_text()
    for forbidden in (
        "Repository",
        "Pipeline",
        "Pinecone",
        "InteriorDesignAgent",
        "BundleOptimizer",
        "await",
        "float(",
    ):
        assert forbidden not in source, forbidden


# ── fallback wording ════════════════════════════════════════════════════════
