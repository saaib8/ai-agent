"""Choosing one whole room out of many eligible products.

The third of three decisions, and deliberately the narrowest. The specialist
decided *what* the room needs; discovery decided *which* products qualify; this
decides *which combination* to take. It consults no model, runs no search,
touches no repository and writes no state - everything it reasons over arrived
already verified.

**Why a combination cannot be chosen one need at a time.** The best sofa and
the best table are not the best room: an SAR 8,000 sofa that ranks first can
make a required table unaffordable, when the SAR 5,200 sofa ranked second
leaves room for both. Independent top-1 selection is the defect this service
exists to prevent (CLAUDE.md 27).

The objective is strictly lexicographic - never a weighted score:

    hard      locks preserved, acquisition honoured, budget and currency rules,
              a need's residual filled completely or not at all
    1-3       maximise fully satisfied REQUIRED, then RECOMMENDED, then OPTIONAL
    4         within a tier, prefer earlier needs in design plan order
    5         for that need set, minimise each need's M9 rank, read in plan order
    6         product id, for stability only

**The algorithm's correctness depends on V1 independence.** Needs share exactly
one resource - the budget - and nothing else: no cross-need SKU exclusivity (a
product may be chosen for two needs), no shared inventory count, no
compatibility constraint between products, no geometric placement. That is what
makes "the cheapest completion of the remaining needs" exactly computable, and
the greedy lookahead below optimal rather than merely plausible. **If a later
milestone introduces any coupling, this algorithm must be revisited.**

No fit claim is made anywhere. M12A establishes only that an object taller than
the ceiling does not go in, and no targeted horizontal applicability contract
exists, so this service asserts nothing about whether the room holds what it
selected (CLAUDE.md 15.1).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from decimal import Decimal

from app.core.logging import get_logger
from app.schemas.acquisition import BundleAcquisition
from app.schemas.bundle import (
    BundleLine,
    BundleOptimizationOutcome,
    BundleOptimizationRequest,
    BundleStatus,
    BundleUnavailable,
    BundleUnavailableReason,
    LockedBundleProduct,
    RoomBundle,
    TotalUnavailableReason,
    UnmetNeed,
    UnmetReason,
)
from app.schemas.design import DesignCategoryNeed, DesignPriority
from app.schemas.design_discovery import DesignNeedCandidates
from app.schemas.discovery import PriceConstraint
from app.schemas.product import ProductCandidate
from app.schemas.resolution import RankedProductCandidate
from app.services.bundle_money import SpendLine, new_spend

logger = get_logger(__name__)

_TIERS = (DesignPriority.REQUIRED, DesignPriority.RECOMMENDED, DesignPriority.OPTIONAL)
"""Optimised strictly in this order. Three satisfied required needs beat two
required plus five recommended, by construction rather than by weighting."""


class _Residual:
    """One need after locks, as the optimiser needs to see it."""

    __slots__ = ("candidates", "index", "need", "quantity", "reason")

    def __init__(
        self,
        need: DesignCategoryNeed,
        index: int,
        quantity: int,
        candidates: tuple[RankedProductCandidate, ...],
        reason: UnmetReason,
    ) -> None:
        self.need = need
        self.index = index
        self.quantity = quantity
        self.candidates = candidates
        """Usable candidates in M9 rank order. Order is authoritative and is
        never re-sorted: M9 already placed relaxation depth ahead of semantic
        similarity, so the first that fits is the most faithful that fits."""

        self.reason = reason
        """Why this need would go unmet if nothing were selected for it."""

    @property
    def priority(self) -> DesignPriority:
        return self.need.priority

    @property
    def minimum_cost(self) -> Decimal | None:
        """The cheapest complete residual. None when it cannot be filled."""
        if not self.candidates:
            return None
        cheapest = min(c.product.price_amount for c in self.candidates)
        return cheapest * self.quantity


class BundleOptimizer:
    """A verified room plan and its candidates in, one room out."""

    def optimize(
        self, request: BundleOptimizationRequest
    ) -> BundleOptimizationOutcome:
        """Select the whole room, deterministically.

        Returns a bundle - possibly a partial or an infeasible one, both of
        which are honest answers - or a refusal when the arithmetic itself
        cannot be done.
        """
        started = time.perf_counter()
        budget = request.budget
        if budget is not None and budget.min_amount is not None:
            # A floor, or a range. Honouring only the ceiling would answer a
            # different question, and there is no "spend at least" objective.
            return _unavailable(BundleUnavailableReason.UNSUPPORTED_BUDGET_FORM)

        locked_lines, covered = _allocate_locks(request)
        blocked = _locked_spend_problem(request.locked, budget)
        if blocked is not None:
            return _unavailable(blocked)

        mandatory = _to_buy_total(request.locked)
        if budget is not None and not _within(mandatory, budget):
            return self._infeasible(request, locked_lines, covered, started)

        residuals = _residuals(request, covered, budget)
        chosen = _choose(residuals, mandatory, budget)
        selected = _select_candidates(chosen, mandatory, budget)

        return self._bundle(request, locked_lines, residuals, selected, budget, started)

    # ── assembling the answer ───────────────────────────────────────────────

    def _bundle(
        self,
        request: BundleOptimizationRequest,
        locked_lines: list[BundleLine],
        residuals: list[_Residual],
        selected: dict[int, RankedProductCandidate],
        budget: PriceConstraint | None,
        started: float,
    ) -> RoomBundle:
        lines = list(locked_lines)
        by_index = {residual.index: residual for residual in residuals}
        for index in sorted(selected):
            residual = by_index[index]
            lines.append(
                BundleLine(
                    need_index=index,
                    product=selected[index].product,
                    quantity=residual.quantity,
                    locked=False,
                    acquisition=BundleAcquisition.TO_BUY,
                    relaxation_depth=selected[index].relaxation_depth,
                )
            )

        unmet = tuple(
            UnmetNeed(
                need_index=residual.index,
                priority=residual.priority,
                shortfall=residual.quantity,
                reason=residual.reason,
                commerce_category=residual.need.commerce_category,
                commerce_subcategory=residual.need.commerce_subcategory,
                cheapest_price=(
                    residual.minimum_cost
                    if residual.reason is UnmetReason.BUDGET_EXHAUSTED
                    else None
                ),
            )
            for residual in residuals
            if residual.index not in selected
        )
        total, currency, unavailable = _money(lines, budget)
        status = (
            BundleStatus.PARTIAL
            if any(entry.priority is DesignPriority.REQUIRED for entry in unmet)
            else BundleStatus.COMPLETE
        )
        bundle = RoomBundle(
            lines=tuple(lines),
            status=status,
            unmet=unmet,
            new_spend_total=total,
            currency=currency,
            total_unavailable=unavailable,
        )
        self._log(request, bundle, started)
        return bundle

    def _infeasible(
        self,
        request: BundleOptimizationRequest,
        locked_lines: list[BundleLine],
        covered: dict[int, int],
        started: float,
    ) -> RoomBundle:
        """The locks alone break the budget.

        The locked lines are still returned: the customer asked to keep these,
        and the only useful thing to say is which of them account for it.
        Nothing new is selected, and no lock is dropped to make the numbers
        work (CLAUDE.md 10).
        """
        unmet = tuple(
            UnmetNeed(
                need_index=entry.need_index,
                priority=entry.need.priority,
                shortfall=entry.need.quantity - covered.get(entry.need_index, 0),
                reason=UnmetReason.BUDGET_EXHAUSTED,
                commerce_category=entry.need.commerce_category,
                commerce_subcategory=entry.need.commerce_subcategory,
            )
            for entry in request.discovery.needs
            if entry.need.quantity - covered.get(entry.need_index, 0) > 0
        )
        total, currency, unavailable = _money(locked_lines, request.budget)
        bundle = RoomBundle(
            lines=tuple(locked_lines),
            status=BundleStatus.INFEASIBLE,
            unmet=unmet,
            new_spend_total=total,
            currency=currency,
            total_unavailable=unavailable,
        )
        self._log(request, bundle, started)
        return bundle

    @staticmethod
    def _log(
        request: BundleOptimizationRequest, bundle: RoomBundle, started: float
    ) -> None:
        """Shape and counts only: no product, no customer text, no retailer."""
        short = {entry.need_index for entry in bundle.unmet}
        logger.info(
            "bundle_optimization_completed",
            status=str(bundle.status),
            need_count=len(request.discovery.needs),
            fulfilled_required=_fulfilled(request, short, DesignPriority.REQUIRED),
            fulfilled_recommended=_fulfilled(
                request, short, DesignPriority.RECOMMENDED
            ),
            fulfilled_optional=_fulfilled(request, short, DesignPriority.OPTIONAL),
            unmet_count=len(bundle.unmet),
            line_count=len(bundle.lines),
            locked_line_count=sum(1 for line in bundle.lines if line.locked),
            already_owned_count=sum(
                1
                for line in bundle.lines
                if line.acquisition is BundleAcquisition.ALREADY_OWNED
            ),
            to_buy_count=sum(
                1
                for line in bundle.lines
                if line.acquisition is BundleAcquisition.TO_BUY
            ),
            budget_supplied=request.budget is not None,
            total_available=bundle.new_spend_total is not None,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )


def _fulfilled(
    request: BundleOptimizationRequest, short: set[int], priority: DesignPriority
) -> int:
    return sum(
        1
        for entry in request.discovery.needs
        if entry.need.priority is priority and entry.need_index not in short
    )


def _unavailable(reason: BundleUnavailableReason) -> BundleUnavailable:
    logger.info("bundle_optimization_unavailable", reason=str(reason))
    return BundleUnavailable(reason=reason)


# ── locks ───────────────────────────────────────────────────────────────────


def _satisfies(product: ProductCandidate, need: DesignCategoryNeed) -> bool:
    """Whether one locked unit structurally satisfies one unit of a need.

    Structure only. Colour, style and a need's semantic intent are ranking
    preferences and never hard eligibility, so none of them is consulted: a
    lock that matches the type is a match, and one that does not cannot be
    talked into being one (CLAUDE.md 12.4).

    Capacity is matched against the reviewed value alone. A NULL capacity is
    *unverified*, never "any", so it satisfies no stated requirement
    (CLAUDE.md 6.1, 31). A sibling subtype is a different approved value and is
    never substituted.
    """
    commerce = product.commerce
    if commerce.category != need.commerce_category:
        return False
    if (
        need.commerce_subcategory is not None
        and commerce.subcategory != need.commerce_subcategory
    ):
        return False
    wanted = need.seating_capacity
    if wanted is None:
        return True
    seats = commerce.seating_capacity
    if seats is None:
        return False
    if wanted.min_capacity is not None and seats < wanted.min_capacity:
        return False
    return wanted.max_capacity is None or seats <= wanted.max_capacity


def _allocate_locks(
    request: BundleOptimizationRequest,
) -> tuple[list[BundleLine], dict[int, int]]:
    """Assign locked units to need units, then account for what is left over.

    Deterministic by construction: needs in design plan order, locks in product
    id order, one physical unit to at most one need unit. A need already
    covered by locks receives nothing new; surplus units stay in the room as
    lines belonging to no need, because a lock is preserved whether or not the
    current plan has a place for it.
    """
    locks = sorted(request.locked, key=lambda lock: lock.product.product_id)
    remaining = [lock.quantity for lock in locks]
    assigned: dict[tuple[int, int], int] = {}
    covered: dict[int, int] = {}

    for entry in request.discovery.needs:
        wanted = entry.need.quantity
        for position, lock in enumerate(locks):
            if wanted == 0:
                break
            if remaining[position] == 0 or not _satisfies(lock.product, entry.need):
                continue
            taken = min(remaining[position], wanted)
            remaining[position] -= taken
            wanted -= taken
            assigned[(entry.need_index, position)] = taken
            covered[entry.need_index] = covered.get(entry.need_index, 0) + taken

    lines = [
        BundleLine(
            need_index=need_index,
            product=locks[position].product,
            quantity=quantity,
            locked=True,
            acquisition=locks[position].acquisition,
        )
        for (need_index, position), quantity in sorted(assigned.items())
    ]
    lines.extend(
        BundleLine(
            need_index=None,
            product=lock.product,
            quantity=remaining[position],
            locked=True,
            acquisition=lock.acquisition,
        )
        for position, lock in enumerate(locks)
        if remaining[position] > 0
    )
    return lines, covered


def _usable(product: ProductCandidate) -> bool:
    """Whether a price is one commerce can act on.

    Absence of a budget is absence of a ceiling, not permission to treat a
    non-positive amount or an unlabelled one as spendable.
    """
    return product.price_amount > 0 and bool(product.price_unit.strip())


def _locked_spend_problem(
    locks: Sequence[LockedBundleProduct], budget: PriceConstraint | None
) -> BundleUnavailableReason | None:
    """Whether a lock that must be bought makes the arithmetic impossible.

    Only `TO_BUY` locks can. One the customer already owns contributes nothing
    to new spend, so its price - however odd - never has to be read.

    A lock cannot be dropped, so neither problem can be worked around by
    excluding it the way an ordinary candidate would be.
    """
    for lock in locks:
        if lock.acquisition is not BundleAcquisition.TO_BUY:
            continue
        if not _usable(lock.product):
            return BundleUnavailableReason.LOCKED_PRICE_UNUSABLE
        if budget is not None and lock.product.price_unit != budget.currency:
            return BundleUnavailableReason.BUDGET_NOT_COMPARABLE
    return None


def _to_buy_total(locks: Sequence[LockedBundleProduct]) -> Decimal:
    return sum(
        (
            lock.product.price_amount * lock.quantity
            for lock in locks
            if lock.acquisition is BundleAcquisition.TO_BUY
        ),
        Decimal(0),
    )


# ── budget arithmetic ───────────────────────────────────────────────────────


def _within(total: Decimal, budget: PriceConstraint) -> bool:
    """Whether a spend obeys the ceiling, at the exact endpoint.

    An exclusive bound is compared with `<`. Nothing subtracts an epsilon: the
    smallest representable difference is a property of the data, not of the
    rule, and inventing one would admit or refuse a product at the boundary for
    no stated reason.
    """
    ceiling = budget.max_amount
    assert ceiling is not None, "a supported budget carries a ceiling"
    return total < ceiling if budget.max_exclusive else total <= ceiling


def _affordable(
    total: Decimal, budget: PriceConstraint | None
) -> bool:
    return budget is None or _within(total, budget)


# ── residual needs ──────────────────────────────────────────────────────────


def _residuals(
    request: BundleOptimizationRequest,
    covered: dict[int, int],
    budget: PriceConstraint | None,
) -> list[_Residual]:
    """Every need still wanting units, with the candidates that could fill it.

    The filters narrow in a fixed order so the reason a need goes unmet is the
    first thing that actually stopped it: nothing was found, nothing had a
    usable price, or nothing was priced in the budget's currency. Those are
    three different things to tell a customer.
    """
    residuals: list[_Residual] = []
    for entry in request.discovery.needs:
        quantity = entry.need.quantity - covered.get(entry.need_index, 0)
        if quantity <= 0:
            continue
        candidates, reason = _usable_candidates(entry, budget)
        residuals.append(
            _Residual(entry.need, entry.need_index, quantity, candidates, reason)
        )
    return residuals


def _usable_candidates(
    entry: DesignNeedCandidates, budget: PriceConstraint | None
) -> tuple[tuple[RankedProductCandidate, ...], UnmetReason]:
    pool = () if entry.pool is None else entry.pool.candidates
    if not pool:
        return (), UnmetReason.NO_CANDIDATES

    priced = tuple(c for c in pool if _usable(c.product))
    if not priced:
        return (), UnmetReason.NO_USABLE_PRICE
    if budget is None:
        return priced, UnmetReason.BUDGET_EXHAUSTED

    comparable = tuple(
        c for c in priced if c.product.price_unit == budget.currency
    )
    if not comparable:
        return (), UnmetReason.NOT_BUDGET_COMPARABLE
    return comparable, UnmetReason.BUDGET_EXHAUSTED


# ── which needs to satisfy ──────────────────────────────────────────────────


def _cheapest_sum(costs: Sequence[Decimal], count: int) -> Decimal:
    return sum(sorted(costs)[:count], Decimal(0))


def _choose(
    residuals: Sequence[_Residual],
    mandatory: Decimal,
    budget: PriceConstraint | None,
) -> list[_Residual]:
    """The need set, by maximal fulfilment and then by design plan order.

    Two stages, and the second is the one that is easy to get wrong.

    First the maximal triple. Within a tier every satisfied need is worth the
    same, so taking needs cheapest-first maximises the count - and the *k*
    cheapest also cost the least of any *k*, which leaves the most budget for
    the tiers below. That is what lets the tiers be solved in order.

    Then the actual set. Plan-order preference ranks *below* all three counts,
    so the lookahead must keep the **whole remaining triple** reachable, not
    just its own tier's count: preferring an earlier, dearer required need must
    not quietly cost a recommended one. A need is included whenever doing so
    leaves the triple achievable, and skipped only when it does not - and when
    inclusion is infeasible, every selection achieving the triple excludes it
    anyway, so skipping never loses reachability.
    """
    fillable = [residual for residual in residuals if residual.candidates]
    by_tier = {
        tier: [r for r in fillable if r.priority is tier] for tier in _TIERS
    }
    costs = {
        tier: [_cost(r) for r in by_tier[tier]] for tier in _TIERS
    }

    targets: dict[DesignPriority, int] = {}
    committed = mandatory
    for tier in _TIERS:
        count = _max_count(costs[tier], committed, budget)
        targets[tier] = count
        committed += _cheapest_sum(costs[tier], count)

    chosen: list[_Residual] = []
    spent = mandatory
    for position, tier in enumerate(_TIERS):
        undecided = list(by_tier[tier])
        taken = 0
        # The cheapest way to satisfy the tiers not yet walked at all.
        later = sum(
            (
                _cheapest_sum(costs[other], targets[other])
                for other in _TIERS[position + 1 :]
            ),
            Decimal(0),
        )
        for residual in by_tier[tier]:
            undecided.remove(residual)
            still_needed = targets[tier] - taken - 1
            if still_needed < 0:
                continue
            completion = _cheapest_sum([_cost(r) for r in undecided], still_needed)
            if not _affordable(
                spent + _cost(residual) + completion + later, budget
            ):
                continue
            chosen.append(residual)
            spent += _cost(residual)
            taken += 1
    return chosen


def _cost(residual: _Residual) -> Decimal:
    minimum = residual.minimum_cost
    assert minimum is not None, "a fillable need has a cheapest completion"
    return minimum


def _max_count(
    costs: Sequence[Decimal], committed: Decimal, budget: PriceConstraint | None
) -> int:
    """How many of a tier's needs are affordable at their cheapest.

    Ascending-cost greedy is exact here: any *k* affordable needs cost at least
    as much as the *k* cheapest, so if any selection of size *k* fits, the *k*
    cheapest fit too.
    """
    if budget is None:
        return len(costs)
    total = committed
    count = 0
    for cost in sorted(costs):
        if not _within(total + cost, budget):
            break
        total += cost
        count += 1
    return count


# ── which product for each chosen need ──────────────────────────────────────


def _select_candidates(
    chosen: Sequence[_Residual],
    mandatory: Decimal,
    budget: PriceConstraint | None,
) -> dict[int, RankedProductCandidate]:
    """The best-ranked product per need that still leaves the rest affordable.

    Walked in design plan order, which is what makes the rank objective
    lexicographic rather than a sum: an earlier need's ranking is never traded
    away to improve a later one's, and ranks from different pools are never
    added together or compared to each other.

    The lookahead is why a second-ranked sofa can be chosen over a first-ranked
    one: taking the dearer product is refused when it would leave a later
    chosen need unaffordable. The cheapest candidate always passes, so a need
    that reached this point is always filled.
    """
    ordered = sorted(chosen, key=lambda residual: residual.index)
    selected: dict[int, RankedProductCandidate] = {}
    spent = mandatory

    for position, residual in enumerate(ordered):
        rest = sum(
            (_cost(other) for other in ordered[position + 1 :]), Decimal(0)
        )
        for candidate in residual.candidates:
            line = candidate.product.price_amount * residual.quantity
            if _affordable(spent + line + rest, budget):
                selected[residual.index] = candidate
                spent += line
                break
    return selected


# ── the money ───────────────────────────────────────────────────────────────


def _money(
    lines: Sequence[BundleLine], budget: PriceConstraint | None
) -> tuple[Decimal | None, str | None, TotalUnavailableReason | None]:
    """New spend, through the one helper every rendering of a room uses.

    Delegated rather than computed here so that a room rebuilt from state after
    a local change cannot arrive at a different figure from the one this
    optimisation produced.
    """
    return new_spend(
        [
            SpendLine(
                unit_price=line.product.price_amount,
                price_unit=line.price_unit,
                quantity=line.quantity,
                acquisition=line.acquisition,
            )
            for line in lines
        ],
        fallback_currency=budget.currency if budget else None,
    )
