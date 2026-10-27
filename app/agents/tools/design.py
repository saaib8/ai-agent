"""Nora's tools: design advice, fit, and the room she designs.

Design judgement is Nora's, written as checked arguments; finding the products
is the room engine's, and every figure comes from code. A room's pieces are
numbered as its cards show them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import Field, create_model, model_validator

from app.agents.context import ToolContext
from app.agents.facts import product_facts
from app.agents.operations.rooms import PieceRevision, RoomDesignInputs, RoomOutcome
from app.agents.payloads import OptionsShown, RoomShown
from app.agents.services import AgentServices
from app.agents.tools.base import ToolArgs, ToolOutcome, ZoryTool, list_of, one_of
from app.agents.tools.schemas import ToolSchemas
from app.agents.tools.shelf import _room_hints
from app.agents.tools.shopping import NoArgs, hydrated, resolve_product
from app.agents.work import ToolStatus, TurnWork
from app.core.exceptions import IntegrationUnavailableError
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import MAX_ROOM_NOTE_CHARS, RoomProjectState, SwapBudgetOfferState
from app.schemas.catalog_overview import CatalogOverview
from app.schemas.design import (
    MAX_NEXT_SIZES,
    MAX_PLACEMENTS,
    MAX_REGULAR_SEATING_COUNT,
    InteriorDesignResult,
    NextSize,
    PieceDesign,
    Placement,
)
from app.schemas.dimensions import CENTIMETRES_PER_UNIT, DimensionUnit
from app.schemas.discovery import PriceConstraint
from app.schemas.geometry import RoomGeometry, RoomMeasurement, RoomMeasurementRole
from app.schemas.product import ProductCandidate
from app.schemas.product_reference import ProductReferenceSelector
from app.services.agent_state import remember_next_sizes, remember_placements
from app.services.bundle_cards import group_bundle_cards
from app.services.design_checks import usable_next_sizes
from app.services.fit import fit_checks, fit_words, floor_share, knows_room_size, room_spaces
from app.services.room_design import (
    MAX_PIECE_QUANTITY,
    MAX_ROOM_PIECES,
    MAX_SEATS_EACH,
    PieceSpec,
    RoomDesignChecker,
    RoomDesignProblem,
)
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.rooms import RoomPieces
from app.taxonomy.words import customer_words

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


class FitArgs(ToolArgs):
    about: list[ProductReferenceSelector] = Field(
        default_factory=list, max_length=6, description="The pieces to measure."
    )
    alongside: list[Literal["cart", "liked", "room"]] = Field(
        default_factory=list,
        description=(
            "What they already have, to judge the pieces beside: their cart, what they "
            "liked, the room being designed."
        ),
    )
    size: RoomSize | None = Field(
        default=None, description="A room size they just gave, when not recorded yet."
    )


MAX_ALONGSIDE = 12
"""Products given beside the ones measured."""


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


def measure_fit_tool(schemas: ToolSchemas) -> ZoryTool[FitArgs]:
    """The facts a design judgement needs, measured - never the judgement.

    Whether a piece looks right, goes with what they have or works in their
    room is Nora's to say. This gives her the pieces as they are, what they
    already have, and each piece against each of their walls and doorways, so
    every figure she quotes is one code measured (CLAUDE.md 10.6).
    """

    async def measure(args: FitArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
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
        room = work.staged.room_project
        geometry = geometry_of(args.size) or (room.geometry if room else None)
        measured = fit_checks(products, room_spaces(geometry))
        facts: dict[str, object] = {
            "products": [product_facts(p, None) for p in products],
            "room_size_known": knows_room_size(geometry),
        }
        if roles:
            facts["already_theirs"] = [
                {"where": role, **product_facts(p, None)} for role, p in roles
            ]
            taken = floor_share([p for _, p in roles], geometry)
            if taken is not None:
                facts["floor_taken_now_percent"] = taken
        if measured:
            # The figures that may be quoted: each piece against each wall and
            # doorway, as measured - never one worked out in a reply.
            facts["measured_fit"] = [
                {
                    "piece": products[c.piece - 1].name_english,
                    "against": c.label or c.space,
                    "fit": fit_words(c),
                }
                for c in measured
            ]
        return ToolOutcome(facts=facts)

    return ZoryTool(
        name="measure_fit",
        description=(
            "Measure pieces for a design judgement: each against their walls and doorway, "
            "how much floor what they have takes, and the pieces they already have "
            "(alongside: cart, liked, room). You judge whether it looks right and works; "
            "these are the only fit figures you may quote."
        ),
        args=FitArgs,
        owners=NORA,
        handler=measure,
    )


class DesignNotesArgs(ToolArgs):
    placements: list[Placement] = Field(default_factory=list, max_length=MAX_PLACEMENTS)
    next_sizes: list[NextSize] = Field(default_factory=list, max_length=MAX_NEXT_SIZES)


def design_notes_tool(schemas: ToolSchemas) -> ZoryTool[DesignNotesArgs]:
    """Where pieces go in their room and the size the next pieces should be -
    Nora's judgement, kept on the room so every agent searches at her size on a
    later turn, never at one it made up (CLAUDE.md 10.6)."""

    async def record(args: DesignNotesArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        room = work.staged.room_project
        if room is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="no_room_on_record")
        # Words only: a placement with a figure in it is dropped on its own,
        # the rest of the notes stand (CLAUDE.md 10.6, 21.1).
        placements = tuple(p for p in args.placements if not p.states_a_figure())
        broken = [p.piece for p in args.placements if p.states_a_figure()]
        taxonomy = context.services.taxonomy
        # A size's family is the registry's, never the agent's (CLAUDE.md 17.1).
        filed = [
            size.model_copy(update={"commerce_category": family})
            if size.commerce_subcategory
            and (family := taxonomy.category_of(size.commerce_subcategory)) is not None
            else size
            for size in args.next_sizes
        ]
        sizes = usable_next_sizes(filed, taxonomy)
        dropped = len(args.next_sizes) - len(sizes)
        state = remember_placements(remember_next_sizes(work.staged, sizes), placements)
        # What was kept, by piece and kind - never the figures she wrote, which
        # would then pass the number check as a tool's own facts. The brief
        # carries her sizes for the next search (designer_size).
        facts: dict[str, object] = {
            "placements_kept": [p.piece for p in placements],
            "sizes_kept_for": [
                customer_words(s.commerce_subcategory or s.commerce_category) for s in sizes
            ],
        }
        if broken:
            facts["placements_dropped"] = {
                "pieces": broken,
                "why": "a placement is words only - its figure belongs to a size",
            }
        if dropped:
            facts["sizes_dropped"] = (
                "a size names no longer side, is out of order, states a figure in its "
                "reason, or is not an approved kind"
            )
        return ToolOutcome(facts=facts, state=state)

    return ZoryTool(
        name="record_design_notes",
        description=(
            "Keep your design decisions for their room: where each piece goes (which of "
            "their walls, beside what, why - words only) and the size the next pieces "
            "should be (longer and shorter side ranges in cm, with a reason). Searches "
            "with designer_size then sit near your size."
        ),
        args=DesignNotesArgs,
        owners=NORA,
        handler=record,
    )


# ── rooms ───────────────────────────────────────────────────────────────────


_PIECE_TYPES = (
    "The approved types that may fill this piece, the one you prefer first - several "
    "when they would do equally (an armchair, lounge chair or accent chair for a reading "
    "seat). One family per piece."
)


def room_design_args(schemas: ToolSchemas) -> type[ToolArgs]:
    """Nora's room design as arguments: approved colours and styles only;
    types are checked against the registry when the room is built."""
    rooms = schemas.room_pieces
    kinds = tuple(rooms.kinds) if rooms is not None else ()
    colour = one_of(tuple(sorted(schemas.attributes.colors)))
    style = one_of(tuple(sorted(schemas.attributes.styles)))
    piece_fields: dict[str, Any] = {
        "role": (
            str,
            Field(min_length=1, max_length=40, description="What it is in the room, in words."),
        ),
        "types": (list[str], Field(min_length=1, max_length=4, description=_PIECE_TYPES)),
        "quantity": (int, Field(default=1, ge=1, le=MAX_PIECE_QUANTITY)),
        "priority": (
            Literal["must", "nice"],
            Field(
                default="must",
                description="must: the room does not work without it. nice: a refinement, "
                "given up first when the budget is short.",
            ),
        ),
        "colours": (list_of(colour), Field(default_factory=list, max_length=3)),
        "styles": (list_of(style), Field(default_factory=list, max_length=2)),
        "avoid_colours": (list_of(colour), Field(default_factory=list, max_length=2)),
        "avoid_styles": (list_of(style), Field(default_factory=list, max_length=2)),
        "feel": (
            str | None,
            Field(default=None, max_length=120, description="Its character, in words."),
        ),
        "longer_min_cm": (int | None, Field(default=None, ge=10, le=1200)),
        "longer_max_cm": (int | None, Field(default=None, ge=10, le=1200)),
        "shorter_min_cm": (
            int | None,
            Field(
                default=None,
                ge=10,
                le=1200,
                description="Its shorter floor side - a bed's width, a rug's short side.",
            ),
        ),
        "shorter_max_cm": (int | None, Field(default=None, ge=10, le=1200)),
        "share_percent": (
            int | None,
            Field(
                default=None,
                ge=1,
                le=100,
                description="How much of the budget this piece's line should take. Shares "
                "add up to at most 100.",
            ),
        ),
        "seats_each": (
            int | None,
            Field(
                default=None,
                ge=2,
                le=MAX_SEATS_EACH,
                description="How many one of it seats - only for a type that seats several.",
            ),
        ),
        "wall": (
            str | None,
            Field(default=None, max_length=40, description="Their name for the wall it goes on."),
        ),
        "beside": (
            str | None,
            Field(default=None, max_length=40, description="The role of the piece beside it."),
        ),
        "from_pick": (
            int | None,
            Field(
                default=None,
                ge=1,
                description="The cart place of their pick that is this piece - it is kept.",
            ),
        ),
    }
    piece = create_model("RoomPieceDesign", __base__=ToolArgs, **piece_fields)
    fields: dict[str, Any] = {
        "room_type": (str, Field(min_length=1, max_length=60, description="The room, in words.")),
        "room_kind": (
            (one_of(kinds) | None, Field(default=None, description="The room, if one of these."))
            if kinds
            else (str | None, None)
        ),
        "same_room": (
            bool,
            Field(
                description="True to design again the room on record, keeping its locked pieces "
                "- whatever they call it ('the lounge'); false for another room."
            ),
        ),
        "budget": (Budget | None, None),
        "size": (RoomSize | None, None),
        "head_count": (
            int | None,
            Field(default=None, ge=1, le=MAX_REGULAR_SEATING_COUNT, description="Who sits there."),
        ),
        "palette": (
            list_of(colour),
            Field(default_factory=list, max_length=5, description="The room's colours."),
        ),
        "styles": (list_of(style), Field(default_factory=list, max_length=2)),
        "around_picks": (
            bool,
            Field(default=False, description="Keep every pick in their cart that is a piece."),
        ),
        "replace_room": (
            bool,
            Field(
                default=False,
                description="Only when they said to start this room in place of the one being "
                "designed (another_room_in_progress).",
            ),
        ),
        "pieces": (list[piece], Field(min_length=1, max_length=MAX_ROOM_PIECES)),
        "why": (
            str | None,
            Field(default=None, max_length=200, description="The idea of the room, in words."),
        ),
        "layout": (
            str | None,
            Field(
                default=None,
                max_length=MAX_ROOM_NOTE_CHARS,
                description="How the pieces are arranged, in a sentence or two of your words "
                "- any arrangement the room and its purpose call for ('an L in the corner "
                "by the window, a second sofa facing the TV'). Words only.",
            ),
        ),
    }
    return cast(type[ToolArgs], create_model("DesignRoomArgs", __base__=ToolArgs, **fields))


def design_room_tool(schemas: ToolSchemas) -> ZoryTool[Any]:
    async def design(args: Any, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        try:
            overview = await services.capabilities.overview(context.retailer)
        except IntegrationUnavailableError:
            return ToolOutcome(status=ToolStatus.REFUSED, code="catalog_unavailable")
        current = work.staged.room_project
        # The room on record counts only when this is that room - or one only
        # noted so far, which the engine designs as the same room.
        room = (
            current
            if current is not None
            and (args.same_room or (not current.bundle_items and not current.in_cart))
            else None
        )
        geometry = geometry_of(args.size)
        walls = [w.wall for w in room.layout] if room is not None else []
        walls += [
            s.label for s in room_spaces(geometry or (room.geometry if room else None)) if s.label
        ]
        checked = RoomDesignChecker(services.taxonomy, services.attributes, services.seating).check(
            [_spec(p) for p in args.pieces],
            overview,
            head_count=args.head_count or (room.regular_seating_count if room else None),
            walls=walls,
            texts=(args.why, args.layout),
        )
        if isinstance(checked, RoomDesignProblem):
            # Our rule broken - said with what is right, so she corrects it.
            facts: dict[str, object] = dict(checked.detail)
            if checked.piece is not None:
                facts["piece"] = checked.piece
            kind = facts.get("type")
            if isinstance(kind, str):
                facts["approved_types_like_it"] = "store_shelf lists the kinds this store stocks"
            return ToolOutcome(status=ToolStatus.INVALID, code=checked.code, facts=facts)
        if not checked.needs:
            return ToolOutcome(
                status=ToolStatus.REFUSED, code="nothing_stocked", facts=checked.facts
            )
        inputs = RoomDesignInputs(
            room_type=args.room_type,
            room_kind=args.room_kind,
            same_room=args.same_room,
            budget=args.budget.constraint() if args.budget else None,
            geometry=geometry,
            head_count=args.head_count,
            palette=tuple(args.palette),
            styles=tuple(args.styles),
            plan=InteriorDesignResult(needs=checked.needs),
            pick_places=checked.picks,
            around_picks=args.around_picks,
            replace_room=args.replace_room,
            layout_idea=args.layout,
        )
        outcome = room_outcome(await services.rooms.design(work.staged, context.retailer, inputs))
        return replace(
            outcome,
            facts={
                **(outcome.facts or {}),
                "design": checked.facts,
                **_still_to_add(services, outcome, overview),
            },
        )

    return ZoryTool(
        name="design_room",
        description=(
            "Build the whole room you designed from the store's real products: every piece "
            "with the types that may fill it, how many, its colours, styles, size, share of "
            "the budget and wall. Code finds the best match for each piece and keeps the "
            "room within the budget. Changing the budget, size or look: design it again with "
            "same_room true, restating the pieces."
        ),
        args=room_design_args(schemas),
        owners=NORA,
        handler=design,
    )


def revise_piece_args(schemas: ToolSchemas) -> type[ToolArgs]:
    colour = one_of(tuple(sorted(schemas.attributes.colors)))
    style = one_of(tuple(sorted(schemas.attributes.styles)))
    fields: dict[str, Any] = {
        "piece": (int, Field(ge=1, description="The room piece, by its card's place.")),
        "colours": (list_of(colour) | None, Field(default=None, max_length=3)),
        "styles": (list_of(style) | None, Field(default=None, max_length=2)),
        "avoid_colours": (list_of(colour) | None, Field(default=None, max_length=2)),
        "avoid_styles": (list_of(style) | None, Field(default=None, max_length=2)),
        "feel": (str | None, Field(default=None, max_length=120)),
        "longer_min_cm": (int | None, Field(default=None, ge=10, le=1200)),
        "longer_max_cm": (int | None, Field(default=None, ge=10, le=1200)),
        "shorter_min_cm": (
            int | None,
            Field(
                default=None,
                ge=10,
                le=1200,
                description="Its shorter floor side - a bed's width, a rug's short side.",
            ),
        ),
        "shorter_max_cm": (int | None, Field(default=None, ge=10, le=1200)),
        "share_percent": (int | None, Field(default=None, ge=1, le=100)),
        "wall": (str | None, Field(default=None, max_length=40)),
    }
    revision = create_model("PieceRevisionArgs", __base__=ToolArgs, **fields)
    return cast(
        type[ToolArgs],
        create_model(
            "RevisePieceArgs",
            __base__=_OneOrMoreRevisions,
            revisions=(
                list[revision],
                Field(
                    min_length=1,
                    max_length=MAX_REVISIONS,
                    description=(
                        "Every piece to put right, together: they are found again as one "
                        "change, costed once."
                    ),
                ),
            ),
        ),
    )


MAX_REVISIONS = 2
"""Pieces one correction round may find again."""


class _OneOrMoreRevisions(ToolArgs):
    """A single revision sent flat - `{piece, colours}` rather than inside
    `revisions` - is read as a round of one, not refused as broken arguments."""

    @model_validator(mode="before")
    @classmethod
    def _a_single_revision(cls, data: Any) -> Any:
        if isinstance(data, dict) and "revisions" not in data and "piece" in data:
            return {"revisions": [data]}
        return data


def revise_piece_tool(schemas: ToolSchemas) -> ZoryTool[Any]:
    async def revise(args: Any, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        room = work.staged.room_project
        if room is None or not room.bundle_items:
            return ToolOutcome(status=ToolStatus.REFUSED, code="no_room")
        try:
            overview = await services.capabilities.overview(context.retailer)
        except IntegrationUnavailableError:
            return ToolOutcome(status=ToolStatus.REFUSED, code="catalog_unavailable")
        revisions: list[PieceRevision] = []
        checked_facts: list[dict[str, object]] = []
        for one in args.revisions:
            designed = _revised_design(services, room, overview, one)
            if isinstance(designed, ToolOutcome):
                # Which piece was refused, so the rest of the round is not lost.
                return replace(designed, facts={"piece": one.piece, **(designed.facts or {})})
            design, facts = designed
            revisions.append(PieceRevision(one.piece, design, one.feel))
            checked_facts.append(
                {"piece": one.piece, **(dict(facts) if isinstance(facts, Mapping) else {})}
            )
        outcome = room_outcome(
            await services.rooms.revise_pieces(work.staged, context.retailer, revisions)
        )
        return replace(
            outcome,
            facts={
                **(outcome.facts or {}),
                "revised": checked_facts,
                **_still_to_add(services, outcome, overview),
            },
        )

    return ZoryTool(
        name="revise_room_piece",
        description=(
            "Find pieces of the room again with new directions - their colours, styles, "
            "what to avoid, feel, size, share or wall - every other piece staying the "
            "product it is, within the budget, as one change. What you leave out stays as "
            "designed."
        ),
        args=revise_piece_args(schemas),
        owners=NORA,
        handler=revise,
    )


def _revised_design(
    services: AgentServices, room: RoomProjectState, overview: CatalogOverview, args: Any
) -> tuple[PieceDesign, object] | ToolOutcome:
    """One piece's revised design, checked like a new one - or why not."""
    cards = group_bundle_cards(room.bundle_items)
    card = cards[args.piece - 1] if 1 <= args.piece <= len(cards) else None
    need = (
        next((n for n in room.design_needs if n.need_id == card.need_ids[0]), None)
        if card is not None and len(card.need_ids) == 1
        else None
    )
    if need is None or need.design is None or need.commerce_subcategory is None:
        return ToolOutcome(status=ToolStatus.REFUSED, code="piece_not_designed")
    old = need.design

    def kept(new: Any, before: Any) -> Any:
        return before if new is None else new

    spec = PieceSpec(
        role=old.role,
        types=(need.commerce_subcategory, *old.alternatives),
        quantity=need.quantity,
        colours=tuple(kept(args.colours, old.colours)),
        styles=tuple(kept(args.styles, old.styles)),
        avoid_colours=tuple(kept(args.avoid_colours, old.avoid_colours)),
        avoid_styles=tuple(kept(args.avoid_styles, old.avoid_styles)),
        feel=kept(args.feel, need.semantic_intent),
        longer_min_cm=kept(args.longer_min_cm, old.longer_min_cm),
        longer_max_cm=kept(args.longer_max_cm, old.longer_max_cm),
        shorter_min_cm=kept(args.shorter_min_cm, old.shorter_min_cm),
        shorter_max_cm=kept(args.shorter_max_cm, old.shorter_max_cm),
        share_percent=kept(args.share_percent, old.share_percent),
        wall=kept(args.wall, old.wall),
        beside=old.beside,
    )
    walls = [w.wall for w in room.layout] + [s.label for s in room_spaces(room.geometry) if s.label]
    checked = RoomDesignChecker(services.taxonomy, services.attributes, services.seating).check(
        [spec], overview, head_count=None, walls=walls
    )
    if isinstance(checked, RoomDesignProblem):
        return ToolOutcome(status=ToolStatus.INVALID, code=checked.code, facts=dict(checked.detail))
    design = checked.needs[0].design if checked.needs else None
    if design is None:
        return ToolOutcome(status=ToolStatus.REFUSED, code="nothing_stocked")
    return design, checked.facts


def _still_to_add(
    services: AgentServices, outcome: ToolOutcome, overview: CatalogOverview
) -> dict[str, object]:
    """When a built room leaves budget unspent, the stocked kinds whose
    cheapest piece fits what is left - so Nora sees, as she reviews it, what
    could finish the room within its budget (CLAUDE.md 10.1)."""
    state = outcome.state
    room = state.room_project if state is not None else None
    left = _budget_left(outcome.facts)
    if room is None or left is None or left <= 0:
        return {}
    could = _could_add(services, room, overview, left, only_fitting=True)
    return {"could_still_add": could} if could else {}


def _spec(piece: Any) -> PieceSpec:
    return PieceSpec(
        role=piece.role,
        types=tuple(piece.types),
        quantity=piece.quantity,
        must=piece.priority == "must",
        colours=tuple(piece.colours),
        styles=tuple(piece.styles),
        avoid_colours=tuple(piece.avoid_colours),
        avoid_styles=tuple(piece.avoid_styles),
        feel=piece.feel,
        longer_min_cm=piece.longer_min_cm,
        longer_max_cm=piece.longer_max_cm,
        shorter_min_cm=piece.shorter_min_cm,
        shorter_max_cm=piece.shorter_max_cm,
        share_percent=piece.share_percent,
        seats_each=piece.seats_each,
        wall=piece.wall,
        beside=piece.beside,
        from_pick=piece.from_pick,
    )


def room_outcome(outcome: RoomOutcome) -> ToolOutcome:
    ok = outcome.code == "ok"
    view = outcome.presentation()
    shown = ok or outcome.committed or outcome.code == "over_budget"
    return ToolOutcome(
        status=ToolStatus.OK if shown else ToolStatus.REFUSED,
        code=None if ok else outcome.code,
        facts={**outcome.facts, "committed": outcome.committed},
        state=outcome.state if outcome.committed else None,
        # A proposal that waits on their yes is described, never drawn: the
        # room on screen stays the room they have.
        payload=RoomShown(room=view) if view is not None and (ok or outcome.committed) else None,
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
        added_piece=None if piece is not None else _added_piece(added),
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


def _added_piece(added: ProductCandidate | None) -> str:
    """The kind an addition is - the same words `add_to_room` reports."""
    if added is None:
        return "piece"
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
        if offer.is_change:
            outcome = await services.rooms.change(
                work.staged,
                context.retailer,
                remove_pieces=offer.removed_pieces,
                add_products=offer.added_product_ids,
                allow_over_budget=True,
            )
        elif offer.bundle_ordinal is None:
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


def _could_add(
    services: AgentServices,
    room: RoomProjectState,
    overview: CatalogOverview,
    budget_left: Decimal | None,
    *,
    only_fitting: bool = False,
) -> list[dict[str, object]]:
    """The stocked kinds the room could take - those it holds none of before
    those it already has, then the widest choice first; the room guide's kinds
    and the reviewed pairings are marked, never put first - each with whether
    its cheapest piece fits what the budget has left and whether it comes in the room's colours. A
    kind it already holds stays on the list with how many (`in_room`): a
    second artwork for a long wall or a second lamp is often what finishes a
    room. Facts for Nora to choose from, never a list to add."""
    held: dict[str, int] = {}
    for need in room.design_needs:
        for kind in (need.commerce_subcategory, *(need.design.alternatives if need.design else ())):
            if kind:
                held[kind] = held.get(kind, 0) + need.quantity
    hints = _room_hints(services.room_pieces, room.room_kind)
    paired = {
        c.commerce_subcategory
        for kind in held
        if services.complements
        for c in services.complements.for_type(kind)
    }
    palette = set(room.palette)
    kinds = [
        shelf
        for shelf in overview.shelves
        if shelf.commerce_subcategory
        and (not only_fitting or budget_left is None or shelf.price_minimum <= budget_left)
    ]
    kinds.sort(
        key=lambda s: (
            s.commerce_subcategory in held,
            -s.active_count,
        )
    )
    could: list[dict[str, object]] = []
    for shelf in kinds[:MAX_COULD_ADD]:
        kind = shelf.commerce_subcategory or ""
        entry: dict[str, object] = {"kind": kind, "label": customer_words(kind)}
        if kind in held:
            entry["in_room"] = held[kind]
        if kind in hints:
            entry["in_this_room"] = True
        if kind in paired:
            entry["usually_paired"] = True
        if budget_left is not None:
            entry["fits_budget_left"] = shelf.price_minimum <= budget_left
        if palette:
            colours = {
                services.attributes.canonical(AttributeFamily.COLOR, c) for c in shelf.colours
            }
            entry["in_room_colours"] = bool(colours & palette)
        could.append(entry)
    return could


def _budget_left(facts: Mapping[str, object] | None) -> Decimal | None:
    left = (facts or {}).get("budget_left")
    return left if isinstance(left, Decimal) else None


def room_additions_tool(schemas: ToolSchemas) -> ZoryTool[NoArgs]:
    async def additions(args: NoArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        room = work.staged.room_project
        if room is None or not room.bundle_items:
            return ToolOutcome(status=ToolStatus.REFUSED, code="no_room")
        try:
            overview = await services.capabilities.overview(context.retailer)
        except IntegrationUnavailableError:
            return ToolOutcome(status=ToolStatus.REFUSED, code="catalog_unavailable")
        current = await services.rooms.current(work.staged, context.retailer)
        left = _budget_left(current.facts if current else None)
        facts: dict[str, object] = {"could_add": _could_add(services, room, overview, left)}
        if left is not None:
            facts["budget_left"] = left
        return ToolOutcome(facts=facts)

    return ZoryTool(
        name="room_could_add",
        description=(
            "The kinds the store stocks that could finish the room - those the room guide "
            "often puts in such a room and those reviewed to go with its pieces first, "
            "including kinds it already has (in_room: how many) - with whether each fits "
            "the budget left and comes in the room's colours. You choose which would "
            "finish this room best; show one with search_products, then add_to_room."
        ),
        args=NoArgs,
        owners=DESIGNERS,
        handler=additions,
    )


MAX_COULD_ADD = 20
"""Kinds offered as finishing touches in one answer."""


class AddToRoomArgs(ToolArgs):
    which: ProductReferenceSelector


MAX_CHANGE = 6
"""Pieces taken out, or products put in, by one change."""


class ChangeRoomArgs(ToolArgs):
    remove: list[int] = Field(
        default_factory=list,
        max_length=MAX_CHANGE,
        description="Room pieces to take out, by their card's place in the room.",
    )
    add: list[ProductReferenceSelector] = Field(
        default_factory=list,
        max_length=MAX_CHANGE,
        description="Products on screen to put in - a piece's options included.",
    )


def change_room_tool(schemas: ToolSchemas) -> ZoryTool[ChangeRoomArgs]:
    async def change(args: ChangeRoomArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        if any(piece < 1 for piece in args.remove):
            return ToolOutcome(status=ToolStatus.REFUSED, code="piece_not_found")
        products: list[int] = []
        for which in args.add:
            product = await resolve_product(services, which, work, context)
            if isinstance(product, ToolOutcome):
                return product
            products.append(product)
        outcome = await services.rooms.change(
            work.staged,
            context.retailer,
            remove_pieces=tuple(args.remove),
            add_products=tuple(products),
            allow_over_budget=False,
        )
        told = room_outcome(outcome)
        room = work.staged.room_project
        if outcome.code != "over_budget" or room is None or not products:
            return told
        bundle = outcome.bundle
        offer = SwapBudgetOfferState(
            added_piece=_added_piece(
                next(
                    (
                        line.product
                        for line in (bundle.lines if bundle is not None else ())
                        if line.product.product_id == products[0]
                    ),
                    None,
                )
            ),
            chosen_product_id=products[0],
            bundle_revision=room.bundle_revision,
            room_total=bundle.new_spend_total if bundle is not None else None,
            room_currency=bundle.currency if bundle is not None else None,
            removed_pieces=tuple(dict.fromkeys(args.remove)),
            added_product_ids=tuple(dict.fromkeys(products)),
        )
        return replace(
            told,
            facts={**(told.facts or {}), "needs_their_yes": True},
            state=work.staged.model_copy(update={"swap_budget_offer": offer}),
        )

    return ZoryTool(
        name="change_room",
        description=(
            "One change to the room: take pieces out and put products in together - "
            '"these two instead of that pair", a piece\'s options, several finishing '
            "touches at once. Every other piece stays as it is and the room is costed "
            "once. Over budget nothing changes: tell them the one new total and ask; "
            "their yes is accept_over_budget, which applies the whole change."
        ),
        args=ChangeRoomArgs,
        owners=DESIGNERS,
        handler=change,
    )


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
        measure_fit_tool(schemas),
        design_notes_tool(schemas),
        design_room_tool(schemas),
        revise_piece_tool(schemas),
        piece_options_tool(schemas),
        swap_piece_tool(schemas),
        accept_over_budget_tool(schemas),
        decline_over_budget_tool(schemas),
        remove_piece_tool(schemas),
        piece_status_tool(schemas),
        room_additions_tool(schemas),
        add_to_room_tool(schemas),
        change_room_tool(schemas),
        show_room_tool(schemas),
    ]
