"""Nora's room design: the rules every design keeps, and the tool she writes it with."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from app.agents.work import TurnWork
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.catalog_overview import CatalogOverview, SeatingSpread, SubcategoryShelf
from app.services.room_design import PieceSpec, RoomDesignChecker, RoomDesignProblem
from app.taxonomy.seating import load_seating_semantics

from tests.unit.agents.fakes import (
    ATTRIBUTES,
    CONTEXT,
    TAXONOMY,
    agent_services,
    real_loop,
    tool_context,
)

SEATING = load_seating_semantics(taxonomy=TAXONOMY)
CHECKER = RoomDesignChecker(TAXONOMY, ATTRIBUTES, SEATING)
LOOP = real_loop()


def _shelf(category: str, kind: str, **extra: Any) -> SubcategoryShelf:
    return SubcategoryShelf(
        commerce_category=category,
        commerce_subcategory=kind,
        active_count=6,
        price_minimum=Decimal("500"),
        price_maximum=Decimal("5000"),
        **extra,
    )


OVERVIEW = CatalogOverview(
    store_id=CONTEXT.store_id,
    shelves=(
        _shelf(
            "seating",
            "sofa",
            seating=SeatingSpread(known_count=4, minimum=2, maximum=4),
            colours=("Beige", "Grey"),
            styles=("Modern",),
        ),
        _shelf("seating", "chair", implied_seats=1, colours=("Olive",)),
        _shelf("seating", "lounge-chair", implied_seats=1, colours=("Beige",)),
        _shelf("seating", "chaise-lounge"),
        _shelf("tables", "center-table", colours=("Walnut",)),
    ),
)


def _check(*pieces: PieceSpec, head_count: int | None = None) -> Any:
    return CHECKER.check(pieces, OVERVIEW, head_count=head_count, walls=("TV wall",))


def test_a_design_becomes_one_need_per_piece_in_her_order() -> None:
    checked = _check(
        PieceSpec(role="main sofa", types=("sofa",), seats_each=3, share_percent=50),
        PieceSpec(role="reading chair", types=("lounge-chair", "chair"), quantity=2, must=False),
    )

    sofa, chairs = checked.needs
    assert (sofa.commerce_category, sofa.commerce_subcategory) == ("seating", "sofa")
    assert sofa.seating_capacity.min_capacity == sofa.seating_capacity.max_capacity == 3
    assert sofa.design.share_percent == 50
    assert chairs.commerce_subcategory == "lounge-chair"
    assert chairs.design.alternatives == ("chair",) and chairs.quantity == 2
    assert chairs.priority == "optional"
    assert checked.facts["seats_planned"] == 5


def test_a_type_she_files_wrongly_is_corrected_by_the_registry() -> None:
    checked = _check(PieceSpec(role="bedside", types=("nightstand",)))

    # Nightstands are filed under tables; the store here stocks none.
    assert checked.facts["not_stocked"] == ["bedside"] and checked.needs == ()


def test_an_unapproved_type_is_refused_with_what_was_sent() -> None:
    problem = _check(PieceSpec(role="sofa", types=("couch",)))

    assert isinstance(problem, RoomDesignProblem)
    assert problem.code == "type_not_approved" and problem.detail == {"type": "couch"}


def test_one_piece_is_one_family() -> None:
    problem = _check(PieceSpec(role="side", types=("chair", "center-table")))

    assert isinstance(problem, RoomDesignProblem) and problem.code == "types_span_families"


def test_unstocked_types_and_looks_are_left_out_never_matched_near() -> None:
    checked = _check(
        PieceSpec(
            role="reading chair",
            types=("recliner", "lounge-chair"),
            colours=("Beige", "Ruby"),
        )
    )

    (need,) = checked.needs
    assert need.commerce_subcategory == "lounge-chair" and need.design.alternatives == ()
    assert need.design.colours == ("Beige",)
    assert checked.facts["types_not_stocked"] == [{"piece": 1, "types": ["recliner"]}]
    assert "reading chair" in checked.facts["looks_left_out"]["pieces"]


def test_a_seat_count_goes_only_on_a_type_that_seats_several() -> None:
    for kinds in (("chair",), ("chaise-lounge",), ("sofa", "lounge-chair")):
        problem = _check(PieceSpec(role="seat", types=kinds, seats_each=2))
        assert isinstance(problem, RoomDesignProblem), kinds
        assert problem.code == "seats_each_only_on_multi_seat_types"


def test_a_chaise_lounge_is_a_piece_but_never_a_seat() -> None:
    checked = _check(
        PieceSpec(role="sofa", types=("sofa",), seats_each=3),
        PieceSpec(role="chaise", types=("chaise-lounge",)),
        head_count=5,
    )

    assert len(checked.needs) == 2
    assert checked.facts["seats_planned"] == 3 and checked.facts["seats_short"] == 2


def test_shares_never_add_up_to_more_than_the_budget() -> None:
    problem = _check(
        PieceSpec(role="sofa", types=("sofa",), share_percent=70),
        PieceSpec(role="table", types=("center-table",), share_percent=40),
    )

    assert isinstance(problem, RoomDesignProblem) and problem.code == "shares_over_whole_budget"


def test_words_state_no_figure_and_unknown_walls_are_dropped() -> None:
    figured = _check(PieceSpec(role="sofa", types=("sofa",), feel="about 2 metres"))
    walled = _check(PieceSpec(role="sofa", types=("sofa",), wall="garden wall"))

    assert isinstance(figured, RoomDesignProblem) and figured.code == "words_state_a_figure"
    assert walled.needs[0].design.wall is None
    assert walled.facts["walls_not_on_record"] == ["sofa"]


# ── the tool ────────────────────────────────────────────────────────────────


class _Shelves:
    async def overview(self, context: Any) -> CatalogOverview:
        return OVERVIEW


class _Rooms:
    def __init__(self) -> None:
        self.inputs: list[Any] = []

    async def design(self, state: Any, context: Any, inputs: Any) -> Any:
        from app.agents.operations.rooms import RoomOutcome

        self.inputs.append(inputs)
        return RoomOutcome(state=state, bundle=None, code="ok", committed=True, facts={})


async def _design(args: dict[str, Any], persona: AgentPersona = AgentPersona.NORA) -> Any:
    rooms = _Rooms()
    services = agent_services(capabilities=_Shelves(), seating=SEATING)  # type: ignore[arg-type]
    services = replace(services, rooms=rooms)
    tool = LOOP._registry.get("design_room")
    assert tool is not None and tool.handler is not None
    work = TurnWork.starting(AgentStateV1())
    work.working = persona
    outcome = await tool.handler(tool.args.model_validate(args), tool_context(services), work)
    return outcome, rooms


ARGS = {
    "room_type": "living room",
    "room_kind": "living_room",
    "same_room": False,
    "budget": {"amount": 15000, "currency": "SAR"},
    "head_count": 5,
    "palette": ["Beige", "Olive"],
    "pieces": [
        {"role": "main sofa", "types": ["sofa"], "seats_each": 3, "share_percent": 50},
        {"role": "chairs", "types": ["chair", "lounge-chair"], "quantity": 2},
    ],
}


async def test_design_room_builds_what_she_designed() -> None:
    outcome, rooms = await _design(ARGS)

    (inputs,) = rooms.inputs
    assert [n.design.role for n in inputs.plan.needs] == ["main sofa", "chairs"]
    assert inputs.palette == ("Beige", "Olive") and inputs.head_count == 5
    assert outcome.facts["design"]["seats_planned"] == 5


async def test_a_broken_design_is_handed_back_with_what_is_right() -> None:
    broken = {**ARGS, "pieces": [{"role": "seat", "types": ["chair"], "seats_each": 2}]}

    outcome, rooms = await _design(broken)

    assert outcome.status == "invalid" and rooms.inputs == []
    assert outcome.code == "seats_each_only_on_multi_seat_types"
    assert "chair" in outcome.facts["one_seat_types"]


def test_only_nora_designs_a_room() -> None:
    tool = LOOP._registry.get("design_room")
    assert tool is not None and tool.owners == frozenset({AgentPersona.NORA})
    assert LOOP._registry.get("plan_room") is None
    assert LOOP._registry.get("revise_room") is None


def test_a_room_just_designed_is_drawn_only_when_nora_shows_it() -> None:
    from app.agents.payloads import RoomShown
    from app.agents.screen import presentation
    from app.agents.work import ToolRecord, ToolStatus
    from app.schemas.bundle_presentation import GroundedBundlePresentation
    from app.schemas.language import ReplyLanguage

    room = GroundedBundlePresentation.model_construct(items=())
    work = TurnWork.starting(AgentStateV1())
    work.record(
        ToolRecord(
            agent=AgentPersona.NORA,
            tool="design_room",
            status=ToolStatus.OK,
            payload=RoomShown.model_construct(room=room),
        )
    )

    assert presentation(work, work.staged, (), ReplyLanguage.EN) is None
    shown = presentation(work, work.staged, (), ReplyLanguage.EN, "room")
    assert shown is not None and shown.room is room


async def test_a_declined_room_size_is_kept_and_not_asked_again() -> None:
    services = agent_services()
    tool = LOOP._registry.get("note_room")
    assert tool is not None and tool.handler is not None

    noted = await tool.handler(
        tool.args.model_validate({"room_kind": "living_room", "declined_size": True}),
        tool_context(services),
        TurnWork.starting(AgentStateV1()),
    )

    assert noted.state.room_project.size_declined is True
    assert noted.facts["size_declined"] is True


async def test_finishing_a_room_leaves_the_next_room_to_them() -> None:
    from app.schemas.agent_state import RoomProjectState

    state = AgentStateV1(room_project=RoomProjectState(room_type="living room", in_cart=True))
    tool = LOOP._registry.get("finish_room")
    assert tool is not None and tool.handler is not None

    done = await tool.handler(
        tool.args.model_validate({}), tool_context(agent_services()), TurnWork.starting(state)
    )

    # No list of next rooms from the registry: the agent asks in its own words.
    assert done.facts == {"finished": "living room", "rooms_finished": ["living room"]}
    assert done.state.room_project is None


def test_a_size_range_out_of_order_is_handed_back() -> None:
    problem = _check(PieceSpec(role="sofa", types=("sofa",), longer_min_cm=240, longer_max_cm=200))

    assert isinstance(problem, RoomDesignProblem) and problem.code == "size_range_out_of_order"


def test_a_wall_they_named_with_a_number_can_be_used() -> None:
    checked = CHECKER.check(
        (PieceSpec(role="sofa", types=("sofa",), wall="wall 2"),),
        OVERVIEW,
        head_count=None,
        walls=("wall 2",),
    )

    assert checked.needs[0].design.wall == "wall 2"


def test_what_could_still_finish_a_room_within_its_budget() -> None:
    from app.agents.tools.design import _could_add
    from app.schemas.agent_state import RoomDesignNeedState, RoomProjectState
    from app.schemas.design import DesignPriority, PieceDesign
    from app.taxonomy.complements import load_complements
    from app.taxonomy.rooms import load_room_pieces

    services = agent_services(
        seating=SEATING,
        room_pieces=load_room_pieces(taxonomy=TAXONOMY, seating=SEATING),
        complements=load_complements(taxonomy=TAXONOMY),
    )
    room = RoomProjectState(
        room_type="living room",
        room_kind="living_room",
        palette=("Walnut",),
        next_design_need_id=2,
        design_needs=(
            RoomDesignNeedState(
                need_id=1,
                commerce_category="seating",
                commerce_subcategory="sofa",
                priority=DesignPriority.REQUIRED,
                quantity=1,
                design=PieceDesign(role="main sofa", alternatives=("lounge-chair",)),
            ),
        ),
    )

    fitting = _could_add(services, room, OVERVIEW, Decimal(400), only_fitting=True)
    every = {e["kind"]: e for e in _could_add(services, room, OVERVIEW, Decimal(400))}

    # What the room holds stays on offer, with how many - a second of a kind
    # may be what finishes it - after the kinds it holds none of.
    assert every["sofa"]["in_room"] == 1 and every["lounge-chair"]["in_room"] == 1
    assert "in_room" not in every["center-table"]
    kinds = list(every)
    assert kinds.index("center-table") < kinds.index("sofa")
    # Nothing here costs as little as 400.
    assert fitting == []
    table = every["center-table"]
    assert table["fits_budget_left"] is False and table["in_room_colours"] is True
    assert table["in_this_room"] is True and table["usually_paired"] is True


async def test_a_rooms_budget_is_kept_the_moment_it_is_said() -> None:
    tool = LOOP._registry.get("note_room")
    assert tool is not None and tool.handler is not None

    noted = await tool.handler(
        tool.args.model_validate(
            {"room_kind": "living_room", "budget": {"amount": 15000, "currency": "SAR"}}
        ),
        tool_context(agent_services()),
        TurnWork.starting(AgentStateV1()),
    )

    room = noted.state.room_project
    assert room.budget is not None and room.budget.max_amount == Decimal(15000)
    assert noted.facts["budget"] == Decimal(15000)


async def test_the_rooms_purpose_is_kept_the_moment_it_is_said() -> None:
    tool = LOOP._registry.get("note_room")
    assert tool is not None and tool.handler is not None

    noted = await tool.handler(
        tool.args.model_validate({"room_kind": "living_room", "purpose": "hosting guests"}),
        tool_context(agent_services()),
        TurnWork.starting(AgentStateV1()),
    )

    assert noted.state.room_project.purpose == "hosting guests"
    assert noted.facts["purpose"] == "hosting guests"


async def test_a_layout_is_words_and_never_a_figure() -> None:
    _, rooms = await _design({**ARGS, "layout": "an L in the corner by the window"})
    figured, unbuilt = await _design({**ARGS, "layout": "sofa 3 m from the window"})

    assert rooms.inputs[0].layout_idea == "an L in the corner by the window"
    assert figured.code == "why_states_a_figure" and unbuilt.inputs == []
