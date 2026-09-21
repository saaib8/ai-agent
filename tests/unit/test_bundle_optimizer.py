"""Choosing one room out of many eligible products.

The regression this service exists to prevent has a shape: the best sofa and
the best table are not the best room. An expensive top-ranked sofa that makes a
required table unaffordable must lose to the cheaper second-ranked one, and no
amount of per-need excellence is allowed to produce a room the customer cannot
buy.

Everything else here defends the boundaries around that decision — locks are
never dropped, acquisition is never guessed, currencies are never converted,
and a partial room is never called complete.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.schemas.acquisition import BundleAcquisition
from app.schemas.bundle import (
    BundleOptimizationRequest,
    BundleStatus,
    BundleUnavailable,
    BundleUnavailableReason,
    LockedBundleProduct,
    RoomBundle,
    TotalUnavailableReason,
    UnmetReason,
)
from app.schemas.design import DesignCategoryNeed, DesignPriority
from app.schemas.design_discovery import (
    DesignDiscoveryResult,
    DesignNeedCandidates,
    DesignNeedSkipReason,
)
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import PriceConstraint, SeatingCapacityConstraint
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.relaxation import StopReason
from app.schemas.resolution import CandidatePoolResult, RankedProductCandidate
from app.services.bundle_optimizer import BundleOptimizer

OPTIMIZER = BundleOptimizer()
SOURCE = Path(__file__).parents[2] / "app/services/bundle_optimizer.py"


def product(
    product_id: int,
    *,
    price: str = "1000.00",
    unit: str = "SAR",
    category: str = "seating",
    subcategory: str | None = "sofa",
    seats: int | None = None,
) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Item {product_id}",
        name_arabic="منتج",
        price_amount=Decimal(price),
        price_unit=unit,
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(
            category=category, subcategory=subcategory, seating_capacity=seats
        ),
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
    seats: SeatingCapacityConstraint | None = None,
) -> DesignCategoryNeed:
    return DesignCategoryNeed(
        commerce_category=category,
        commerce_subcategory=subcategory,
        priority=priority,
        quantity=quantity,
        seating_capacity=seats,
    )


def pool(*products: ProductCandidate, depths: tuple[int, ...] | None = None) -> CandidatePoolResult:
    """Candidates in M9 rank order — the order given is the order ranked."""
    weights = depths or tuple(0 for _ in products)
    return CandidatePoolResult(
        candidates=tuple(
            RankedProductCandidate(product=p, relaxation_depth=d)
            for p, d in zip(products, weights, strict=True)
        ),
        eligible_count=len(products),
        was_relaxed=any(weights),
        stop_reason=StopReason.EXACT_SUFFICIENT,
        semantic_used=False,
    )


def entry(
    index: int, design_need: DesignCategoryNeed, candidates: CandidatePoolResult | None
) -> DesignNeedCandidates:
    if candidates is None:
        return DesignNeedCandidates(
            need_index=index,
            need=design_need,
            skipped=DesignNeedSkipReason.RETAILER_CANNOT_SUPPLY,
        )
    return DesignNeedCandidates(need_index=index, need=design_need, pool=candidates)


def optimize(
    *needs: tuple[DesignCategoryNeed, CandidatePoolResult | None],
    budget: PriceConstraint | None = None,
    locked: tuple[LockedBundleProduct, ...] = (),
) -> Any:
    discovery = DesignDiscoveryResult(
        needs=tuple(entry(i, n, p) for i, (n, p) in enumerate(needs))
    )
    return OPTIMIZER.optimize(
        BundleOptimizationRequest(discovery=discovery, budget=budget, locked=locked)
    )


def sar(amount: str, *, exclusive: bool = False) -> PriceConstraint:
    return PriceConstraint(
        currency="SAR", max_amount=Decimal(amount), max_exclusive=exclusive
    )


def chosen(bundle: RoomBundle) -> dict[int, int]:
    """need_index -> selected product id, for the newly bought lines."""
    return {
        line.need_index: line.product.product_id
        for line in bundle.lines
        if not line.locked and line.need_index is not None
    }


# ── the whole point: a global choice, not per-need top-1 ════════════════════


def test_a_cheaper_lower_ranked_product_wins_when_it_completes_the_room() -> None:
    """The regression M12D exists to prevent, in one test.

    Rank #1 sofa at 8,000 leaves 7,000 — not enough for the required table at
    9,000. Rank #2 at 5,200 leaves 9,800, and the room is complete.
    """
    result = optimize(
        (need("seating", "sofa"), pool(product(1, price="8000"), product(2, price="5200"))),
        (need("tables", "center-table"), pool(
            product(3, price="9000", category="tables", subcategory="center-table")
        )),
        budget=sar("15000"),
    )

    assert result.status is BundleStatus.COMPLETE
    assert chosen(result) == {0: 2, 1: 3}
    assert result.new_spend_total == Decimal("14200")


def test_the_best_ranked_product_wins_when_it_costs_the_room_nothing() -> None:
    result = optimize(
        (need("seating", "sofa"), pool(product(1, price="8000"), product(2, price="5200"))),
        (need("tables", "center-table"), pool(
            product(3, price="2000", category="tables", subcategory="center-table")
        )),
        budget=sar("15000"),
    )

    assert chosen(result) == {0: 1, 1: 3}


def test_needs_are_never_selected_independently_at_top_one() -> None:
    """Three needs where independent top-1 would overspend by 3,000."""
    result = optimize(
        (need("seating", "sofa"), pool(product(1, price="5000"), product(2, price="3000"))),
        (need("tables", "center-table", priority=DesignPriority.REQUIRED),
         pool(product(3, price="4000", category="tables", subcategory="center-table"),
              product(4, price="2000", category="tables", subcategory="center-table"))),
        (need("decor", "carpet", priority=DesignPriority.REQUIRED),
         pool(product(5, price="3000", category="decor", subcategory="carpet"))),
        budget=sar("9000"),
    )

    assert result.status is BundleStatus.COMPLETE
    assert result.new_spend_total <= Decimal("9000")


# ── priority is lexicographic ═══════════════════════════════════════════════


def test_three_required_needs_beat_two_required_plus_recommended() -> None:
    """Counts, compared tier by tier — never a weighted total."""
    result = optimize(
        (need("seating", "sofa"), pool(product(1, price="3000"))),
        (need("tables", "center-table"), pool(
            product(2, price="3000", category="tables", subcategory="center-table")
        )),
        (need("decor", "carpet"), pool(
            product(3, price="3000", category="decor", subcategory="carpet")
        )),
        (need("lighting", "floor-lamp", priority=DesignPriority.RECOMMENDED),
         pool(product(4, price="1000", category="lighting", subcategory="floor-lamp"))),
        budget=sar("9000"),
    )

    assert result.status is BundleStatus.COMPLETE
    assert len(chosen(result)) == 3
    assert [u.priority for u in result.unmet] == [DesignPriority.RECOMMENDED]


def test_optional_is_dropped_before_recommended() -> None:
    result = optimize(
        (need("seating", "sofa"), pool(product(1, price="5000"))),
        (need("tables", "center-table", priority=DesignPriority.RECOMMENDED),
         pool(product(2, price="2000", category="tables", subcategory="center-table"))),
        (need("decor", "carpet", priority=DesignPriority.OPTIONAL),
         pool(product(3, price="2000", category="decor", subcategory="carpet"))),
        budget=sar("7000"),
    )

    assert set(chosen(result)) == {0, 1}
    assert [u.priority for u in result.unmet] == [DesignPriority.OPTIONAL]


def test_plan_order_decides_between_equally_affordable_needs() -> None:
    """Same tier, same cost, only one affordable: the earlier need wins."""
    result = optimize(
        (need("seating", "sofa"), pool(product(1, price="5000"))),
        (need("tables", "center-table"), pool(
            product(2, price="5000", category="tables", subcategory="center-table")
        )),
        budget=sar("5000"),
    )

    assert set(chosen(result)) == {0}
    assert result.unmet[0].need_index == 1


def _two_required_and_one_recommended(budget: str) -> Any:
    """Only one required need is affordable: the dear earlier one at 9,000 or
    the cheaper later one at 4,000. A recommended need costs 1,000."""
    return optimize(
        (need("seating", "sofa"), pool(product(1, price="9000"))),
        (need("tables", "center-table"), pool(
            product(2, price="4000", category="tables", subcategory="center-table")
        )),
        (need("decor", "carpet", priority=DesignPriority.RECOMMENDED),
         pool(product(3, price="1000", category="decor", subcategory="carpet"))),
        budget=sar(budget),
    )


def test_plan_order_wins_when_the_lower_tier_still_fits() -> None:
    """9,000 + 1,000 lands exactly on 10,000, so the earlier required need is
    preferred and the recommended need survives. Both counts are maximal."""
    result = _two_required_and_one_recommended("10000")

    assert set(chosen(result)) == {0, 2}
    assert result.new_spend_total == Decimal("10000")


def test_plan_order_never_costs_a_lower_tier_its_count() -> None:
    """The correction the preflight caught, isolated.

    At 9,500 the earlier required need would leave nothing for the recommended
    one. Plan order ranks *below* every fulfilment count, so the later, cheaper
    required need is taken instead and both tiers keep their maximum. A
    lookahead that checked only its own tier would have taken the 9,000 one and
    silently lost a recommended need.
    """
    result = _two_required_and_one_recommended("9500")

    assert set(chosen(result)) == {1, 2}
    assert result.new_spend_total == Decimal("5000")


# ── quantity ════════════════════════════════════════════════════════════════


def test_quantity_buys_the_same_product_n_times() -> None:
    result = optimize(
        (need("seating", "dining-chair", quantity=4),
         pool(product(1, price="500", subcategory="dining-chair"))),
        budget=sar("2000"),
    )

    line = result.lines[0]
    assert line.quantity == 4
    assert line.line_total == Decimal("2000")
    assert result.new_spend_total == Decimal("2000")


def test_a_residual_is_filled_completely_or_not_at_all() -> None:
    """Three chairs at 500 need 1,500. With 1,400 available, buying two would
    be a partial-fill policy nobody designed."""
    result = optimize(
        (need("seating", "dining-chair", quantity=3),
         pool(product(1, price="500", subcategory="dining-chair"))),
        budget=sar("1400"),
    )

    assert chosen(result) == {}
    assert result.unmet[0].shortfall == 3
    assert result.unmet[0].reason is UnmetReason.BUDGET_EXHAUSTED


def test_quantity_defaults_to_one() -> None:
    result = optimize((need(), pool(product(1))))

    assert result.lines[0].quantity == 1


# ── locks ═══════════════════════════════════════════════════════════════════


def lock(
    p: ProductCandidate,
    acquisition: BundleAcquisition = BundleAcquisition.TO_BUY,
    quantity: int = 1,
) -> LockedBundleProduct:
    return LockedBundleProduct(product=p, acquisition=acquisition, quantity=quantity)


def test_a_lock_satisfies_a_matching_need_and_nothing_is_bought_for_it() -> None:
    result = optimize(
        (need("seating", "sofa"), pool(product(9, price="4000"))),
        locked=(lock(product(1, price="4000")),),
    )

    assert chosen(result) == {}
    assert result.lines[0].locked is True
    assert result.lines[0].need_index == 0
    assert result.status is BundleStatus.COMPLETE


def test_a_sibling_subtype_does_not_satisfy_a_need() -> None:
    """`sectional-sofa` is a different approved value, never substituted."""
    result = optimize(
        (need("seating", "sofa"), pool(product(9, price="4000"))),
        locked=(lock(product(1, subcategory="sectional-sofa")),),
    )

    assert chosen(result) == {0: 9}
    assert any(line.need_index is None and line.locked for line in result.lines)


def test_a_broad_category_need_is_satisfied_by_any_subtype() -> None:
    result = optimize(
        (need("seating", None), pool(product(9))),
        locked=(lock(product(1, subcategory="recliner")),),
    )

    assert chosen(result) == {}


def test_a_lock_must_meet_a_stated_seating_requirement() -> None:
    """A two-seat sofa does not satisfy a plan that reasoned to four."""
    result = optimize(
        (need("seating", "sofa", seats=SeatingCapacityConstraint.at_least(4)),
         pool(product(9, price="4000", seats=4))),
        locked=(lock(product(1, seats=2)),),
    )

    assert chosen(result) == {0: 9}


def test_an_unverified_capacity_satisfies_no_requirement() -> None:
    """NULL means unverified, never "any" (CLAUDE.md 6.1)."""
    result = optimize(
        (need("seating", "sofa", seats=SeatingCapacityConstraint.at_least(4)),
         pool(product(9, price="4000", seats=4))),
        locked=(lock(product(1, seats=None)),),
    )

    assert chosen(result) == {0: 9}


def test_a_lock_covers_part_of_a_quantity_and_the_rest_is_bought() -> None:
    result = optimize(
        (need("seating", "dining-chair", quantity=4),
         pool(product(9, price="500", subcategory="dining-chair"))),
        locked=(lock(product(1, subcategory="dining-chair"), quantity=1),),
    )

    bought = next(line for line in result.lines if not line.locked)
    assert bought.quantity == 3
    assert result.status is BundleStatus.COMPLETE


def test_surplus_locked_units_stay_in_the_room_unmatched() -> None:
    result = optimize(
        (need("seating", "sofa", quantity=1), pool(product(9))),
        locked=(lock(product(1), quantity=3),),
    )

    matched = [line for line in result.lines if line.need_index == 0]
    surplus = [line for line in result.lines if line.need_index is None]
    assert matched[0].quantity == 1
    assert surplus[0].quantity == 2


def test_a_lock_whose_type_is_absent_from_the_plan_is_preserved() -> None:
    result = optimize(
        (need("tables", "center-table"),
         pool(product(9, category="tables", subcategory="center-table"))),
        locked=(lock(product(1, category="decor", subcategory="carpet")),),
    )

    assert any(line.need_index is None and line.locked for line in result.lines)


def test_lock_allocation_is_deterministic_across_orderings() -> None:
    locks = (lock(product(7)), lock(product(3)), lock(product(5)))
    first = optimize((need("seating", "sofa", quantity=2), pool(product(9))), locked=locks)
    second = optimize(
        (need("seating", "sofa", quantity=2), pool(product(9))),
        locked=tuple(reversed(locks)),
    )

    def shape(bundle: RoomBundle) -> list[tuple[int, int]]:
        return sorted(
            (-1 if line.need_index is None else line.need_index, line.product.product_id)
            for line in bundle.lines
        )

    assert shape(first) == shape(second)
    # Locks in product-id order fill the two units; the highest id is surplus.
    assert shape(first) == [(-1, 7), (0, 3), (0, 5)]


def test_no_relaxation_depth_is_invented_for_a_lock() -> None:
    result = optimize((need(), pool(product(9))), locked=(lock(product(1)),))

    locked_line = next(line for line in result.lines if line.locked)
    assert locked_line.relaxation_depth is None


def test_a_selected_line_carries_its_search_provenance() -> None:
    result = optimize((need(), pool(product(9), depths=(2,))))

    assert result.lines[0].relaxation_depth == 2


# ── acquisition ═════════════════════════════════════════════════════════════


def test_an_owned_lock_consumes_no_budget() -> None:
    result = optimize(
        (need("tables", "center-table"),
         pool(product(9, price="5000", category="tables", subcategory="center-table"))),
        locked=(lock(product(1, price="99000"), BundleAcquisition.ALREADY_OWNED),),
        budget=sar("6000"),
    )

    assert result.status is BundleStatus.COMPLETE
    assert result.new_spend_total == Decimal("5000")


def test_a_to_buy_lock_consumes_its_current_price() -> None:
    result = optimize(
        (need("tables", "center-table"),
         pool(product(9, price="5000", category="tables", subcategory="center-table"))),
        locked=(lock(product(1, price="4000")),),
        budget=sar("10000"),
    )

    assert result.new_spend_total == Decimal("9000")


def test_locked_spend_alone_over_budget_is_infeasible() -> None:
    """The lock is not dropped to make the numbers work."""
    result = optimize(
        (need(), pool(product(9, price="100"))),
        locked=(lock(product(1, price="20000")),),
        budget=sar("15000"),
    )

    assert result.status is BundleStatus.INFEASIBLE
    assert all(line.locked for line in result.lines)
    assert result.lines[0].product.product_id == 1


# ── budget forms and endpoints ══════════════════════════════════════════════


@pytest.mark.parametrize(
    ("price", "budget", "exclusive", "bought"),
    [
        ("5000.00", "5000.00", False, True),   # inclusive, exactly on it
        ("5000.01", "5000.00", False, False),  # one cent above
        ("5000.00", "5000.00", True, False),   # exclusive, exactly on it
        ("4999.99", "5000.00", True, True),    # exclusive, one cent below
    ],
)
def test_the_ceiling_is_honoured_at_the_exact_endpoint(
    price: str, budget: str, exclusive: bool, bought: bool
) -> None:
    result = optimize(
        (need(), pool(product(1, price=price))),
        budget=sar(budget, exclusive=exclusive),
    )

    assert bool(chosen(result)) is bought


def _code_only(path: Path) -> str:
    """Identifiers and data literals, with prose excluded.

    Written after a guard flagged the word "narrowest" for containing "rate".
    A docstring explaining that nothing converts a currency must not read as
    evidence that something does.
    """
    tree = ast.parse(path.read_text())
    docstrings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node not in docstrings:
            parts.append(repr(node.value))
        elif isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            parts.append(node.name)
    return " ".join(parts)


def test_no_epsilon_appears_in_the_budget_arithmetic() -> None:
    code = _code_only(SOURCE)

    for forbidden in ("0.01", "epsilon", "1e-"):
        assert forbidden not in code, forbidden


def test_no_monetary_value_passes_through_a_float() -> None:
    """`float` appears once, annotating a latency timestamp. What matters is
    that nothing *converts* with it, so the call is what is forbidden."""
    tree = ast.parse(SOURCE.read_text())

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "float" not in called
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]


@pytest.mark.parametrize(
    "budget",
    [
        PriceConstraint(currency="SAR", min_amount=Decimal("1000")),
        PriceConstraint(
            currency="SAR", min_amount=Decimal("1000"), max_amount=Decimal("5000")
        ),
    ],
    ids=["min-only", "range"],
)
def test_an_unsupported_budget_form_is_refused_not_reinterpreted(
    budget: PriceConstraint,
) -> None:
    result = optimize((need(), pool(product(1))), budget=budget)

    assert isinstance(result, BundleUnavailable)
    assert result.reason is BundleUnavailableReason.UNSUPPORTED_BUDGET_FORM


def test_without_a_budget_every_fillable_need_is_taken_at_rank_one() -> None:
    result = optimize(
        (need("seating", "sofa"), pool(product(1, price="90000"), product(2, price="10"))),
        (need("tables", "center-table", priority=DesignPriority.OPTIONAL),
         pool(product(3, price="70000", category="tables", subcategory="center-table"))),
    )

    assert chosen(result) == {0: 1, 1: 3}
    assert result.status is BundleStatus.COMPLETE


# ── currency ════════════════════════════════════════════════════════════════


def test_a_candidate_in_another_currency_is_excluded_from_a_budgeted_room() -> None:
    result = optimize(
        (need(), pool(product(1, price="100", unit="USD"), product(2, price="4000"))),
        budget=sar("5000"),
    )

    assert chosen(result) == {0: 2}


def test_a_need_with_no_comparable_candidate_says_so_precisely() -> None:
    result = optimize(
        (need(), pool(product(1, price="100", unit="USD"))), budget=sar("5000")
    )

    assert result.unmet[0].reason is UnmetReason.NOT_BUDGET_COMPARABLE


def test_a_to_buy_lock_in_another_currency_makes_the_room_uncomputable() -> None:
    """It cannot be dropped and it cannot be compared."""
    result = optimize(
        (need(), pool(product(9))),
        locked=(lock(product(1, unit="USD")),),
        budget=sar("15000"),
    )

    assert isinstance(result, BundleUnavailable)
    assert result.reason is BundleUnavailableReason.BUDGET_NOT_COMPARABLE


def test_an_owned_lock_in_another_currency_is_fine() -> None:
    """Its price is never read, so its unit cannot make the room uncomputable."""
    result = optimize(
        (need(), pool(product(9, price="1000"))),
        locked=(
            lock(
                product(1, unit="USD", category="decor", subcategory="carpet"),
                BundleAcquisition.ALREADY_OWNED,
            ),
        ),
        budget=sar("15000"),
    )

    assert isinstance(result, RoomBundle)
    assert result.new_spend_total == Decimal("1000")


def test_mixed_units_without_a_budget_give_a_bundle_and_no_total() -> None:
    result = optimize(
        (need("seating", "sofa"), pool(product(1, price="1000", unit="SAR"))),
        (need("tables", "center-table"),
         pool(product(2, price="100", unit="USD", category="tables", subcategory="center-table"))),
    )

    assert result.status is BundleStatus.COMPLETE
    assert result.new_spend_total is None
    assert result.currency is None
    assert result.total_unavailable is TotalUnavailableReason.MIXED_PRICE_UNITS


def test_nothing_converts_or_normalises_a_currency() -> None:
    code = _code_only(SOURCE)

    for forbidden in ("upper", "lower", "casefold", "convert", "exchange"):
        assert forbidden not in code, forbidden


def test_a_case_differing_unit_is_a_different_unit() -> None:
    """Byte-for-byte: normalising would be a guess about an unvalidated column."""
    result = optimize(
        (need(), pool(product(1, price="100", unit="sar"))), budget=sar("5000")
    )

    assert result.unmet[0].reason is UnmetReason.NOT_BUDGET_COMPARABLE


def test_a_room_of_only_owned_pieces_reports_no_total_without_a_budget() -> None:
    result = optimize(
        (need(), None),
        locked=(lock(product(1), BundleAcquisition.ALREADY_OWNED),),
    )

    assert result.new_spend_total is None
    assert result.total_unavailable is TotalUnavailableReason.NO_PRICED_LINES


# ── price validity ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("price", ["0.00", "-10.00"])
def test_a_non_positive_price_is_never_spendable(price: str) -> None:
    result = optimize((need(), pool(product(1, price=price), product(2, price="500"))))

    assert chosen(result) == {0: 2}


def test_a_blank_price_unit_is_never_spendable() -> None:
    result = optimize((need(), pool(product(1, unit="   "), product(2, price="500"))))

    assert chosen(result) == {0: 2}


def test_no_budget_does_not_make_an_invalid_price_valid() -> None:
    """Absence of a ceiling is absence of a ceiling, not permission."""
    result = optimize((need(), pool(product(1, price="0.00"))))

    assert chosen(result) == {}
    assert result.unmet[0].reason is UnmetReason.NO_USABLE_PRICE


def test_a_to_buy_lock_without_a_usable_price_is_uncomputable() -> None:
    result = optimize((need(), pool(product(9))), locked=(lock(product(1, price="0.00")),))

    assert isinstance(result, BundleUnavailable)
    assert result.reason is BundleUnavailableReason.LOCKED_PRICE_UNUSABLE


def test_an_owned_lock_without_a_usable_price_is_preserved() -> None:
    result = optimize(
        (need("tables", "center-table"),
         pool(product(9, category="tables", subcategory="center-table"))),
        locked=(lock(product(1, price="0.00"), BundleAcquisition.ALREADY_OWNED),),
    )

    assert isinstance(result, RoomBundle)
    assert any(line.locked for line in result.lines)


def test_every_monetary_value_is_decimal() -> None:
    result = optimize((need(quantity=3), pool(product(1, price="333.33"))))

    assert isinstance(result.new_spend_total, Decimal)
    assert result.new_spend_total == Decimal("999.99")


# ── zero candidates ═════════════════════════════════════════════════════════


def test_a_required_need_with_no_candidates_makes_the_room_partial() -> None:
    result = optimize(
        (need("seating", "sofa"), pool()),
        (need("tables", "center-table"),
         pool(product(2, category="tables", subcategory="center-table"))),
    )

    assert result.status is BundleStatus.PARTIAL
    assert result.unmet[0].reason is UnmetReason.NO_CANDIDATES
    assert chosen(result) == {1: 2}


def test_a_skipped_need_is_reported_as_having_no_candidates() -> None:
    result = optimize((need(), None))

    assert result.unmet[0].reason is UnmetReason.NO_CANDIDATES


@pytest.mark.parametrize(
    "priority", [DesignPriority.RECOMMENDED, DesignPriority.OPTIONAL]
)
def test_a_lower_priority_gap_leaves_the_room_complete(
    priority: DesignPriority,
) -> None:
    result = optimize(
        (need("seating", "sofa"), pool(product(1))),
        (need("tables", "center-table", priority=priority), pool()),
    )

    assert result.status is BundleStatus.COMPLETE
    assert result.unmet[0].priority is priority


# ── duplicate SKUs ══════════════════════════════════════════════════════════


def test_two_needs_may_choose_the_same_product() -> None:
    shared = product(1, price="500", category="seating", subcategory=None)
    result = optimize(
        (need("seating", None), pool(shared)),
        (need("seating", None, priority=DesignPriority.RECOMMENDED), pool(shared)),
    )

    assert chosen(result) == {0: 1, 1: 1}
    assert result.new_spend_total == Decimal("1000")


# ── boundaries ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "forbidden",
    [
        "AgentStateV1",
        "RoomProjectState",
        "ProductRepository",
        "ProductSearchPipeline",
        "DesignDiscoveryService",
        "Pinecone",
        "SemanticRankingService",
        "StructuredLLMClient",
        "InteriorDesignAgent",
        "RetailerContext",
        "commit_search_results",
        "store_id",
    ],
)
def test_the_optimiser_reaches_no_other_machinery(forbidden: str) -> None:
    assert forbidden not in SOURCE.read_text()


def test_the_optimiser_awaits_nothing() -> None:
    """Deterministic and synchronous: there is nothing to wait for."""
    tree = ast.parse(SOURCE.read_text())

    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Await)]
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]


def test_the_optimiser_holds_no_room_composition_knowledge() -> None:
    tree = ast.parse(SOURCE.read_text())
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } - {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    for category in ("seating", "tables", "lighting", "sofa", "carpet"):
        assert not any(category in literal for literal in literals), category


def test_the_request_cannot_carry_state_or_scope() -> None:
    assert set(BundleOptimizationRequest.model_fields) == {
        "discovery",
        "budget",
        "locked",
    }


def test_the_design_discovery_path_still_emits_the_default_sort() -> None:
    """M12D trusts M9's ranked order, and M9 places relaxation depth before
    similarity only on the default key. An explicit sort on this path would
    silently invalidate that (see semantic_ranking `_by_explicit_sort`)."""
    bridge = (Path(__file__).parents[2] / "app/services/design_discovery.py").read_text()
    tree = ast.parse(bridge)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ProductSearchRequest"
        ):
            assert all(k.arg != "sort" for k in node.keywords)
    assert "ProductSort" not in bridge
