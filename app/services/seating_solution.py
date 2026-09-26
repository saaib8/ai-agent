"""Composing a seating requirement no single product can meet.

"A sofa for eight" where the largest sofa seats five is not a dead end - it is a
combination: a six-seat set and a two-seater, three sofas arranged together, or
a sofa with a couple of armchairs. This planner works out every one the store
can really build and keeps the best of each shape.

It owns every judgement that must be a fact rather than a guess (CLAUDE.md 3.3):

* which types a combination may use, and how many each seats - the reviewed
  seating registry and the catalog's recorded counts;
* which real products fill each piece - from Product Discovery, the same
  validated, store-scoped search every other path uses (CLAUDE.md 27);
* whether the seats add up and the budget holds - summed here, in code, never
  trusted from a model.

The seat count is *met*, not relaxed: the requirement is satisfied by adding
pieces, never by lowering it (CLAUDE.md 13.4). The budget is never broken.

Every piece is held to the rest of the customer's request, as a single-product
search would be: strict colours and styles filter it, wishes order it, and their
sizes measure a piece of the type they gave them for. A strict colour or style
no combination can meet is lifted only as the last resort, and the bundle
records it so the reply says so (CLAUDE.md 12.4).

No model is called. This is the ``build_combination`` tool: which shapes to
offer, and which one to show, can be decided by a caller - a question to the
customer today, the agent loop later.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations_with_replacement
from itertools import product as every_mix

from app.core.logging import get_logger
from app.schemas.catalog_overview import CatalogOverview
from app.schemas.discovery import PriceConstraint, ProductSearchRequest
from app.schemas.product import EligibleProduct, ProductCandidate
from app.schemas.retailer import RetailerContext
from app.schemas.seating_solution import (
    SeatingArrangement,
    SeatingArrangementLine,
    SeatingBundle,
    SeatingBundleLine,
    SeatingRequirements,
    SeatingShape,
    SeatingShapeOption,
    SeatingSolution,
    SeatingSolutionOutcome,
)
from app.services.catalog_capability import CatalogCapabilityService
from app.services.discovery import ProductDiscoveryService
from app.services.hydration import ProductHydrationService
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.seating import SeatingSemantics

logger = get_logger(__name__)

SEATING_CATEGORY = "seating"

MAX_BUNDLES = 3
"""How many combinations to show at once. Enough for a real choice, few enough
to stay a proposal rather than a catalogue."""

MAX_LARGE_PIECES = 3
"""Three sofas arranged together is a room; four is a waiting area."""

MAX_EXTRA_SEATS = 3
"""A couple of armchairs beside a sofa is a living room; five is a queue."""

VARIANTS_PER_PIECE = 3
"""How many products of each kind of piece a combination may choose between on
the first page - the best matches to their wishes, plus the cheapest when it is
not among them. One would give every layout a single version; the catalog
often has several that fit just as well. Each "show me more" looks this much
deeper, up to `MAX_VARIANTS_PER_PIECE`, so paging can reach every product that
fits while the first answer stays fast."""

MAX_VARIANTS_PER_PIECE = 12

ROOM_ARRANGEMENTS = 8
"""How many ways to seat a room are weighed against the rest of the room. The
best by rank, plus the cheapest, so a tight budget can still fit the rest."""

MAX_DRAFTS = 5000
"""A ceiling on the combinations kept for one request - the best by rank, never
the first found - so a large catalog cannot flood a turn. Logged when reached."""


@dataclass(frozen=True, slots=True)
class _Piece:
    """One kind of piece a combination can use: a type at one seat count, with
    the few products that could fill it, best match to their wishes first."""

    subcategory: str
    seats: int
    confirmed: bool
    variants: tuple[EligibleProduct, ...]


_Pick = tuple[_Piece, EligibleProduct, int]
"""A piece, the product chosen for it, and how many."""


@dataclass(frozen=True, slots=True)
class _Draft:
    """A combination before its products are read for display."""

    shape: SeatingShape
    picks: tuple[_Pick, ...]
    total: Decimal
    pieces: int
    missed_wishes: int
    lifted: tuple[AttributeFamily, ...]
    sizes_applied: bool | None

    @property
    def rank(self) -> tuple[int, int, Decimal]:
        """Fewest missed wishes first, then fewer pieces, then cheaper.

        A customer who asked for beige is better served by one more beige
        armchair than by a tidier combination in the wrong colour.
        """
        return (self.missed_wishes, self.pieces, self.total)

    @property
    def product_ids(self) -> tuple[int, ...]:
        return tuple(sorted({product.product_id for _, product, _ in self.picks}))

    @property
    def contents(self) -> tuple[tuple[int, int], ...]:
        """Each product and how many - what the customer would actually buy."""
        return tuple(sorted((product.product_id, qty) for _, product, qty in self.picks))


class SeatingSolutionPlanner:
    """Meet a seat count with several pieces when no single one can."""

    def __init__(
        self,
        capability: CatalogCapabilityService,
        discovery: ProductDiscoveryService,
        hydration: ProductHydrationService,
        seating: SeatingSemantics,
    ) -> None:
        self._capability = capability
        self._discovery = discovery
        self._hydration = hydration
        self._seating = seating

    async def plan(
        self,
        *,
        target_seats: int,
        budget_amount: Decimal | None,
        currency: str,
        context: RetailerContext,
        requirements: SeatingRequirements | None = None,
        shape: SeatingShape | None = None,
        exclude: frozenset[tuple[tuple[int, int], ...]] = frozenset(),
    ) -> SeatingSolution:
        """Combinations that seat ``target_seats`` within ``budget_amount``.

        Without a ``shape``, the best of each shape the store can build; with
        one, the best few of that shape. `options` lists every shape with
        combinations not yet seen, with its lowest total, so a caller can offer
        the choice.

        ``exclude`` holds combinations already shown and paged past, or turned
        down - each as its products and quantities. They are never shown again,
        so "show me more" always means new ones; when a shape has none left the
        outcome says so rather than repeating what is on screen.

        Returns an outcome for every case a caller must tell apart: a single
        piece already suffices, the store has no seating, combinations exist but
        none fits the budget, or here are the ones that do.
        """
        wanted = requirements or SeatingRequirements()
        overview = await self._capability.overview(context)
        base = {
            "target_seats": target_seats,
            "budget_amount": budget_amount,
            "currency": currency,
        }

        ceiling = overview.max_seats_in(SEATING_CATEGORY)
        if ceiling is None:
            return SeatingSolution(**base, outcome=SeatingSolutionOutcome.NO_SEATING)
        if ceiling >= target_seats:
            return SeatingSolution(**base, outcome=SeatingSolutionOutcome.SINGLE_PIECE_SUFFICES)

        # Each page of "show me more" looks further down each kind's products.
        depth = len(exclude) // MAX_BUNDLES
        variants = min(VARIANTS_PER_PIECE * (1 + depth), MAX_VARIANTS_PER_PIECE)
        drafts: list[_Draft] = []
        for lift in _lift_order(wanted):
            pieces = await self._pieces(
                overview, budget_amount, currency, wanted, lift, context, variants
            )
            drafts = _combinations(pieces, target_seats, budget_amount, wanted, lift)
            if drafts:
                break

        unseen = [draft for draft in drafts if draft.contents not in exclude]
        options = _options(unseen)
        bundles = await self._bundles(
            _shown(unseen, shape), budget_amount, currency, wanted, context
        )
        closest: Decimal | None = None
        if bundles:
            outcome = SeatingSolutionOutcome.BUNDLES
        elif drafts and exclude:
            outcome = SeatingSolutionOutcome.NO_MORE
        else:
            outcome = SeatingSolutionOutcome.NONE_WITHIN_BUDGET
            if budget_amount is not None:
                closest = await self._closest_total(
                    overview, currency, wanted, context, target_seats, variants
                )
        logger.info(
            "seating_solution_planned",
            store_id=context.store_id,
            target_seats=target_seats,
            single_piece_ceiling=ceiling,
            combination_count=len(drafts),
            excluded_count=len(exclude),
            shapes=[f"{o.shape}:{o.combination_count}" for o in options],
            requested_shape=str(shape) if shape else None,
            bundle_count=len(bundles),
            lifted=[str(f) for f in (drafts[0].lifted if drafts else ())],
            outcome=str(outcome),
        )
        return SeatingSolution(
            **base,
            outcome=outcome,
            bundles=tuple(bundles),
            options=options if outcome is not SeatingSolutionOutcome.NONE_WITHIN_BUDGET else (),
            wishes_given=bool(wanted.wished_colors or wanted.wished_styles),
            lifted=drafts[0].lifted if drafts else (),
            requested_shape=shape,
            already_seen=len(exclude),
            closest_total=closest,
        )

    async def _closest_total(
        self,
        overview: CatalogOverview,
        currency: str,
        wanted: SeatingRequirements,
        context: RetailerContext,
        target: int,
        variants: int,
    ) -> Decimal | None:
        """The lowest total that seats them if the budget is set aside - held
        to everything else they asked - so the reply can say how far over it
        is, never merely that it is. `None` when nothing seats them at all."""
        for lift in _lift_order(wanted):
            pieces = await self._pieces(overview, None, currency, wanted, lift, context, variants)
            drafts = _combinations(pieces, target, None, wanted, lift)
            if drafts:
                return min(draft.total for draft in drafts)
        return None

    async def arrangements(
        self,
        *,
        target_seats: int,
        budget_amount: Decimal | None,
        currency: str,
        context: RetailerContext,
        types: frozenset[str],
        requirements: SeatingRequirements | None = None,
        limit: int = ROOM_ARRANGEMENTS,
    ) -> tuple[SeatingArrangement, ...]:
        """Ways to seat ``target_seats`` for a room, best first.

        The room's own seating: one piece when one seats them all, otherwise a
        combination, from ``types`` only. Returns the best ``limit`` by rank
        plus the cheapest, because the room's other pieces share the budget
        and the caller weighs them all together (CLAUDE.md 27). Empty when the
        store cannot seat them within the budget.
        """
        wanted = requirements or SeatingRequirements()
        overview = await self._capability.overview(context)
        drafts: list[_Draft] = []
        for lift in _lift_order(wanted):
            pieces = await self._pieces(
                overview, budget_amount, currency, wanted, lift, context, types=types
            )
            drafts = _combinations(pieces, target_seats, budget_amount, wanted, lift, single=True)
            if drafts:
                break
        chosen = list(drafts[:limit])
        if drafts:
            cheapest = min(drafts, key=lambda d: (d.total, d.rank))
            if cheapest not in chosen:
                chosen.append(cheapest)
        logger.info(
            "room_seating_arranged",
            store_id=context.store_id,
            target_seats=target_seats,
            arrangement_count=len(drafts),
            offered=len(chosen),
        )
        return tuple(
            SeatingArrangement(
                lines=tuple(
                    SeatingArrangementLine(
                        product_id=product.product_id,
                        commerce_subcategory=piece.subcategory,
                        quantity=quantity,
                        seats_each=piece.seats,
                    )
                    for piece, product, quantity in draft.picks
                ),
                total_price=draft.total,
                lifted=draft.lifted,
            )
            for draft in chosen
        )

    async def _pieces(
        self,
        overview: CatalogOverview,
        budget: Decimal | None,
        currency: str,
        wanted: SeatingRequirements,
        lift: tuple[AttributeFamily, ...],
        context: RetailerContext,
        variants: int = VARIANTS_PER_PIECE,
        types: frozenset[str] | None = None,
    ) -> list[_Piece]:
        """Every kind of piece the store stocks that meets what is not lifted.

        One search per type; a multi-seat type's products are grouped by their
        recorded seat count, and a product with none is left out - an
        unverified capacity never fills a seat. A one-seat extra seats one by
        reviewed data, and says so (CLAUDE.md 6.2).

        ``types`` narrows the choice to a room's own seating types; without it
        the reviewed combination extras are the one-seat pieces allowed.
        """
        pieces: list[_Piece] = []
        for shelf in overview.shelves_in(SEATING_CATEGORY):
            subcategory = shelf.commerce_subcategory
            if subcategory is None:
                continue
            large = self._seating.seats_several(subcategory)
            if types is not None:
                if subcategory not in types or not (large or self._seating.seats_one(subcategory)):
                    continue
            elif (large and not (
                self._seating.is_combination_main(subcategory) or subcategory == wanted.asked_type
            )) or (not large and not self._seating.is_combination_extra(subcategory)):
                continue
            pool = await self._discovery.eligible_pool(
                _request(subcategory, budget, currency, wanted, lift), context
            )
            groups: dict[int, list[EligibleProduct]] = {}
            for product in pool:
                seats = product.seating_capacity if large else 1
                if seats is not None and seats >= 1:
                    groups.setdefault(seats, []).append(product)
            for seats, products in groups.items():
                pieces.append(
                    _Piece(
                        subcategory=subcategory,
                        seats=seats,
                        confirmed=large,
                        variants=_variants(products, wanted, variants),
                    )
                )
        return pieces

    async def _bundles(
        self,
        drafts: Sequence[_Draft],
        budget: Decimal | None,
        currency: str,
        wanted: SeatingRequirements,
        context: RetailerContext,
    ) -> list[SeatingBundle]:
        """Read the chosen products once and build the displayable bundles.

        A product the catalog no longer returns drops its bundle rather than
        being served from anything stale, and so does one whose fresh price
        would now break the budget - the totals shown are the fresh ones.
        """
        ids = list(dict.fromkeys(pid for draft in drafts for pid in draft.product_ids))
        if not ids:
            return []
        found = {p.product_id: p for p in await self._hydration.hydrate_ids(ids, context)}
        bundles: list[SeatingBundle] = []
        for draft in drafts:
            lines: list[SeatingBundleLine] = []
            for piece, product, quantity in draft.picks:
                row = found.get(product.product_id)
                if row is None:
                    break
                lines.append(_line(piece, row, quantity, wanted))
            else:
                total = sum((line.line_total for line in lines), Decimal(0))
                if budget is None or total <= budget:
                    bundles.append(
                        SeatingBundle(
                            shape=draft.shape,
                            lines=tuple(lines),
                            total_seats=sum(line.line_seats for line in lines),
                            total_price=total,
                            currency=currency,
                            lifted=draft.lifted,
                            sizes_applied=draft.sizes_applied,
                        )
                    )
        return bundles


def _request(
    subcategory: str,
    budget: Decimal | None,
    currency: str,
    wanted: SeatingRequirements,
    lift: tuple[AttributeFamily, ...],
) -> ProductSearchRequest:
    """One type's search, holding everything not lifted.

    No seat filter: products are grouped by their recorded count afterwards,
    so one search serves every seat count of the type.
    """
    return ProductSearchRequest(
        commerce_category=SEATING_CATEGORY,
        commerce_subcategory=subcategory,
        price=PriceConstraint.at_most(budget, currency) if budget is not None else None,
        colors_any_of=() if AttributeFamily.COLOR in lift else wanted.colors_any_of,
        styles_all_of=() if AttributeFamily.STYLE in lift else wanted.styles_all_of,
        dimensions=wanted.dimensions if subcategory == wanted.sized_type else (),
    )


def _combinations(
    pieces: Sequence[_Piece],
    target: int,
    budget: Decimal | None,
    wanted: SeatingRequirements,
    lift: tuple[AttributeFamily, ...],
    *,
    single: bool = False,
) -> list[_Draft]:
    """Every combination that seats exactly the target, best first.

    One spare seat is allowed only when nothing seats it exactly - a group of
    nine is better served by ten seats than by nothing. ``single`` also admits
    one piece, or one-seat pieces alone, as a room's seating does.
    """
    for seats in (target, target + 1):
        found = 0

        def every_draft(seats: int = seats) -> Iterator[_Draft]:
            nonlocal found
            for layout in _layouts(pieces, seats, single=single):
                for draft in _filled(layout, budget, wanted, lift):
                    found += 1
                    yield draft

        drafts = heapq.nsmallest(MAX_DRAFTS, every_draft(), key=lambda d: d.rank)
        if found > MAX_DRAFTS:
            logger.warning("seating_combinations_capped", cap=MAX_DRAFTS, found=found)
        if drafts:
            return _distinct(drafts)
    return []


def _layouts(
    pieces: Sequence[_Piece], seats: int, *, single: bool = False
) -> Iterator[tuple[tuple[_Piece, int], ...]]:
    """Every arrangement of pieces seating exactly ``seats``: two or three large
    pieces, or one or two large pieces plus up to three extra seats of one kind.
    A large piece that seats the whole target alone is not a combination -
    unless ``single``, when it is the simplest answer, and a few one-seat
    pieces alone may seat a small group."""
    large = [p for p in pieces if p.confirmed]
    extras = [p for p in pieces if not p.confirmed]
    for count in range(0 if single else 1, MAX_LARGE_PIECES + 1):
        for group in combinations_with_replacement(large, count):
            covered = sum(p.seats for p in group)
            if covered > seats or (not single and any(p.seats >= seats for p in group)):
                continue
            layout = _grouped(group)
            missing = seats - covered
            if missing == 0 and (count > 1 or (single and count == 1)):
                yield layout
            elif 1 <= missing <= MAX_EXTRA_SEATS and count < MAX_LARGE_PIECES:
                for extra in extras:
                    yield (*layout, (extra, missing))


def _grouped(group: Sequence[_Piece]) -> tuple[tuple[_Piece, int], ...]:
    counts: dict[_Piece, int] = {}
    for piece in group:
        counts[piece] = counts.get(piece, 0) + 1
    return tuple(counts.items())


def _variants(
    products: Sequence[EligibleProduct], wanted: SeatingRequirements, limit: int
) -> tuple[EligibleProduct, ...]:
    """The products a piece may be filled with: the best matches to their
    wishes, and the cheapest - so the budget can still be met."""
    ranked = sorted(products, key=lambda p: _preference(p, wanted))
    chosen = list(ranked[:limit])
    cheapest = min(products, key=lambda p: (p.price_amount, p.product_id))
    if cheapest not in chosen:
        chosen.append(cheapest)
    return tuple(chosen)


def _filled(
    layout: tuple[tuple[_Piece, int], ...],
    budget: Decimal | None,
    wanted: SeatingRequirements,
    lift: tuple[AttributeFamily, ...],
) -> Iterator[_Draft]:
    """The layout filled with every mix of real products within budget.

    Several large pieces of one kind may be different products - three
    3-seaters need not match. Extra seats are one model repeated, so the
    armchairs beside a sofa look like a set.
    """
    slots: list[list[tuple[_Pick, ...]]] = []
    for piece, quantity in layout:
        if piece.confirmed:
            slots.append(
                [
                    _counted(piece, choice)
                    for choice in combinations_with_replacement(piece.variants, quantity)
                ]
            )
        else:
            slots.append([((piece, variant, quantity),) for variant in piece.variants])
    for choice in every_mix(*slots):
        picks = tuple(pick for slot in choice for pick in slot)
        total = sum((item.price_amount * qty for _, item, qty in picks), Decimal(0))
        if budget is not None and total > budget:
            continue
        large_types = {piece.subcategory for piece, _, _ in picks if piece.confirmed}
        yield _Draft(
            shape=(
                SeatingShape.SEPARATE_SOFAS
                if all(piece.confirmed for piece, _, _ in picks)
                else SeatingShape.SOFA_WITH_EXTRA_SEATS
            ),
            picks=picks,
            total=total,
            pieces=sum(qty for _, _, qty in picks),
            missed_wishes=sum(
                qty * (_wish_count(wanted) - _wish_hits(item.main_color, item.styles, wanted))
                for _, item, qty in picks
            ),
            lifted=lift,
            sizes_applied=None if not wanted.dimensions else large_types == {wanted.sized_type},
        )


def _counted(piece: _Piece, choice: Sequence[EligibleProduct]) -> tuple[_Pick, ...]:
    """One kind of piece filled with these products, each with how many."""
    counts: dict[EligibleProduct, int] = {}
    for item in choice:
        counts[item] = counts.get(item, 0) + 1
    return tuple((piece, item, quantity) for item, quantity in counts.items())


def _distinct(drafts: Sequence[_Draft]) -> list[_Draft]:
    """One draft per purchase, best first: two layouts that pick the same
    products in the same quantities are the same combination to a customer."""
    seen: dict[tuple[tuple[int, int], ...], _Draft] = {}
    for draft in sorted(drafts, key=lambda d: d.rank):
        seen.setdefault(draft.contents, draft)
    return list(seen.values())


def _options(drafts: Sequence[_Draft]) -> tuple[SeatingShapeOption, ...]:
    """Every shape that exists, cheapest first, each "from" the lowest total
    among its best matches to their wishes - the price of what would actually
    be shown, not of a cheaper piece in a colour they did not ask for."""
    by_shape: dict[SeatingShape, list[_Draft]] = {}
    for draft in drafts:
        by_shape.setdefault(draft.shape, []).append(draft)
    options = []
    for shape, group in by_shape.items():
        fewest_missed = min(d.missed_wishes for d in group)
        options.append(
            SeatingShapeOption(
                shape=shape,
                from_price=min(d.total for d in group if d.missed_wishes == fewest_missed),
                combination_count=len(group),
            )
        )
    return tuple(sorted(options, key=lambda o: o.from_price))


def _shown(drafts: Sequence[_Draft], shape: SeatingShape | None) -> list[_Draft]:
    """The combinations to show, best first but varied: every shape first (when
    none was chosen), then every different arrangement, and only then other
    product versions of an arrangement already shown - those are what "show me
    more" reaches."""
    pool = [d for d in drafts if shape is None or d.shape is shape]
    shown: list[_Draft] = []
    for key in (lambda d: d.shape, _arrangement, lambda d: d.contents):
        seen = {key(d) for d in shown}
        for draft in pool:
            if len(shown) >= MAX_BUNDLES:
                break
            if key(draft) not in seen and draft not in shown:
                shown.append(draft)
                seen.add(key(draft))
    return sorted(shown, key=lambda d: d.rank)


def _arrangement(draft: _Draft) -> tuple[tuple[str, int, int], ...]:
    """Which kinds of piece, and how many of each - the layout, whatever the
    particular products."""
    counts: dict[tuple[str, int], int] = {}
    for piece, _, quantity in draft.picks:
        key = (piece.subcategory, piece.seats)
        counts[key] = counts.get(key, 0) + quantity
    return tuple(sorted((sub, seats, n) for (sub, seats), n in counts.items()))


def _lift_order(wanted: SeatingRequirements) -> list[tuple[AttributeFamily, ...]]:
    """What to set aside on each attempt: nothing first, then every strict
    family together as the one last resort. A strict value no approved value
    expresses can never be met, so it is lifted from the start."""
    unmeetable = tuple(wanted.unmatchable_strict)
    attempts = [unmeetable]
    everything = wanted.strict_families
    if everything != unmeetable:
        attempts.append(everything)
    return attempts


def _wish_count(wanted: SeatingRequirements) -> int:
    """How many wishes each piece could meet: a colour wish, and each style."""
    return int(bool(wanted.wished_colors)) + len(wanted.wished_styles)


def _wish_hits(main_color: str | None, styles: tuple[str, ...], wanted: SeatingRequirements) -> int:
    """How many of their wishes a piece meets: its colour, and each wished style
    it carries. "Modern beige" is two wishes, and a piece meeting both beats
    one meeting either."""
    colour = int(main_color is not None and main_color in wanted.wished_colors)
    return colour + sum(style in styles for style in wanted.wished_styles)


def _preference(product: EligibleProduct, wanted: SeatingRequirements) -> tuple[int, Decimal, int]:
    """Most wishes met first, then cheaper, then a stable tie-break."""
    hits = _wish_hits(product.main_color, product.styles, wanted)
    return (-hits, product.price_amount, product.product_id)


def _line(
    piece: _Piece, row: ProductCandidate, quantity: int, wanted: SeatingRequirements
) -> SeatingBundleLine:
    return SeatingBundleLine(
        product_id=row.product_id,
        name=row.name_english,
        commerce_subcategory=piece.subcategory,
        unit_price=row.price_amount,
        quantity=quantity,
        seats_each=piece.seats,
        seats_are_confirmed=piece.confirmed,
        image_url=row.image_url,
        product_url=row.product_url,
        matches_wish=_wish_hits(row.main_color, row.styles, wanted) > 0,
    )
