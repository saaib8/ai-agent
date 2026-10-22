"""The team graph: the supervisor delegates, agents act, hand back and reply."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.agents.context import ToolContext
from app.agents.loop import AgentLoop
from app.agents.screen_actions import Tap, TapEvent
from app.agents.services import AgentServices
from app.agents.team import AgentTeam, TeamTurn
from app.agents.tools.base import ToolArgs, ToolOutcome, ZoryTool
from app.agents.tools.common import HAND_BACK, RESPOND
from app.agents.tools.registry import ToolRegistry
from app.agents.trace import TurnTrace
from app.agents.work import TurnWork
from app.agents.writer import ReplyWriter
from app.core.config import AgentsSettings
from app.core.exceptions import AgentTurnIncompleteError
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import CustomerResponse
from app.schemas.conversation import ConversationContext, ConversationMessage, ConversationRole
from app.schemas.language import ReplyLanguage
from app.schemas.retailer import RetailerContext

from tests.unit.agents.fake_chat_model import ScriptedChatModel, call, step

SARA, NORA, SALMAN, FAYSAL = (
    AgentPersona.SARA,
    AgentPersona.NORA,
    AgentPersona.SALMAN,
    AgentPersona.FAYSAL,
)


class _Hydration:
    async def hydrate_ids(self, ids: Any, context: Any) -> tuple[()]:
        return ()


class _Capabilities:
    async def overview(self, context: Any) -> Any:
        return SimpleNamespace(currency="SAR")


SERVICES = cast(
    AgentServices,
    SimpleNamespace(hydration=_Hydration(), rooms=None, capabilities=_Capabilities()),
)


class NoteArgs(ToolArgs):
    pass


async def _noted(args: NoteArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
    return ToolOutcome(facts={"noted": True})


NOTE = ZoryTool(
    name="note", description="note", args=NoteArgs, owners=frozenset(AgentPersona), handler=_noted
)


def _team(
    supervisor: ScriptedChatModel,
    agents: dict[AgentPersona, ScriptedChatModel],
    **settings: Any,
) -> tuple[AgentTeam, AgentLoop]:
    models = {a: agents.get(a, ScriptedChatModel(script=[])) for a in AgentPersona}
    loop = AgentLoop(ToolRegistry([NOTE, RESPOND, HAND_BACK]), max_tool_calls=3)
    team = AgentTeam(supervisor, models, AgentsSettings(**settings), loop)
    return team, loop


def _turn(
    message: str = "hi",
    tap: Tap | None = None,
    conversation: ConversationContext | None = None,
) -> TeamTurn:
    return TeamTurn(
        message=message,
        conversation=conversation or ConversationContext(),
        state=AgentStateV1(),
        context=ToolContext(retailer=RetailerContext(store_id=50), services=SERVICES),
        tap=tap,
    )


async def _run(team: AgentTeam, loop: AgentLoop, turn: TeamTurn | None = None) -> Any:
    return await team.run(turn or _turn(), TurnTrace(store_id=50, path="typed"))


def transfer(agent: AgentPersona, call_id: str = "s1") -> Any:
    return step(call(f"transfer_to_{agent.value}", call_id, need="shopping"))


def reply(text: str, call_id: str = "r1") -> Any:
    return step(call("respond", call_id, message=text))


async def test_the_supervisor_delegates_and_the_agent_replies() -> None:
    supervisor = ScriptedChatModel(script=[transfer(NORA)])
    nora = ScriptedChatModel(script=[step(call("note", "n1")), reply("A rug would ground it.")])
    team, loop = _team(supervisor, {NORA: nora})

    result = await _run(team, loop)

    assert result.owner is NORA
    assert result.reply.message == "A rug would ground it."
    assert result.handoffs == (NORA,)
    assert [r.tool for r in result.work.records] == ["note"]


async def test_a_hand_back_reaches_the_next_agent_with_what_was_done() -> None:
    supervisor = ScriptedChatModel(script=[transfer(SARA), transfer(SALMAN, "s2")])
    sara = ScriptedChatModel(
        script=[step(call("note", "a1")), step(call("hand_back", "a2", reason="needs_sales"))]
    )
    salman = ScriptedChatModel(script=[reply("This side table suits it.")])
    team, loop = _team(supervisor, {SARA: sara, SALMAN: salman})

    result = await _run(team, loop)

    assert result.owner is SALMAN
    assert result.handoffs == (SARA, SALMAN)
    told = str(salman.seen[0][-1].content)
    assert '"done_this_turn"' in told and '"tool": "note"' in told


async def test_an_agent_who_names_the_colleague_passes_the_turn_straight_on() -> None:
    supervisor = ScriptedChatModel(script=[transfer(SARA)])
    sara = ScriptedChatModel(
        script=[
            step(call("note", "a1")),
            step(call("hand_back", "a2", reason="needs_design", to="nora")),
        ]
    )
    nora = ScriptedChatModel(script=[reply("It sits well on the TV wall.")])
    team, loop = _team(supervisor, {SARA: sara, NORA: nora})

    result = await _run(team, loop)

    assert result.owner is NORA and result.handoffs == (SARA, NORA)
    # No routing call between them: the supervisor chose once.
    assert len(supervisor.seen) == 1
    told = str(nora.seen[0][-1].content)
    assert '"asked_for": "design"' in told and '"tool": "note"' in told


async def test_a_named_colleague_still_counts_against_the_handoff_limit() -> None:
    supervisor = ScriptedChatModel(script=[transfer(SARA)])
    sara = ScriptedChatModel(
        script=[
            step(call("hand_back", "a1", reason="needs_design", to="nora")),
            reply("Here is what I found."),
        ]
    )
    team, loop = _team(supervisor, {SARA: sara}, max_hops=1)

    result = await _run(team, loop)

    assert result.owner is SARA and result.handoffs == (SARA, SARA)


async def test_a_tap_is_routed_by_the_supervisor_with_who_offered_it() -> None:
    supervisor = ScriptedChatModel(script=[transfer(SALMAN)])
    salman = ScriptedChatModel(script=[reply("Added the room to your cart.")])
    team, loop = _team(supervisor, {SALMAN: salman})
    conversation = ConversationContext(
        messages=(
            ConversationMessage(role=ConversationRole.USER, content="design my room"),
            ConversationMessage(role=ConversationRole.ASSISTANT, content="Here it is.", agent=NORA),
        )
    )
    tap = Tap(TapEvent(button="add_room_to_cart"))

    result = await _run(team, loop, _turn("Add all to cart", tap=tap, conversation=conversation))

    assert result.owner is SALMAN
    routed = str(supervisor.seen[0][-1].content)
    assert '"button_offered_by": "nora"' in routed and "add_room_to_cart" in routed
    told = str(salman.seen[0][-1].content)
    assert "[tapped a button]" in told and 'The button read: "Add all to cart"' in told


async def test_a_supervisor_that_chooses_nothing_is_asked_once_more() -> None:
    supervisor = ScriptedChatModel(script=[step(text="hmm"), transfer(NORA, "s2")])
    nora = ScriptedChatModel(script=[reply("Tell me about the room.")])
    team, loop = _team(supervisor, {NORA: nora})

    result = await _run(team, loop)

    assert result.owner is NORA
    assert len(supervisor.seen) == 2


async def test_finishing_names_who_replies() -> None:
    supervisor = ScriptedChatModel(
        script=[transfer(SARA), step(call("finish", "s2", reply_by="sara"))]
    )
    sara = ScriptedChatModel(
        script=[step(call("hand_back", "a1", reason="done")), reply("Here they are.")]
    )
    team, loop = _team(supervisor, {SARA: sara})

    result = await _run(team, loop)

    assert result.owner is SARA
    assert sara.offered[-1] == ["respond"]
    assert '"reason": "done"' in str(supervisor.seen[1][-1].content)


async def test_the_supervisor_knows_each_agents_tools() -> None:
    supervisor = ScriptedChatModel(script=[transfer(SARA)])
    sara = ScriptedChatModel(script=[reply("Hello, I'm Sara.")])
    team, loop = _team(supervisor, {SARA: sara})

    await _run(team, loop)

    assert '"agents_tools"' in str(supervisor.seen[0][-1].content)


async def test_the_handoff_limit_makes_the_last_agent_reply() -> None:
    supervisor = ScriptedChatModel(script=[transfer(SARA)])
    sara = ScriptedChatModel(
        script=[
            step(call("hand_back", "a1", reason="needs_design")),
            reply("Here is what I found."),
        ]
    )
    team, loop = _team(supervisor, {SARA: sara}, max_hops=1)

    result = await _run(team, loop)

    assert result.owner is SARA
    assert sara.offered[-1] == ["respond"]


async def test_the_model_call_budget_is_shared_by_the_whole_team() -> None:
    supervisor = ScriptedChatModel(script=[transfer(SARA)])
    sara = ScriptedChatModel(script=[step(call("note", f"n{i}")) for i in range(5)])
    team, loop = _team(supervisor, {SARA: sara}, max_model_calls=3)

    with pytest.raises(AgentTurnIncompleteError):
        await _run(team, loop)


async def test_a_rewrite_offers_respond_alone() -> None:
    supervisor = ScriptedChatModel(script=[transfer(SARA)])
    sara = ScriptedChatModel(script=[reply("It costs 999."), reply("Here are three sofas.")])
    team, loop = _team(supervisor, {SARA: sara})
    turn = _turn()
    first = await _run(team, loop, turn)

    corrected = await team.rewrite(
        turn, first, "the figure 999 came from no tool", TurnTrace(store_id=50, path="typed")
    )

    assert corrected.message == "Here are three sofas."
    assert sara.offered[-1] == ["respond"]


async def _add_room(args: NoteArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
    return ToolOutcome(facts={"added": True})


ADD_ROOM = ZoryTool(
    name="add_room_to_cart",
    description="add the room",
    args=NoteArgs,
    owners=frozenset({SALMAN}),
    handler=_add_room,
)


def test_a_tap_with_no_words_is_recorded_without_figures() -> None:
    tap = Tap(TapEvent(button="swap_room_piece", room_piece=2, option=3))

    assert tap.recorded == "[tapped: swap room piece]"


async def _word(team: AgentTeam, facts: dict[str, Any]) -> Any:
    return await team.word(
        SARA,
        "[The customer picked the sofa in their photo]",
        facts,
        AgentStateV1(),
        ConversationContext(),
        ToolContext(retailer=RetailerContext(store_id=50), services=SERVICES),
        TurnTrace(store_id=50, path="tap"),
    )


async def test_an_agent_words_what_happened_from_its_facts() -> None:
    sara = ScriptedChatModel(script=[reply("Here are 3 sofas like the one in your photo.")])
    team, _ = _team(ScriptedChatModel(script=[]), {SARA: sara})

    worded = await _word(team, {"matches": 3})

    assert worded is not None and worded.message.startswith("Here are 3 sofas")
    assert sara.offered[-1] == ["respond"]


async def test_a_worded_reply_with_figures_from_nowhere_twice_is_none() -> None:
    sara = ScriptedChatModel(script=[reply("It costs 999."), reply("Still 999.")])
    team, _ = _team(ScriptedChatModel(script=[]), {SARA: sara})

    assert await _word(team, {"matches": 3}) is None


async def test_the_writer_falls_back_to_its_fixed_words() -> None:
    sara = ScriptedChatModel(script=[reply("It costs 999."), reply("Still 999.")])
    team, _ = _team(ScriptedChatModel(script=[]), {SARA: sara})
    writer = ReplyWriter(team, SERVICES, deadline_s=5)
    fixed = CustomerResponse(message="Here are the closest matches.")

    written = await writer.write(
        happened="[photo]",
        facts={},
        state=AgentStateV1(),
        conversation=ConversationContext(),
        retailer=RetailerContext(store_id=50),
        language=ReplyLanguage.EN,
        fixed=fixed,
    )

    assert written is fixed


async def test_a_stopped_turn_may_correct_its_one_broken_reply() -> None:
    supervisor = ScriptedChatModel(script=[transfer(SARA)])
    chips = [{"label": "Yes", "value": "yes"}]
    sara = ScriptedChatModel(
        script=[
            step(call("note", "n1")),
            step(),
            step(call("respond", "r1", message="I noted it.", choices=chips)),
            reply("I noted it, but could not finish the rest."),
        ]
    )
    team, loop = _team(supervisor, {SARA: sara})

    result = await _run(team, loop)

    assert result.reply.message.startswith("I noted it, but")
    assert result.work.records[0].tool == "note"
    told = str(sara.seen[2][-1].content)
    assert "ran out of steps" in told and "empty step" not in told
