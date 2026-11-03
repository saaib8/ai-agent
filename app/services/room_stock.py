"""A room never uses a product more times than the store has it (known issue 37).

Stock never chooses a product: the optimiser picks each piece by look, size
and budget exactly as before. Only afterwards, counting every piece of the
room in plan order, is a product the store holds too few of handled:

* a piece wanted in more than one becomes two pieces of the same kind - the
  chosen product for as many as are left, the rest from the same search
  without it, so the closest look fills them;
* a piece whose product the earlier pieces have used up takes the same search
  without it.

The search is not run again, each piece keeps one product - so every later
swap or revision works as on any other piece - and unknown stock is never a
limit. Locked pieces are counted, never changed.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from app.schemas.bundle import RoomBundle
from app.schemas.design import DesignCategoryNeed, InteriorDesignResult
from app.schemas.design_discovery import DesignDiscoveryResult, DesignNeedCandidates
from app.schemas.product import ProductCandidate


def allowed(product: ProductCandidate, bought_once: Collection[str]) -> int | None:
    """How many of a product one room may use: what the store has, and only
    one of a kind never bought twice - two pieces of art are two works
    (known issue 38). None is no limit."""
    stock = product.listing.stock_quantity
    if product.commerce.subcategory in bought_once:
        return 1 if stock is None else min(stock, 1)
    return stock


@dataclass(frozen=True, slots=True)
class _Short:
    product_id: int
    left: int
    """How many of it this piece may still take; 0 when the room used them up."""


def split_short_pieces(
    plan: InteriorDesignResult,
    discovery: DesignDiscoveryResult,
    room: RoomBundle,
    bought_once: Collection[str] = (),
) -> tuple[InteriorDesignResult, DesignDiscoveryResult] | None:
    """The plan and its candidates with every piece the store is short for
    split or moved on, or None when the store has enough of everything."""
    short = _short_pieces(discovery, room, None, bought_once)
    if not short:
        return None
    entries: list[DesignNeedCandidates] = []
    for entry in discovery.needs:
        found = short.get(entry.need_index)
        if found is None:
            entries.append(entry)
        elif found.left == 0:
            entries.append(_narrowed(entry, entry.need.quantity, found.product_id, chosen=False))
        else:
            rest = entry.need.quantity - found.left
            entries.append(_narrowed(entry, found.left, found.product_id, chosen=True))
            entries.append(_narrowed(entry, rest, found.product_id, chosen=False))
    entries = [entry.model_copy(update={"need_index": n}) for n, entry in enumerate(entries)]
    split_plan = plan.model_copy(update={"needs": tuple(entry.need for entry in entries)})
    return split_plan, DesignDiscoveryResult(needs=tuple(entries))


def without_used_up(
    discovery: DesignDiscoveryResult,
    room: RoomBundle,
    searched: Collection[int],
    bought_once: Collection[str] = (),
) -> DesignDiscoveryResult | None:
    """The candidates of the pieces found again (`searched`, by plan position)
    without a product the rest of the room has used up, or None when none
    is. The plan keeps its shape: a revised piece moves on, it never splits."""
    short = {
        index: found
        for index, found in _short_pieces(discovery, room, searched, bought_once).items()
        if found.left == 0
    }
    if not short:
        return None
    return DesignDiscoveryResult(
        needs=tuple(
            _narrowed(entry, entry.need.quantity, short[entry.need_index].product_id, chosen=False)
            if entry.need_index in short
            else entry
            for entry in discovery.needs
        )
    )


def _short_pieces(
    discovery: DesignDiscoveryResult,
    room: RoomBundle,
    only: Collection[int] | None,
    bought_once: Collection[str],
) -> dict[int, _Short]:
    """Each piece whose product the store has too few of for it, counting the
    room in plan order - locked pieces first, as they are kept whatever. Only
    the pieces in `only` are reported when it is given; the others still
    count."""
    used: dict[int, int] = {}
    for line in room.lines:
        if line.locked:
            used[line.product.product_id] = used.get(line.product.product_id, 0) + line.quantity
    wanted = {entry.need_index: entry.need.quantity for entry in discovery.needs}
    short: dict[int, _Short] = {}
    lines = [line for line in room.lines if not line.locked and line.need_index is not None]
    # Pieces kept as they are count first, so a piece found again moves on.
    lines.sort(key=lambda line: (only is not None and line.need_index in only, line.need_index))
    for line in lines:
        product_id = line.product.product_id
        before = used.get(product_id, 0)
        used[product_id] = before + line.quantity
        stock = allowed(line.product, bought_once)
        index = line.need_index
        assert index is not None
        if stock is None or before + line.quantity <= stock:
            continue
        if only is not None and index not in only:
            continue
        left = max(stock - before, 0)
        # A piece a lock covers in part is left as it is when it would split.
        if left and wanted.get(index) != line.quantity:
            continue
        short[index] = _Short(product_id, left)
        used[product_id] = before + left
    return short


def _narrowed(
    entry: DesignNeedCandidates, quantity: int, product_id: int, *, chosen: bool
) -> DesignNeedCandidates:
    """The piece for `quantity` of its kind: the chosen product alone, or every
    candidate but it - each with its own tier, so the order stays the search's.
    Its share of the budget is its part of the whole piece's, never all of it."""
    need = part_of(entry.need, quantity)
    pool = entry.pool
    if pool is None:
        return entry.model_copy(update={"need": need})
    kept = [
        (candidate, entry.tiers[n] if entry.tiers else None)
        for n, candidate in enumerate(pool.candidates)
        if (candidate.product.product_id == product_id) is chosen
    ]
    return entry.model_copy(
        update={
            "need": need,
            "pool": pool.model_copy(update={"candidates": tuple(c for c, _ in kept)}),
            "tiers": tuple(t for _, t in kept if t is not None),
        }
    )


def part_of(need: DesignCategoryNeed, quantity: int) -> DesignCategoryNeed:
    """The piece for `quantity` of its whole, with its part of the budget
    share - a lamp of a pair never spends what the pair was given."""
    part = need.model_copy(update={"quantity": quantity})
    design = need.design
    if design is None or design.share_percent is None or quantity >= need.quantity:
        return part
    share = max(1, design.share_percent * quantity // need.quantity)
    return part.model_copy(update={"design": design.model_copy(update={"share_percent": share})})


def short_by_stock(room: RoomBundle, bought_once: Collection[str] = ()) -> dict[int, int]:
    """Each piece (by plan position) asking for more of its product than the
    room may use, with how many it may. A product is never in two pieces of a
    room after a change, so a piece's own line is all that counts."""
    return {
        line.need_index: stock
        for line in room.lines
        if not line.locked
        and line.need_index is not None
        and (stock := allowed(line.product, bought_once)) is not None
        and stock < line.quantity
    }
