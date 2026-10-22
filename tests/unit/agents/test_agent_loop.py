"""The agent loop: allowlist, order, correction, budgets and scope."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from app.agents.context import ToolContext
from app.agents.loop import AgentLoop, HandedBack, HopEnd, Responded, TurnBudget
from app.agents.tools.base import ToolArgs, ToolOutcome, ZoryTool
from app.agents.tools.common import HAND_BACK, RESPOND, HandBackReason
from app.agents.tools.registry import ToolRegistry
from app.agents.trace import TurnTrace
from app.agents.work import ToolStatus, TurnWork
from app.core.exceptions import (
    AgentTurnIncompleteError,
    ComparisonRefusedError,
    ConfigurationError,
)
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.retailer import RetailerContext
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import Field

from tests.unit.agents.fake_chat_model import ScriptedChatModel, call, step

SARA, NORA = AgentPersona.SARA, AgentPersona.NORA
CONTEXT = ToolContext(retailer=RetailerContext(store_id=50), services=cast(Any, None))


class CountArgs(ToolArgs):
    times: int = Field(ge=1, le=3)


class Calls:
    def __init__(self) -> None:
        self.seen: list[tuple[int, int]] = []

    async def __call__(self, args: CountArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        self.seen.append((args.times, context.retailer.store_id))
        return ToolOutcome(facts={"counted": args.times})


def _loop(
    handler: Calls, *, max_tool_calls: int = 6, owners: frozenset[AgentPersona] = frozenset({SARA})
) -> AgentLoop:
    counting = ZoryTool(
        name="count", description="count", args=CountArgs, owners=owners, handler=handler
    )
    return AgentLoop(ToolRegistry([counting, RESPOND, HAND_BACK]), max_tool_calls=max_tool_calls)


async def _run(
    loop: AgentLoop,
    model: ScriptedChatModel,
    *,
    agent: AgentPersona = SARA,
    budget: int = 8,
    work: TurnWork | None = None,
) -> HopEnd:
    return await loop.run(
        agent,
        model,
        [HumanMessage("hi")],
        work or TurnWork.starting(AgentStateV1()),
        CONTEXT,
        TurnBudget(max_model_calls=budget),
        TurnTrace(store_id=50, path="typed"),
    )


async def test_a_tool_runs_and_its_result_goes_back_before_the_reply() -> None:
    handler = Calls()
    model = ScriptedChatModel(
        script=[
            step(call("count", "a", times=2)),
            step(call("respond", "b", message="Done.")),
        ]
    )
    work = TurnWork.starting(AgentStateV1())

    end = await _run(_loop(handler), model, work=work)

    assert isinstance(end, Responded) and end.reply.message == "Done."
    assert handler.seen == [(2, 50)]
    answer = model.seen[1][-1]
    assert isinstance(answer, ToolMessage)
    assert json.loads(str(answer.content)) == {
        "status": "ok",
        "facts": {"counted": 2},
        "figures": {"count.counted": "2"},
    }
    assert [r.tool for r in work.records] == ["count"]


async def test_a_tool_outside_the_agents_allowlist_is_never_offered_or_run() -> None:
    handler = Calls()
    model = ScriptedChatModel(
        script=[
            step(call("count", "a", times=1)),
            step(call("respond", "b", message="Sorry.")),
        ]
    )
    work = TurnWork.starting(AgentStateV1())

    await _run(_loop(handler), model, agent=NORA, work=work)

    assert handler.seen == []
    assert "count" not in model.offered[0]
    assert work.records[0].status is ToolStatus.REFUSED
    assert work.records[0].code == "not_permitted"


async def test_broken_arguments_get_one_correction() -> None:
    handler = Calls()
    model = ScriptedChatModel(
        script=[
            step(call("count", "a", times=9)),
            step(call("count", "b", times=1)),
            step(call("respond", "c", message="Done.")),
        ]
    )

    await _run(_loop(handler), model)

    told = json.loads(str(model.seen[1][-1].content))
    assert told["status"] == "invalid" and told["code"] == "arguments_invalid"
    assert all("9" not in rule for rule in told["facts"]["rules_broken"])
    assert handler.seen == [(1, 50)]


async def test_broken_arguments_twice_end_the_turn() -> None:
    model = ScriptedChatModel(
        script=[
            step(call("count", "a", times=9)),
            step(call("count", "b", times=0)),
        ]
    )

    with pytest.raises(AgentTurnIncompleteError):
        await _run(_loop(Calls()), model)


async def test_a_store_in_the_arguments_is_refused_not_obeyed() -> None:
    handler = Calls()
    model = ScriptedChatModel(
        script=[
            step(call("count", "a", times=1, store_id=7)),
            step(call("count", "b", times=1)),
            step(call("respond", "c", message="Done.")),
        ]
    )

    await _run(_loop(handler), model)

    assert handler.seen == [(1, 50)]


def test_a_tool_whose_arguments_could_name_a_record_stops_startup() -> None:
    class Unsafe(ToolArgs):
        product_id: int

    async def unused(args: Unsafe, context: ToolContext, work: TurnWork) -> ToolOutcome:
        return ToolOutcome()

    unsafe = ZoryTool(
        name="unsafe", description="x", args=Unsafe, owners=frozenset({SARA}), handler=unused
    )
    with pytest.raises(ConfigurationError):
        ToolRegistry([unsafe])


async def test_the_turn_budget_is_shared_and_enforced() -> None:
    model = ScriptedChatModel(script=[step(call("count", "a", times=1))] * 3)

    with pytest.raises(AgentTurnIncompleteError):
        await _run(_loop(Calls()), model, budget=2)


async def test_the_tool_budget_refuses_extra_calls() -> None:
    handler = Calls()
    model = ScriptedChatModel(
        script=[
            step(call("count", "a", times=1), call("count", "b", times=2)),
            step(call("respond", "c", message="Done.")),
        ]
    )

    await _run(_loop(handler, max_tool_calls=1), model)

    assert handler.seen == [(1, 50)]


async def test_words_without_a_call_are_the_reply() -> None:
    model = ScriptedChatModel(script=[step(text="Hello there.")])

    end = await _run(_loop(Calls()), model)

    assert isinstance(end, Responded) and end.reply.message == "Hello there."


async def test_hand_back_returns_the_turn() -> None:
    model = ScriptedChatModel(script=[step(call("hand_back", "a", reason="needs_sales"))])

    end = await _run(_loop(Calls()), model)

    assert isinstance(end, HandedBack) and end.hand_back.reason is HandBackReason.NEEDS_SALES


async def test_a_rule_refusal_is_told_to_the_model_by_its_code() -> None:
    async def refuses(args: CountArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        raise ComparisonRefusedError(reason="dissimilar")

    tool = ZoryTool(
        name="count", description="c", args=CountArgs, owners=frozenset({SARA}), handler=refuses
    )
    loop = AgentLoop(ToolRegistry([tool, RESPOND]), max_tool_calls=3)
    model = ScriptedChatModel(
        script=[
            step(call("count", "a", times=1)),
            step(call("respond", "b", message="Those can't be compared.")),
        ]
    )

    await _run(loop, model)

    told = json.loads(str(model.seen[1][-1].content))
    assert told == {"status": "refused", "code": ComparisonRefusedError.code}


async def test_unreadable_arguments_get_the_same_one_correction() -> None:
    from langchain_core.messages import AIMessage

    handler = Calls()
    unreadable = AIMessage(
        content="",
        invalid_tool_calls=[{"name": "count", "args": "{tim", "id": "a", "error": "bad json"}],
    )
    model = ScriptedChatModel(
        script=[
            unreadable,
            step(call("count", "b", times=1)),
            step(call("respond", "c", message="Done.")),
        ]
    )

    end = await _run(_loop(handler), model)

    assert isinstance(end, Responded)
    assert json.loads(str(model.seen[1][-1].content))["code"] == "arguments_unreadable"
    assert handler.seen == [(1, 50)]


async def test_a_decimal_price_reaches_the_model_as_text() -> None:
    from decimal import Decimal

    async def priced(args: CountArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        return ToolOutcome(facts={"price": Decimal("1499.00")})

    tool = ZoryTool(
        name="count", description="c", args=CountArgs, owners=frozenset({SARA}), handler=priced
    )
    loop = AgentLoop(ToolRegistry([tool, RESPOND]), max_tool_calls=3)
    model = ScriptedChatModel(
        script=[
            step(call("count", "a", times=1)),
            step(call("respond", "b", message="Done.")),
        ]
    )
    work = TurnWork.starting(AgentStateV1())

    await _run(loop, model, work=work)

    assert json.loads(str(model.seen[1][-1].content))["facts"] == {"price": "1499.00"}
    assert work.records[0].facts == {"price": "1499.00"}


@pytest.mark.parametrize("unsafe", ["free_dict", "camel", "alias"])
def test_other_ways_to_name_a_record_stop_startup(unsafe: str) -> None:
    from pydantic import AliasChoices

    class FreeDict(ToolArgs):
        filters: dict[str, int]

    class Camel(ToolArgs):
        productId: int  # noqa: N815

    class Aliased(ToolArgs):
        ref: int = Field(validation_alias=AliasChoices("ref", "store_id"))

    choices: dict[str, type[ToolArgs]] = {"free_dict": FreeDict, "camel": Camel, "alias": Aliased}
    args = choices[unsafe]

    async def unused(a: object, context: ToolContext, work: TurnWork) -> ToolOutcome:
        return ToolOutcome()

    tool = ZoryTool(name="x", description="x", args=args, owners=frozenset({SARA}), handler=unused)
    with pytest.raises(ConfigurationError):
        ToolRegistry([tool])
