"""What follows an add: the rules on the session, the routing, the follow-up a
pick returns and the chips code draws beside a grid of kinds (CLAUDE.md 10.3)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.agents.after_add import AddContext, AfterAdd, decide, goal_for, progress_of
from app.agents.screen_actions import Tap, TapEvent
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import (
    AgentStateV1,
    CartQuantity,
    GoalKind,
    GoalState,
    ProductInteractionState,
)
from app.schemas.language import ReplyLanguage
from app.taxonomy.seating import load_seating_semantics

from tests.unit.agents.fake_chat_model import ScriptedChatModel, call, step
from tests.unit.agents.fakes import TAXONOMY, _product
from tests.unit.agents.test_team import NORA, SALMAN, SARA, _run, _team, _turn, reply

SEATING = load_seating_semantics(taxonomy=TAXONOMY)


def _with(goals: tuple[GoalState, ...], cart: tuple[int, ...], **quantities: int) -> AgentStateV1:
    return AgentStateV1(
        goals=goals,
        product_interaction=ProductInteractionState(
            selected_product_ids=cart,
            cart_quantities=tuple(
                CartQuantity(product_id=int(pid[1:]), quantity=q) for pid, q in quantities.items()
            ),
            last_added_product_id=cart[-1] if cart else None,
        ),
    )


def _decide(state: AgentStateV1) -> AfterAdd:
    by_id = {
        pid: _product(pid, subcategory="bed")
        for pid in state.product_interaction.selected_product_ids
    }
    return decide(AddContext(state, None, progress_of(state, by_id))).path


BEDS = GoalState(goal=1, kind=GoalKind.PIECE, summary="two beds", for_kind="bed", count=2)


def test_a_goal_of_two_beds_waits_for_the_second_bed() -> None:
    one = _with((BEDS.model_copy(update={"members": (1,)}),), (1,))
    both = _with((BEDS.model_copy(update={"members": (1, 2)}),), (1, 2))
    twice = _with((BEDS.model_copy(update={"members": (1,)}),), (1,), p1=2)

    assert _decide(one) is AfterAdd.CONTINUE_GOAL
    assert _decide(both) is AfterAdd.PLAN_THEN_SHOW
    assert _decide(twice) is AfterAdd.PLAN_THEN_SHOW, "the same bed twice meets it"


def test_no_goal_is_the_usual_plan_then_show() -> None:
    decision = decide(AddContext(_with((), (1,)), None, ()))

    assert decision.path is AfterAdd.PLAN_THEN_SHOW and decision.first is AgentPersona.NORA


def test_an_added_piece_joins_the_goal_it_counts_toward() -> None:
    sofa_goal = GoalState(goal=2, kind=GoalKind.SEATING, summary="seat ten", seats=10)
    state = _with((BEDS, sofa_goal), (7,))

    assert goal_for(state, _product(7, subcategory="bed"), SEATING) == 1
    assert goal_for(state, _product(7, subcategory="sofa"), SEATING) == 2
    assert goal_for(state, _product(7, subcategory="carpet"), SEATING) is None


async def test_a_tapped_add_with_no_goal_runs_nora_then_salman_without_routing() -> None:
    supervisor = ScriptedChatModel(script=[])
    nora = ScriptedChatModel(
        script=[step(call("cross_sell_advice", "n1")), step(call("hand_back", "n2", reason="done"))]
    )
    salman = ScriptedChatModel(
        script=[step(call("what_goes_with", "s1")), reply("These go nicely with it.")]
    )
    team, loop = _team(supervisor, {NORA: nora, SALMAN: salman})
    tap = Tap(TapEvent(button="added_to_cart", pick=1))

    result = await _run(team, loop, _turn("I've added the Sofa to my cart", tap=tap))

    assert result.handoffs == (NORA, SALMAN), "Salman follows whoever Nora names"
    assert supervisor.seen == []


async def test_an_add_toward_an_unmet_goal_stays_with_sara() -> None:
    supervisor = ScriptedChatModel(script=[])
    sara = ScriptedChatModel(script=[reply("Here is the second bed.")])
    team, loop = _team(supervisor, {SARA: sara})
    tap = Tap(TapEvent(button="added_to_cart", pick=1))
    turn = replace(_turn("I've added the Bed to my cart", tap=tap), state=_with((BEDS,), (1,)))

    result = await _run(team, loop, turn)

    assert result.owner is SARA and result.handoffs == (SARA,)
    assert supervisor.seen == []


def test_a_pick_returns_its_turn_ready_to_send() -> None:
    from decimal import Decimal

    from app.schemas.picks import PickView
    from app.services.picks import _follow_up

    pick = PickView(
        pick=3,
        name_english="Oak Bed",
        image_url="https://example.test/b.jpg",
        price_amount=Decimal("100"),
        price_unit="SAR",
    )

    follow = _follow_up(3, (pick,), ReplyLanguage.EN)

    assert follow is not None
    assert follow.message == "I've added the Oak Bed to my cart"
    assert follow.product_action.kind == "added_to_cart" and follow.product_action.pick == 3
    assert _follow_up(None, (pick,), ReplyLanguage.EN) is None


async def test_beside_kind_cards_only_codes_chips_are_drawn() -> None:
    from app.agents.payloads import GridChip, SearchShown
    from app.agents.screen import presentation
    from app.agents.work import ToolRecord, ToolStatus, TurnWork
    from app.schemas.text_choice import TextReplyChoice

    from tests.unit.agents.fakes import FakePipeline

    run = await FakePipeline(ids=(11, 12)).execute(None, None)
    work = TurnWork.starting(AgentStateV1())
    work.record(
        ToolRecord(
            agent=AgentPersona.SALMAN,
            tool="what_goes_with",
            status=ToolStatus.OK,
            payload=SearchShown(
                grounding=run.grounding,
                more_options=False,
                kind_cards=True,
                grid_chips=(
                    GridChip(offers="another_of_its_kind", name="sofa", name_ar="كنبة"),
                    GridChip(offers="whole_room", name="sofa", name_ar="كنبة"),
                    GridChip(offers="whole_room", name="bed", name_ar="سرير", room="bedroom"),
                ),
            ),
        )
    )
    written = (TextReplyChoice(label="Comforters", value="Show me comforters"),)

    english = presentation(work, AgentStateV1(), written, ReplyLanguage.EN)
    arabic = presentation(work, AgentStateV1(), written, ReplyLanguage.AR)

    assert english is not None and arabic is not None
    assert [c.label for c in english.choices] == [
        "Another sofa",
        "Design a room around it",
        "Design the bedroom",
    ], "the room is named only when a fact says it"
    assert [c.value for c in english.choices] == [
        "Show me another sofa",
        "Design a room around my sofa",
        "Design the bedroom around my bed",
    ]
    assert [c.label for c in arabic.choices][:2] == ["المزيد من كنبة", "صمّم غرفة حولها"]


# ── which room a cart product is for (CLAUDE.md 36) ─────────────────────────

BED, NIGHTSTAND, RUG, SOFA = 1, 2, 3, 4
KINDS = {BED: "bed", NIGHTSTAND: "nightstand", RUG: "carpet", SOFA: "sofa"}
STARTERS = frozenset({"bed", "sofa"})


def _cart(*pids: int) -> AgentStateV1:
    return AgentStateV1(product_interaction=ProductInteractionState(selected_product_ids=pids))


def test_a_piece_added_beside_another_goes_where_it_goes() -> None:
    from app.services.agent_state import link_pick, pick_group, room_of_pick, tag_pick_rooms

    state = link_pick(link_pick(_cart(BED, NIGHTSTAND, RUG), NIGHTSTAND, BED), RUG, NIGHTSTAND)

    assert pick_group(state, RUG) == BED, "a chain follows back to the first piece"
    assert room_of_pick(state, RUG) is None, "no room is known yet"
    tagged = tag_pick_rooms(state, (BED,), "Bedroom")
    assert room_of_pick(tagged, RUG) == "bedroom" and room_of_pick(tagged, NIGHTSTAND) == "bedroom"


ONE_ROOM = {"bed": "bedroom", "nightstand": "bedroom"}


def _design(state: AgentStateV1, room: str, named: frozenset[int] = frozenset()) -> Any:
    from app.services.room_cart import picks_for_design

    return picks_for_design(state, room, None, KINDS, STARTERS, named, ONE_ROOM.get)


def test_a_sofa_after_a_bed_asks_about_the_sofa_not_the_bed() -> None:
    from app.services.agent_state import link_pick

    state = link_pick(link_pick(_cart(BED, NIGHTSTAND, RUG, SOFA), NIGHTSTAND, BED), RUG, BED)

    living = _design(state, "living room", frozenset({SOFA}))

    assert living.ask == (SOFA,), "the bed is obviously the bedroom's; the sofa is the question"
    assert not living.takes & {BED, NIGHTSTAND, RUG}, "nothing of the bedroom joins unasked"
    assert living.rooms[RUG] == "bedroom", "a rug chosen beside the bed goes where the bed goes"


def test_once_the_sofa_is_placed_nothing_more_is_asked() -> None:
    from app.services.agent_state import link_pick, tag_pick_rooms

    state = link_pick(_cart(BED, NIGHTSTAND, SOFA), NIGHTSTAND, BED)
    in_living = tag_pick_rooms(state, (SOFA,), "living room")
    in_bedroom = tag_pick_rooms(state, (SOFA,), "bedroom")

    assert _design(in_living, "living room", frozenset({SOFA})).takes == {SOFA}
    bedroom = _design(in_bedroom, "bedroom", frozenset({SOFA}))
    assert bedroom.takes == {BED, NIGHTSTAND, SOFA} and bedroom.ask == ()


def test_one_room_alone_in_the_cart_asks_nothing() -> None:
    lone_sofa = _design(_cart(SOFA, RUG), "living room")
    bedroom = _design(_cart(BED, NIGHTSTAND), "bedroom", frozenset({BED}))

    assert lone_sofa.takes == {SOFA, RUG} and lone_sofa.ask == ()
    assert bedroom.takes == {BED, NIGHTSTAND} and bedroom.ask == ()


def test_a_room_is_recorded_only_when_the_customer_named_it() -> None:
    from app.agents.tools.pick_rooms import _said_by_them

    assert _said_by_them("Living room", ("It's for the living room",))
    assert _said_by_them("living_room", ("the LIVING   room please",))
    assert not _said_by_them("living room", ("budget 12000, 4 people, just design it",))


def test_the_brief_names_the_piece_whose_room_is_in_doubt() -> None:
    from app.agents.views import _rooms_in_doubt
    from app.services.agent_state import link_pick
    from app.taxonomy.rooms import load_room_pieces

    guide = load_room_pieces(taxonomy=TAXONOMY, seating=SEATING)
    by_id = {
        BED: _product(BED, subcategory="bed"),
        NIGHTSTAND: _product(NIGHTSTAND, subcategory="nightstand"),
        SOFA: _product(SOFA, subcategory="sofa"),
    }
    state = link_pick(_cart(BED, NIGHTSTAND, SOFA), NIGHTSTAND, BED)

    doubt = _rooms_in_doubt(state, by_id, guide, arabic=False)
    alone = _rooms_in_doubt(_cart(SOFA), by_id, guide, arabic=False)

    assert doubt is not None and [d.pick for d in doubt.ask_about] == [3], "only the sofa"
    assert doubt.known == {"bedroom": ("bed", "nightstand")}
    assert alone is None, "a sofa alone leaves nothing in doubt"


def test_a_mattress_chosen_beside_a_bed_offers_the_bedroom() -> None:
    from types import SimpleNamespace
    from typing import cast

    from app.agents.services import AgentServices
    from app.agents.tools.sales import _grid_chips
    from app.taxonomy.rooms import load_room_pieces

    services = cast(
        AgentServices,
        SimpleNamespace(
            taxonomy=TAXONOMY,
            seating=SEATING,
            room_pieces=load_room_pieces(taxonomy=TAXONOMY, seating=SEATING),
        ),
    )
    bed, mattress = _product(BED, subcategory="bed"), _product(9, subcategory="mattresses")

    beside_bed = _grid_chips(services, cast(Any, None), mattress, bed, _cart(BED, 9))
    alone = _grid_chips(services, cast(Any, None), mattress, mattress, _cart(9))

    assert [(c.offers, c.name, c.room) for c in beside_bed] == [("whole_room", "bed", "bedroom")]
    assert alone == (), "a mattress alone starts no room"
