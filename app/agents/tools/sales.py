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
from app.agents.services import AgentServices
from app.agents.tools.base import ToolOutcome, ZoryTool
from app.agents.tools.schemas import ToolSchemas
from app.agents.tools.shopping import (
    NoArgs,
    ProductRef,
    _names,
    hydrated,
    resolve_product,
    search_outcome,
)
from app.agents.work import ToolStatus, TurnWork
from app.core.exceptions import IntegrationUnavailableError
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import MAX_FINISHED_ROOMS
from app.schemas.retailer import RetailerCatalogCapabilities
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
        try:
            capabilities = await services.capabilities.capabilities(context.retailer)
        except IntegrationUnavailableError:
            return ToolOutcome(status=ToolStatus.REFUSED, code="catalog_unavailable")
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
            kinds = [
                {
                    "kind": kind.commerce_subcategory,
                    "label": kind.label,
                    **({"label_ar": kind.label_ar} if kind.label_ar else {}),
                }
                for _, kind in stocked
            ]
            return ToolOutcome(
                code=None if kinds else "nothing_to_suggest",
                facts={
                    "goes_with": product_facts(anchor, None),
                    "kinds": kinds,
                    "other_kinds": "store_shelf lists every other kind the store stocks",
                },
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
        resolved = services.companion_search.build(anchor, kind)
        run = await services.search.new(
            resolved, work.staged, context.retailer, alongside=False, asked=False
        )
        if run.grounding is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="catalog_unavailable")
        return search_outcome(
            run, {"goes_with": product_facts(anchor, None), "shown_kind": kind.label}
        )

    return ZoryTool(
        name="what_goes_with",
        description=(
            "What goes with a piece they chose. Without show_kind: the reviewed pairings the "
            "store stocks, the most stocked first - you choose which suits it best, or "
            "another kind store_shelf lists. With show_kind: products of that kind, leaning "
            "to the piece's look. Never its own kind (that is more_like_this)."
        ),
        args=GoesWithArgs,
        owners=SALMAN,
        handler=goes_with,
    )


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
        change = await services.cart_operations.add(
            work.staged, context.retailer, wanted, whole=True
        )
        took = change.added or change.already
        state = as_room_in_cart(work.staged, change.state) if took else change.state
        names = await _names(services, tuple(p for p, _ in wanted), context)
        facts = {
            "added": [names.get(p) for p in change.added],
            "refused_by_store": [
                {"name": names.get(r.product_id), "reason": r.reason} for r in change.refused
            ],
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
