"""Salman's tools: what goes with a pick, and the room into the cart.

What goes with a piece is Salman's choice among what the store stocks, the
reviewed pairings marked; the products shown are a real search. Nothing here is "usually
bought together": the store records no such thing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import Field

from app.agents.context import ToolContext
from app.agents.facts import product_facts
from app.agents.operations.cart import as_room_in_cart, room_pieces_to_buy
from app.agents.payloads import SearchShown
from app.agents.services import AgentServices
from app.agents.tools.base import ToolOutcome, ZoryTool
from app.agents.tools.schemas import ToolSchemas
from app.agents.tools.shopping import (
    NoArgs,
    ProductRef,
    hydrated,
    in_stock,
    refusal_facts,
    resolve_product,
    search_outcome,
)
from app.agents.work import ToolStatus, TurnWork
from app.core.exceptions import IntegrationUnavailableError
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import (
    MAX_FINISHED_ROOMS,
    AgentStateV1,
    CrossSellKindPlan,
    CrossSellPlan,
)
from app.schemas.discovery import MAX_EXCLUDED_PRODUCT_IDS
from app.schemas.grounding import GroundedProduct, SearchExecutionGrounding, SearchOutcome
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.query import RankingLean, ResolvedSearch
from app.schemas.relaxation import StopReason
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerContext
from app.services.agent_state import commit_search_results
from app.services.cross_sell import live_plan, log_plan_vs_shown, longer_side, plan_lean
from app.services.design_checks import not_the_anchors_kind
from app.services.room_cart import finished
from app.taxonomy.complements import Companion
from app.taxonomy.words import customer_words

SALMAN = frozenset({AgentPersona.SALMAN})


class GoesWithArgs(ProductRef):
    show_kind: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "The kind to show, as listed by a call without it (its `kind`). Omit to be "
            "told the kinds that could go with the piece."
        ),
    )


def goes_with_tool(schemas: ToolSchemas) -> ZoryTool[GoesWithArgs]:
    async def goes_with(args: GoesWithArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        if services.companion_search is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="suggestions_unavailable")
        product_id = await resolve_product(services, args.which, work, context)
        if isinstance(product_id, ToolOutcome):
            return product_id
        picks = work.staged.product_interaction.selected_product_ids
        ids = (product_id, *(p for p in picks if p != product_id))
        products = await hydrated(services, ids, context)
        if not products:
            return ToolOutcome(status=ToolStatus.REFUSED, code="catalog_unavailable")
        anchor = products[0]
        anchor_kinds = (anchor.commerce.subcategory,)
        # What they already have is never shown back to them, and a kind the
        # cart already holds gets no picture: a second bed beside the bed in
        # their cart is not a suggestion. A second seat stays a chip
        # (another_of_its_kind).
        cart = tuple(picks)
        cart_kinds = {p.commerce.subcategory for p in products if p.product_id in cart}
        try:
            capabilities = await services.capabilities.capabilities(context.retailer)
        except IntegrationUnavailableError:
            return ToolOutcome(status=ToolStatus.REFUSED, code="catalog_unavailable")
        plan = _plan_for(work.staged, anchor)
        if args.show_kind is None:
            # Every kind the store stocks may go with it; the reviewed pairings
            # are marked, the most stocked first - never a count, which would
            # read as the shop's stock (CLAUDE.md 10.12). Which to show is the
            # agent's choice.
            reviewed = (
                list(services.complements.for_type(anchor.commerce.subcategory))
                if services.complements
                else []
            )
            stocked = sorted(
                (
                    (count, kind)
                    for kind in _unique(reviewed)
                    if not_the_anchors_kind(kind.commerce_subcategory, anchor_kinds)
                    and (
                        count := capabilities.product_count(
                            kind.commerce_category, kind.commerce_subcategory
                        )
                    )
                ),
                key=lambda pair: -pair[0],
            )
            kinds = [_kind_entry(kind) for _, kind in stocked]
            # The complementary kinds the grid samples - the chips' kinds, before
            # the anchor's own kind (another_of_its_kind) is listed for seating,
            # which is a similar-product chip, not a grid card.
            companions = [kind for _, kind in stocked]
            facts: dict[str, Any] = {"goes_with": product_facts(anchor, None)}
            if not kinds:
                # No reviewed pairing the store stocks: the stocked kinds
                # themselves, so the choice never ends empty-handed - which
                # suit it is the agent's call.
                kinds = _stocked_kinds(services, capabilities, anchor_kinds)
                companions = [
                    companion
                    for entry in kinds
                    if (companion := _stocked_kind(services, capabilities, entry["kind"]))
                    is not None
                ]
                facts["none_reviewed"] = "choose the kinds that would suit it from these"
            else:
                facts["other_kinds"] = "store_shelf lists every other kind the store stocks"
            if plan is not None:
                baseline = [
                    companion
                    for companion in companions
                    if companion.commerce_subcategory not in cart_kinds
                ][:CROSS_SELL_GRID_KINDS]
                # A planned kind keeps its reviewed words on its chip ("rugs",
                # not "carpets"; "wall art", not "art canvas") wherever it is paired.
                named = {c.commerce_subcategory: c for c in reviewed}
                planned = [
                    named.get(name)
                    or (services.complements.worded(name) if services.complements else None)
                    or companion
                    for name in (item.kind for item in plan.kinds)
                    if (companion := _stocked_kind(services, capabilities, name)) is not None
                    and companion.commerce_subcategory not in anchor_kinds
                    and companion.commerce_subcategory not in cart_kinds
                ]
                companions = _unique([*planned, *baseline])[: max(len(baseline), len(planned))]
                facts["design_plan_applied"] = True
                unavailable = [
                    item.kind
                    for item in plan.kinds
                    if item.kind not in {item.commerce_subcategory for item in planned}
                ]
                if unavailable:
                    facts["planned_kinds_replaced"] = unavailable
                # The chips are the plan's kinds as the grid shows them, so every
                # chip opens a page that leans to her plan; the other stocked
                # kinds stay a store_shelf call away.
                kinds = [
                    _kind_entry(companion)
                    for companion in companions
                    if companion.commerce_subcategory not in cart_kinds
                ]
            grid = await _cross_sell_grid(
                anchor,
                [c for c in companions if c.commerce_subcategory not in cart_kinds],
                services,
                work.staged,
                context.retailer,
                cart=cart,
                plan=plan,
            )
            kinds = [*_another_of_its_kind(services, capabilities, anchor.commerce), *kinds]
            if grid is None:
                return ToolOutcome(
                    code=None if kinds else "nothing_to_suggest",
                    facts={**facts, "kinds": kinds},
                )
            grounding, committed, suggestions = grid
            return ToolOutcome(
                facts={
                    **facts,
                    "kinds": kinds,
                    "suggestions": suggestions,
                    "one_per_kind": (
                        "one product from each kind, so they see the variety; offer the "
                        "chips to see more of any one kind, not a generic 'show more'"
                    ),
                },
                state=committed,
                payload=SearchShown(grounding=grounding, more_options=False),
            )
        if not not_the_anchors_kind(args.show_kind, anchor_kinds):
            # Another of its own kind is a similar-product search, not a pairing.
            return ToolOutcome(
                status=ToolStatus.REFUSED,
                code="same_kind_as_the_piece",
                facts={"use_instead": "more_like_this"},
            )
        kind = _stocked_kind(services, capabilities, args.show_kind)
        if kind is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="kind_not_stocked")
        kind_plan = plan.for_kind(kind.commerce_subcategory) if plan else None
        resolved = _leaning(
            _without(cart, services.companion_search.build(anchor, kind)), anchor, kind_plan
        )
        run = await services.search.new(
            resolved, work.staged, context.retailer, alongside=False, asked=False
        )
        if run.grounding is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="catalog_unavailable")
        shown: dict[str, Any] = {
            "goes_with": product_facts(anchor, None),
            "shown_kind": kind.label,
        }
        if (leaned := _leaned_to(resolved.lean)) is not None:
            shown["leaned_to"] = leaned
        for product in run.grounding.products[:CROSS_SELL_GRID_KINDS]:
            log_plan_vs_shown(
                "chip_page",
                kind.commerce_subcategory,
                resolved.lean if kind_plan else None,
                product.main_color,
                longer_side(product.dimensions),
            )
        return search_outcome(run, shown)

    return ZoryTool(
        name="what_goes_with",
        description=(
            "What goes with a piece they chose. Without show_kind: a grid of one product "
            "from each of the first few complementary kinds (suggestions) plus the kinds "
            "as chips (kinds), the most stocked first - present the variety and offer a "
            "chip to see more of one kind; with none reviewed (none_reviewed), the stocked "
            "kinds to choose from. With show_kind: a full page of that one kind, leaning to "
            "Nora's plan for it when she made one, else to the piece's look. Its own kind "
            "is listed only for a piece that seats several (another_of_its_kind), shown "
            "with more_like_this."
        ),
        args=GoesWithArgs,
        owners=SALMAN,
        handler=goes_with,
    )


CROSS_SELL_GRID_KINDS = 4
"""How many complementary kinds the cross-sell grid samples - one product from
each, so the customer sees the variety the chips name instead of a page of one
kind. Each kind is one real companion search run in turn, so this is kept small
to bound the turn's latency; a fresh-session fan-out would let it grow."""


async def _cross_sell_grid(
    anchor: ProductCandidate,
    companions: Sequence[Companion],
    services: AgentServices,
    state: AgentStateV1,
    context: RetailerContext,
    *,
    cart: tuple[int, ...] = (),
    plan: CrossSellPlan | None = None,
) -> tuple[SearchExecutionGrounding, AgentStateV1, list[dict[str, Any]]] | None:
    """One product per complementary kind, assembled into a single grid.

    A product beside a pick is a few teasers, not a page: the top match of each
    of the first few complementary kinds (a nightstand, a lamp, a rug), so the
    customer sees the variety the chips name instead of a page of one kind. Each
    kind is a real companion search (CLAUDE.md 27), leaning to Nora's plan for
    that kind (its colours, styles, what to push down, its size beside the pick)
    or, with no plan for it, to the pick's styles; tapping a chip drills into
    one kind's full page through the show_kind path, leaning alike.

    The grid is recorded as the presented list, so each card is selectable and
    can be staged "in a room" like any search card; the first kind's search
    becomes the active one, so a typed "show me more" pages that kind rather
    than the customer's last, unrelated search.

    None when nothing could be assembled (no kinds, or none in stock), so the
    caller falls back to the chips alone. Nothing in the `cart` is shown.
    """
    builder = services.companion_search
    if builder is None:
        return None
    chosen = list(companions)[:CROSS_SELL_GRID_KINDS]
    if not chosen:
        return None
    # Sequential, not concurrent: the request holds one database session, which
    # serves one query at a time (a fresh-session fan-out, as a room's pieces
    # use, is the way to parallelise this later).
    picks: list[tuple[Companion, int, GroundedProduct, AgentStateV1]] = []
    leans: dict[str, RankingLean | None] = {}
    seen: set[int] = set()
    for companion in chosen:
        kind_plan = plan.for_kind(companion.commerce_subcategory) if plan else None
        resolved = _leaning(_without(cart, builder.build(anchor, companion)), anchor, kind_plan)
        leans[companion.commerce_subcategory] = resolved.lean
        run = await services.search.new(
            resolved,
            state,
            context,
            alongside=False,
            asked=False,
        )
        if run.grounding is None or not run.grounding.products:
            continue
        product_id = run.state.product_interaction.presented_product_ids[0]
        if product_id in seen:
            continue
        seen.add(product_id)
        picks.append((companion, product_id, run.grounding.products[0], run.state))
    if not picks:
        return None
    products = tuple(
        grounded.model_copy(
            update={"grounding_ref": ordinal, "presented_ordinal": ordinal, "relaxation_depth": 0}
        )
        for ordinal, (_, _, grounded, _) in enumerate(picks, 1)
    )
    count = len(products)
    # A curated suggestion set, not a search result: every customer-facing field
    # is benign by construction - not relaxed, nothing set aside, no truncation -
    # so no reply can claim "I widened" or "these are all" of a search that was
    # never one. The counts equal `count`, so `truncated_for_presentation` is
    # false and no "show more" is implied.
    grounding = SearchExecutionGrounding(
        outcome=SearchOutcome.RESULTS,
        products=products,
        eligible_count=count,
        ranked_count=count,
        selected_count=count,
        presented_count=count,
        exact_candidate_count=count,
        stop_reason=StopReason.EXACT_SUFFICIENT,
    )
    for companion, _, grounded, _ in picks:
        log_plan_vs_shown(
            "grid",
            companion.commerce_subcategory,
            leans.get(companion.commerce_subcategory) if plan else None,
            grounded.main_color,
            longer_side(grounded.dimensions),
        )
    committed = commit_search_results(picks[0][3], tuple(pid for _, pid, _, _ in picks))
    suggestions = [
        {
            "kind": companion.commerce_subcategory,
            "label": companion.label,
            "card": ordinal,
            "name": grounded.name_english,
            **({"colour": grounded.main_color} if grounded.main_color else {}),
            **({"styles": list(grounded.styles)} if grounded.styles else {}),
            **(
                {"leaned_to": leaned}
                if (leaned := _leaned_to(leans.get(companion.commerce_subcategory)))
                else {}
            ),
        }
        for ordinal, (companion, _, grounded, _) in enumerate(picks, 1)
    ]
    return grounding, committed, suggestions


def _plan_for(state: AgentStateV1, anchor: ProductCandidate) -> CrossSellPlan | None:
    """Nora's plan, only for the pick it was made for and while it is in the
    cart: another piece's grid is the reviewed pairings, as before (CLAUDE.md
    10.3)."""
    plan = live_plan(state)
    return plan if plan is not None and plan.anchor_product_id == anchor.product_id else None


def _leaning(
    resolved: ResolvedSearch, anchor: ProductCandidate, plan: CrossSellKindPlan | None
) -> ResolvedSearch:
    """A companion search leaning to Nora's plan for its kind (`plan_lean`):
    her styles take the place of the pick's own when she gave any; given only
    colours, the pick's styles still lead the look. With no plan for the kind
    the search is unchanged."""
    lean = plan_lean(anchor, plan) if plan is not None else None
    if lean is None or plan is None:
        return resolved
    return resolved.model_copy(
        update={
            "lean": lean,
            **({"semantic_preferences": ()} if plan.styles else {}),
        }
    )


def _kind_entry(companion: Companion) -> dict[str, Any]:
    return {
        "kind": companion.commerce_subcategory,
        "family": companion.commerce_category,
        "label": companion.label,
        **({"label_ar": companion.label_ar} if companion.label_ar else {}),
    }


def _leaned_to(lean: RankingLean | None) -> dict[str, Any] | None:
    """What a card was ordered by, for Salman's words - so he only says a rug
    is sand when its card is."""
    if lean is None:
        return None
    leaned: dict[str, Any] = {}
    if lean.colours:
        leaned["colours"] = list(lean.colours)
    if lean.styles:
        leaned["styles"] = list(lean.styles)
    if lean.avoid_colours or lean.avoid_styles:
        leaned["pushed_down"] = [*lean.avoid_colours, *lean.avoid_styles]
    if lean.size_target_cm is not None:
        leaned["longer_side_near_cm"] = str(lean.size_target_cm)
    return leaned or None


def _without(cart: tuple[int, ...], resolved: ResolvedSearch) -> ResolvedSearch:
    """A companion search that never shows back what is already in the cart."""
    if not cart:
        return resolved
    request = resolved.request.model_copy(
        update={"exclude_product_ids": cart[-MAX_EXCLUDED_PRODUCT_IDS:]}
    )
    return resolved.model_copy(update={"request": request})


MAX_STOCKED_KINDS = 20
"""Stocked kinds offered when none is reviewed to go with a piece: the most
stocked, enough to choose from without listing the whole shop."""


def _stocked_kinds(
    services: AgentServices,
    capabilities: RetailerCatalogCapabilities,
    anchor_kinds: tuple[str | None, ...],
) -> list[dict[str, Any]]:
    entries = sorted(
        (
            e
            for e in capabilities.capabilities
            if e.commerce_subcategory is not None
            and not_the_anchors_kind(e.commerce_subcategory, anchor_kinds)
            and services.taxonomy.is_pair(e.commerce_category, e.commerce_subcategory)
        ),
        key=lambda e: -e.active_product_count,
    )
    return [
        {"kind": e.commerce_subcategory, "label": customer_words(e.commerce_subcategory)}
        for e in entries[:MAX_STOCKED_KINDS]
        if e.commerce_subcategory is not None
    ]


def _another_of_its_kind(
    services: AgentServices,
    capabilities: RetailerCatalogCapabilities,
    commerce: CommerceClassification,
) -> list[dict[str, Any]]:
    """Another of the piece's own kind, for a piece that seats several: a
    second sofa beside the first is a common choice (two facing, an L, a
    majlis), so it is offered with the pairings - shown with more_like_this,
    which keeps the piece's look, and listed first so it is never crowded out of
    the choices. Any other kind's own kind is never offered."""
    own, family, seating = commerce.subcategory, commerce.category, services.seating
    if seating is None or own is None or family is None or not seating.seats_several(own):
        return []
    if not capabilities.product_count(family, own):
        return []
    return [
        {
            "kind": own,
            "label": customer_words(own),
            "another_of_its_kind": True,
            "show_with": "more_like_this",
        }
    ]


def _unique(kinds: Sequence[Companion]) -> list[Companion]:
    seen: set[str] = set()
    unique: list[Companion] = []
    for kind in kinds:
        if kind.commerce_subcategory not in seen:
            seen.add(kind.commerce_subcategory)
            unique.append(kind)
    return unique


def _stocked_kind(
    services: AgentServices, capabilities: RetailerCatalogCapabilities, subcategory: str
) -> Companion | None:
    """The kind as a companion, when the store stocks it - its reviewed label
    where there is one."""
    entry = next(
        (
            e
            for e in capabilities.capabilities
            if e.commerce_subcategory == subcategory
            and services.taxonomy.is_pair(e.commerce_category, subcategory)
        ),
        None,
    )
    if entry is None:
        return None
    return Companion(
        commerce_category=entry.commerce_category,
        commerce_subcategory=subcategory,
        label=customer_words(subcategory),
    )


def room_to_cart_tool(schemas: ToolSchemas) -> ZoryTool[NoArgs]:
    async def to_cart(args: NoArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        wanted = room_pieces_to_buy(work.staged)
        if not wanted:
            return ToolOutcome(status=ToolStatus.REFUSED, code="nothing_to_add")
        # The same rules as any add: the cart's limit and the store's own cart.
        products = await hydrated(services, tuple(p for p, _ in wanted), context) or ()
        change = await services.cart_operations.add(
            work.staged, context.retailer, wanted, whole=True, stock=in_stock(products)
        )
        took = change.added or change.already
        state = as_room_in_cart(work.staged, change.state) if took else change.state
        names = {p.product_id: p.name_english for p in products}
        facts = {
            "added": [names.get(p) for p in change.added],
            "refused_by_store": refusal_facts(change.refused, names),
            "cart_full": change.full,
        }
        code = "cart_full" if change.full else None
        return ToolOutcome(code=code, facts=facts, state=state)

    return ZoryTool(
        name="add_room_to_cart",
        description="Put every piece of the designed room they are buying into the cart.",
        args=NoArgs,
        owners=SALMAN,
        handler=to_cart,
    )


def finish_room_tool(schemas: ToolSchemas) -> ZoryTool[NoArgs]:
    async def finish(args: NoArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        done = finished(work.staged)
        if done is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="no_room_in_cart")
        rooms = (*work.staged.finished_rooms, done)[-MAX_FINISHED_ROOMS:]
        state = work.staged.model_copy(
            update={"room_project": None, "finished_rooms": rooms, "swap_budget_offer": None}
        )
        # Which room comes next is theirs to say, asked in the agent's words -
        # any room, not a list the registry knows.
        return ToolOutcome(
            facts={
                "finished": done.room_words,
                "rooms_finished": [room.room_words for room in rooms],
            },
            state=state,
        )

    return ZoryTool(
        name="finish_room",
        description="Close the room now in their cart, so the next room starts fresh.",
        args=NoArgs,
        owners=SALMAN,
        handler=finish,
    )


def sales_tools(schemas: ToolSchemas) -> list[ZoryTool[Any]]:
    return [goes_with_tool(schemas), room_to_cart_tool(schemas), finish_room_tool(schemas)]
