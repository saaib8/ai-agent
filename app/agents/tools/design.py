"""Nora's tools: design advice, fit, and the room she designs.

Design judgement is the specialist's, rooms are the room engine's, and every
figure comes from code. A room's pieces are numbered as its cards show them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import Field, create_model

from app.agents.context import ToolContext
from app.agents.facts import design_notes, product_facts
from app.agents.operations.rooms import RoomOutcome, RoomPlanInputs
from app.agents.payloads import OptionsShown, RoomShown
from app.agents.tools.base import ToolArgs, ToolOutcome, ZoryTool, list_of, one_of
from app.agents.tools.schemas import ToolSchemas
from app.agents.tools.shopping import NoArgs, hydrated, resolve_product
from app.agents.work import ToolStatus, TurnWork
from app.core.exceptions import IntegrationUnavailableError, LLMResponseInvalidError
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import RoomProjectState, SwapBudgetOfferState
from app.schemas.design import (
    MAX_DESIGN_QUESTION_CHARS,
    MAX_REGULAR_SEATING_COUNT,
    DesignTask,
    InteriorDesignRequest,
)
from app.schemas.dimensions import CENTIMETRES_PER_UNIT, DimensionUnit
from app.schemas.discovery import PriceConstraint
from app.schemas.geometry import RoomGeometry, RoomMeasurement, RoomMeasurementRole
from app.schemas.product import ProductCandidate
from app.schemas.product_reference import ProductReferenceSelector
from app.services.agent_state import remember_next_sizes
from app.services.design_facts import project_anchors
from app.services.fit import fit_checks, fit_words, floor_share, room_spaces
from app.services.room_composition import piece_for
from app.taxonomy.rooms import RoomPieces

NORA = frozenset({AgentPersona.NORA})
DESIGNERS = frozenset({AgentPersona.NORA, AgentPersona.SALMAN})
"""Salman finishes a room Nora designed; both may add a piece to it."""

Unit = Literal["cm", "m", "mm", "in", "ft"]


class Measure(ToolArgs):
    value: Decimal = Field(gt=0)
    unit: Unit = "cm"

    def cm(self) -> Decimal:
        factor = CENTIMETRES_PER_UNIT[DimensionUnit(self.unit)]
        return (self.value * factor).quantize(Decimal("0.1"))


class RoomSize(ToolArgs):
    length: Measure | None = None
    width: Measure | None = None
    wall: Measure | None = Field(default=None, description="A wall a piece must fit along.")


def geometry_of(size: RoomSize | None) -> RoomGeometry | None:
    if size is None:
        return None
    roles = (
        (RoomMeasurementRole.ROOM_LENGTH, size.length),
        (RoomMeasurementRole.ROOM_WIDTH, size.width),
        (RoomMeasurementRole.USABLE_WALL, size.wall),
    )
    measurements = tuple(
        RoomMeasurement(role=role, centimetres=m.cm()) for role, m in roles if m is not None
    )
    return RoomGeometry(measurements=measurements) if measurements else None


class Budget(ToolArgs):
    amount: Decimal = Field(gt=0, description="The most they will spend on the room.")
    currency: str = Field(min_length=3, max_length=3)

    def constraint(self) -> PriceConstraint:
        return PriceConstraint(currency=self.currency, max_amount=self.amount)


# ── advice and fit ──────────────────────────────────────────────────────────


class AdviceArgs(ToolArgs):
    question: str = Field(min_length=1, max_length=400, description="Their question, plainly.")
    room_type: str | None = Field(default=None, max_length=60)
    size: RoomSize | None = None
    about: list[ProductReferenceSelector] = Field(
        default_factory=list, max_length=6, description="Products the question is about."
    )
    alongside: list[Literal["cart", "liked", "room"]] = Field(
        default_factory=list,
        description=(
            "What they already have that the answer should be judged against - their "
            "cart, what they liked, the room being designed - so the specialist says "
            "whether a piece goes with it."
        ),
    )


MAX_ALONGSIDE = 12
"""Products the specialist sees beside the ones asked about."""


def _alongside(
    work: TurnWork, wanted: Sequence[str], asked: Sequence[int]
) -> list[tuple[str, int]]:
    """Each product they already have, with where it is - in their cart, liked,
    or a piece of the room - leaving out the ones asked about."""
    state = work.staged
    sources: dict[str, Sequence[int]] = {
        "in their cart": state.product_interaction.selected_product_ids,
        "liked": state.product_interaction.liked_product_ids,
        "in the room": tuple(
            line.product_id
            for line in (state.room_project.bundle_items if state.room_project else ())
        ),
    }
    keys = {"cart": "in their cart", "liked": "liked", "room": "in the room"}
    found: dict[int, str] = {}
    for key in dict.fromkeys(wanted):
        for product_id in sources[keys[key]]:
            if product_id not in asked and product_id not in found:
                found[product_id] = keys[key]
    return [(role, pid) for pid, role in found.items()][:MAX_ALONGSIDE]


def advice_tool(schemas: ToolSchemas) -> ZoryTool[AdviceArgs]:
    async def advise(args: AdviceArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        if services.design is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="design_unavailable")
        ids: list[int] = []
        for which in args.about:
            product = await resolve_product(services, which, work, context)
            if isinstance(product, ToolOutcome):
                return product
            ids.append(product)
        beside = _alongside(work, args.alongside, ids)
        wanted = (*ids, *(pid for _, pid in beside))
        everything = await hydrated(services, wanted, context) if wanted else ()
        if everything is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="catalog_unavailable")
        by_id = {p.product_id: p for p in everything}
        products = tuple(by_id[i] for i in ids if i in by_id)
        roles = [(role, by_id[pid]) for role, pid in beside if pid in by_id]
        question = args.question
        if roles:
            # Who is which, by name: the specialist judges the asked-about
            # pieces against what they already have.
            question += "\n\nAsked about: " + (
                "; ".join(p.name_english for p in products) or "the room as a whole"
            )
            question += "\nAlready theirs: " + "; ".join(
                f"{p.name_english} ({role})" for role, p in roles
            )
        room = work.staged.room_project
        geometry = geometry_of(args.size) or (room.geometry if room else None)
        measured = fit_checks(products, room_spaces(geometry))
        try:
            designed = await services.design.plan(
                InteriorDesignRequest(
                    task=DesignTask.GENERAL_ADVICE,
                    question=question[:MAX_DESIGN_QUESTION_CHARS],
                    room_type=args.room_type or (room.room_type if room else None),
                    geometry=geometry,
                    layout=room.layout if room else (),
                    fit_checks=measured,
                    floor_taken_percent=floor_share([p for _, p in roles], geometry)
                    if roles
                    else None,
                    anchors=project_anchors(
                        (*products, *(p for _, p in roles)), dimensions=services.dimensions
                    ),
                )
            )
        except (LLMResponseInvalidError, IntegrationUnavailableError):
            return ToolOutcome(status=ToolStatus.REFUSED, code="design_unavailable")
        facts = {
            "guidance": [
                {
                    "topic": str(g.topic),
                    "advice": g.summary,
                    "rules_of_thumb": [
                        {"label": m.label, "min_cm": m.minimum_cm, "max_cm": m.maximum_cm}
                        for m in g.measurements
                    ],
                }
                for g in designed.guidance
            ],
            "products": [product_facts(p, None) for p in products],
            **(
                {"judged_with": [{"where": role, **product_facts(p, None)} for role, p in roles]}
                if roles
                else {}
            ),
            **design_notes(designed),
        }
        if measured:
            # The figures Nora may quote: each piece against each wall and
            # doorway, as measured - never one she works out herself.
            facts["measured_fit"] = [
                {
                    "piece": products[c.piece - 1].name_english,
                    "against": c.label or c.space,
                    "fit": fit_words(c),
                }
                for c in measured
            ]
        state = remember_next_sizes(work.staged, designed.next_sizes)
        return ToolOutcome(facts=facts, state=state if state is not work.staged else None)

    return ZoryTool(
        name="design_advice",
        description=(
            "Ask the interior-design specialist: layout, sizing, whether a piece fits a "
            "wall, doorway or room, colour and style pairing, whether a piece goes with "
            "what they already have (alongside: their cart, liked products, the room), "
            "what a room needs. Give the products and measurements the question is "
            "about; its figures are rules of thumb, never facts about their room."
        ),
        args=AdviceArgs,
        owners=NORA,
        handler=advise,
    )


# ── rooms ───────────────────────────────────────────────────────────────────


_ROOM_KIND = (
    "Set it whenever the room is one of these. Only then does a call without pieces list "
    "what the room could hold and build nothing; a room of no listed kind is designed at "
    "once from what you pass."
)


def room_plan_args(schemas: ToolSchemas) -> type[ToolArgs]:
    rooms = schemas.room_pieces
    kinds = tuple(rooms.kinds) if rooms is not None else ()
    keys = tuple(
        sorted(
            {
                piece.key
                for kind in kinds
                if rooms is not None and (template := rooms.template(kind)) is not None
                for piece in template.pieces
            }
        )
    )
    colour = one_of(tuple(sorted(schemas.attributes.colors)))
    style = one_of(tuple(sorted(schemas.attributes.styles)))
    fields: dict[str, Any] = {
        "room_type": (str, Field(min_length=1, max_length=60, description="The room, in words.")),
        "budget": (Budget | None, None),
        "size": (RoomSize | None, None),
        "seats": (
            int | None,
            Field(default=None, ge=1, le=MAX_REGULAR_SEATING_COUNT, description="Who sits."),
        ),
        "colours": (list_of(colour), Field(default_factory=list)),
        "styles": (list_of(style), Field(default_factory=list)),
        "around_picks": (bool, Field(default=False, description="Build it around their cart.")),
        "same_room": (
            bool | None,
            Field(
                default=None,
                description=(
                    "True to change the room already being designed, false for a new room "
                    "- however they name it ('the lounge' for the living room)."
                ),
            ),
        ),
        "room_kind": (
            (one_of(kinds) | None, Field(default=None, description=_ROOM_KIND))
            if kinds
            else (str | None, None)
        ),
        "pieces": (
            list_of(one_of(keys)) | None,
            Field(
                default=None,
                description=(
                    "The pieces the room holds, chosen with the customer. Omit to be told "
                    "what this room could hold here, each with how much such a room needs "
                    "it (essential, recommended, optional) - nothing is built until you "
                    "choose. A short budget gives up optional pieces first."
                ),
            ),
        ),
    }
    return cast(type[ToolArgs], create_model("PlanRoomArgs", __base__=ToolArgs, **fields))


def room_outcome(outcome: RoomOutcome) -> ToolOutcome:
    ok = outcome.code == "ok"
    view = outcome.presentation()
    shown = ok or outcome.committed or outcome.code in {"over_budget", "choose_pieces"}
    return ToolOutcome(
        status=ToolStatus.OK if shown else ToolStatus.REFUSED,
        code=None if ok else outcome.code,
        facts={**outcome.facts, "committed": outcome.committed},
        state=outcome.state if outcome.committed else None,
        # A proposal that waits on their yes is described, never drawn: the
        # room on screen stays the room they have.
        payload=RoomShown(room=view) if view is not None and (ok or outcome.committed) else None,
    )


def plan_room_tool(schemas: ToolSchemas) -> ZoryTool[Any]:
    async def plan(args: Any, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        inputs = RoomPlanInputs(
            room_kind=args.room_kind,
            room_type=args.room_type,
            budget=args.budget.constraint() if args.budget else None,
            geometry=geometry_of(args.size),
            pieces=tuple(args.pieces) if args.pieces else None,
            seats=args.seats,
            colours=tuple(args.colours),
            styles=tuple(args.styles),
            around_picks=args.around_picks,
            same_room=args.same_room,
        )
        return room_outcome(await services.rooms.plan(work.staged, context.retailer, inputs))

    return ZoryTool(
        name="plan_room",
        description=(
            "Design a whole room from the store's real products within a budget: the "
            "designer shapes it, discovery finds each piece, the optimiser chooses the set. "
            "Planning again with changed inputs replans the same room, keeping locked pieces."
        ),
        args=room_plan_args(schemas),
        owners=NORA,
        handler=plan,
    )


class ReviseArgs(ToolArgs):
    budget: Budget | None = None
    size: RoomSize | None = None
    seats: int | None = Field(default=None, ge=1, le=MAX_REGULAR_SEATING_COUNT)
    colours: list[str] | None = Field(
        default=None, max_length=6, description="Approved colour names; replaces the room's."
    )
    styles: list[str] | None = Field(
        default=None, max_length=6, description="Approved style names; replaces the room's."
    )


def revise_room_tool(schemas: ToolSchemas) -> ZoryTool[ReviseArgs]:
    async def revise(args: ReviseArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        return room_outcome(
            await services.rooms.revise(
                work.staged,
                context.retailer,
                budget=args.budget.constraint() if args.budget else None,
                geometry=geometry_of(args.size),
                seats=args.seats,
                colours=tuple(args.colours) if args.colours is not None else None,
                styles=tuple(args.styles) if args.styles is not None else None,
            )
        )

    return ZoryTool(
        name="revise_room",
        description=(
            "Change the room's budget, size, head count, colours or styles and re-choose its "
            "pieces; what you leave out stays as it is."
        ),
        args=ReviseArgs,
        owners=DESIGNERS,
        handler=revise,
    )


class PieceArgs(ToolArgs):
    piece: int = Field(ge=1, description="The room piece, by its card's place in the room.")


class PieceOptionsArgs(PieceArgs):
    cheaper: bool = False


def piece_options_tool(schemas: ToolSchemas) -> ZoryTool[PieceOptionsArgs]:
    async def options(args: PieceOptionsArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        found = await services.rooms.piece_options(
            work.staged, context.retailer, args.piece, cheaper=args.cheaper
        )
        ok = found.code == "ok"
        return ToolOutcome(
            status=ToolStatus.OK if ok else ToolStatus.REFUSED,
            code=None if ok else found.code,
            facts=found.facts,
            state=found.state if ok else None,
            payload=OptionsShown(products=found.products) if found.products else None,
        )

    return ZoryTool(
        name="room_piece_options",
        description="Alternatives for one room piece, with the room's total if swapped.",
        args=PieceOptionsArgs,
        owners=DESIGNERS,
        handler=options,
    )


class SwapArgs(PieceArgs):
    option: int = Field(ge=1, description="The option, as room_piece_options numbered it.")


def _held(
    outcome: RoomOutcome,
    work: TurnWork,
    *,
    piece: int | None,
    product_id: int | None,
    rooms: RoomPieces | None = None,
) -> ToolOutcome:
    """A change that would take the room over budget: nothing changes, and the
    offer is kept so the customer's yes can be honoured - and only it."""
    told = room_outcome(outcome)
    room = work.staged.room_project
    if outcome.code != "over_budget" or room is None or product_id is None:
        return told
    bundle = outcome.bundle
    added = (
        next((line.product for line in bundle.lines if line.product.product_id == product_id), None)
        if bundle is not None and piece is None
        else None
    )
    offer = SwapBudgetOfferState(
        bundle_ordinal=piece,
        added_piece=None if piece is not None else _added_piece(added, room, rooms),
        chosen_product_id=product_id,
        bundle_revision=room.bundle_revision,
        room_total=bundle.new_spend_total if bundle is not None else None,
        room_currency=bundle.currency if bundle is not None else None,
    )
    return replace(
        told,
        facts={**(told.facts or {}), "needs_their_yes": True},
        state=work.staged.model_copy(update={"swap_budget_offer": offer}),
    )


def _added_piece(
    added: ProductCandidate | None, room: RoomProjectState, rooms: RoomPieces | None
) -> str:
    """The piece an addition fills, as the room registry names it - the same
    words `add_to_room` reports - else the product's own kind."""
    if added is None:
        return "piece"
    template = rooms.template(room.room_kind) if rooms is not None else None
    filled = (
        piece_for(template, added.commerce.category, added.commerce.subcategory)
        if template is not None
        else None
    )
    if filled is not None:
        return filled.commerce_subcategory or filled.commerce_category
    return added.commerce.subcategory or added.commerce.category or "piece"


def swap_piece_tool(schemas: ToolSchemas) -> ZoryTool[SwapArgs]:
    async def swap(args: SwapArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        shown = work.staged.product_interaction.presented_product_ids
        product_id = shown[args.option - 1] if args.option <= len(shown) else None
        outcome = await services.rooms.swap(
            work.staged, context.retailer, args.piece, args.option, allow_over_budget=False
        )
        return _held(outcome, work, piece=args.piece, product_id=product_id)

    return ZoryTool(
        name="swap_room_piece",
        description=(
            "Put one of a piece's options into the room in its place. If it takes the room "
            "over budget nothing changes: tell them by how much and ask; their yes is "
            "accept_over_budget."
        ),
        args=SwapArgs,
        owners=DESIGNERS,
        handler=swap,
    )


def accept_over_budget_tool(schemas: ToolSchemas) -> ZoryTool[NoArgs]:
    async def accept(args: NoArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        offer = work.staged.swap_budget_offer
        room = work.staged.room_project
        if offer is None or room is None or offer.bundle_revision != room.bundle_revision:
            return ToolOutcome(status=ToolStatus.REFUSED, code="no_offer_to_accept")
        if offer.bundle_ordinal is None:
            outcome = await services.rooms.add_product(
                work.staged, context.retailer, offer.chosen_product_id, allow_over_budget=True
            )
        else:
            shown = work.staged.product_interaction.presented_product_ids
            if offer.chosen_product_id not in shown:
                return ToolOutcome(status=ToolStatus.REFUSED, code="options_expired")
            outcome = await services.rooms.swap(
                work.staged,
                context.retailer,
                offer.bundle_ordinal,
                shown.index(offer.chosen_product_id) + 1,
                allow_over_budget=True,
            )
        done = room_outcome(outcome)
        if outcome.committed:
            done = replace(done, state=outcome.state.model_copy(update={"swap_budget_offer": None}))
        return done

    return ZoryTool(
        name="accept_over_budget",
        description=(
            "They said yes to going over the budget for the change you offered: make it, "
            "and raise the room's budget to its new total."
        ),
        args=NoArgs,
        owners=DESIGNERS,
        handler=accept,
    )


def decline_over_budget_tool(schemas: ToolSchemas) -> ZoryTool[NoArgs]:
    async def decline(args: NoArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        if work.staged.swap_budget_offer is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="no_offer_to_decline")
        return ToolOutcome(state=work.staged.model_copy(update={"swap_budget_offer": None}))

    return ZoryTool(
        name="decline_over_budget",
        description="They said no to going over the budget: the room stays as it is.",
        args=NoArgs,
        owners=DESIGNERS,
        handler=decline,
    )


def remove_piece_tool(schemas: ToolSchemas) -> ZoryTool[PieceArgs]:
    async def remove(args: PieceArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        return room_outcome(await services.rooms.remove(work.staged, context.retailer, args.piece))

    return ZoryTool(
        name="remove_room_piece",
        description="Take one piece out of the room; every other piece stays as it is.",
        args=PieceArgs,
        owners=DESIGNERS,
        handler=remove,
    )


class PieceStatusArgs(PieceArgs):
    status: Literal["locked", "unlocked", "owned"]


def piece_status_tool(schemas: ToolSchemas) -> ZoryTool[PieceStatusArgs]:
    async def mark(args: PieceStatusArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        return room_outcome(
            await services.rooms.set_piece_status(
                work.staged, context.retailer, args.piece, args.status
            )
        )

    return ZoryTool(
        name="set_room_piece",
        description="Keep a piece whatever else changes (locked), free it, or mark it owned.",
        args=PieceStatusArgs,
        owners=DESIGNERS,
        handler=mark,
    )


def room_additions_tool(schemas: ToolSchemas) -> ZoryTool[NoArgs]:
    async def additions(args: NoArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        keys = await services.rooms.pieces_it_could_take(work.staged, context.retailer)
        rooms = services.room_pieces
        room = work.staged.room_project
        template = rooms.template(room.room_kind) if rooms and room else None
        pieces = [
            {
                "piece": key,
                "kind": p.commerce_subcategory or p.commerce_category,
                "label": p.label,
                **({"label_ar": p.label_ar} if p.label_ar else {}),
                "tier": str(p.tier),
            }
            for key in keys
            if template and (p := template.piece(key))
        ]
        return ToolOutcome(facts={"could_add": pieces})

    return ZoryTool(
        name="room_could_add",
        description=(
            "Every piece the store stocks that the room does not hold yet, with how much "
            "such a room needs it. You choose which would finish this room best and in what "
            "order to suggest them; show one with search_products, then add_to_room."
        ),
        args=NoArgs,
        owners=DESIGNERS,
        handler=additions,
    )


class AddToRoomArgs(ToolArgs):
    which: ProductReferenceSelector


def add_to_room_tool(schemas: ToolSchemas) -> ZoryTool[AddToRoomArgs]:
    async def add(args: AddToRoomArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        product = await resolve_product(services, args.which, work, context)
        if isinstance(product, ToolOutcome):
            return product
        outcome = await services.rooms.add_product(
            work.staged, context.retailer, product, allow_over_budget=False
        )
        return _held(
            outcome, work, piece=None, product_id=product, rooms=context.services.room_pieces
        )

    return ZoryTool(
        name="add_to_room",
        description=(
            "Add a product on screen to the room package (not the cart). Over budget, "
            "nothing changes until they agree (accept_over_budget)."
        ),
        args=AddToRoomArgs,
        owners=DESIGNERS,
        handler=add,
    )


def show_room_tool(schemas: ToolSchemas) -> ZoryTool[NoArgs]:
    async def show(args: NoArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        current = await services.rooms.current(work.staged, context.retailer)
        if current is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="no_room")
        return room_outcome(current)

    return ZoryTool(
        name="show_room",
        description="The room designed so far, on screen, with its pieces and total.",
        args=NoArgs,
        owners=DESIGNERS,
        handler=show,
    )


def design_tools(schemas: ToolSchemas) -> list[ZoryTool[Any]]:
    return [
        advice_tool(schemas),
        plan_room_tool(schemas),
        revise_room_tool(schemas),
        piece_options_tool(schemas),
        swap_piece_tool(schemas),
        accept_over_budget_tool(schemas),
        decline_over_budget_tool(schemas),
        remove_piece_tool(schemas),
        piece_status_tool(schemas),
        room_additions_tool(schemas),
        add_to_room_tool(schemas),
        show_room_tool(schemas),
    ]
