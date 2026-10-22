"""Figures in a reply are references code writes in; sums are calculate's."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.agents.figures import figure_table, flatten, render
from app.agents.loop import Responded
from app.agents.reply import repaired
from app.agents.tools.calculate import calculate_tool
from app.agents.tools.common import RespondArgs
from app.agents.work import ToolRecord, ToolStatus, TurnWork
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.conversation import ConversationContext
from langchain_core.messages import ToolMessage

from tests.unit.agents.fake_chat_model import ScriptedChatModel, call, step
from tests.unit.agents.test_agent_loop import Calls, _loop, _run

SARA = AgentPersona.SARA
BRIEF = {
    "cards_on_screen": [{"card": 2, "price": "3600", "currency": "SAR"}],
    "cart": {"totals": [{"currency": "SAR", "total": "4250.50"}]},
    "room": {
        "size": [
            {"measures": "room_length", "cm": "500"},
            {"measures": "room_width", "cm": "400"},
        ]
    },
}


def _work(view: dict[str, Any] = BRIEF, said: tuple[str, ...] = ()) -> TurnWork:
    work = TurnWork.starting(AgentStateV1(), said=said)
    work.show(view)
    return work


def test_each_figure_is_keyed_by_what_it_is() -> None:
    keys = flatten(BRIEF)

    assert keys["cards_on_screen.card2.price"] == Decimal(3600)
    assert keys["cart.totals.sar.total"] == Decimal("4250.50")
    assert keys["room.size.room_width.cm"] == Decimal(400)


def test_a_reference_is_written_in_and_an_unknown_one_is_named() -> None:
    table = flatten(BRIEF)

    text, unknown = render(
        "Card 2 is {cards_on_screen.card2.price} SAR; cart {cart.totals.sar.total}. {nope}",
        table,
    )

    assert text == "Card 2 is 3,600 SAR; cart 4,250.50. {nope}"
    assert unknown == ("nope",)


async def test_a_reply_cites_figures_and_a_wrong_key_is_corrected_in_the_hop() -> None:
    model = ScriptedChatModel(
        script=[
            step(call("respond", "a", message="It is {cards_on_screen.card9.price} SAR.")),
            step(call("respond", "b", message="It is {cards_on_screen.card2.price} SAR.")),
        ]
    )

    end = await _run(_loop(Calls()), model, work=_work())

    assert isinstance(end, Responded) and end.reply.message == "It is 3,600 SAR."
    told = model.seen[1][-1]
    assert isinstance(told, ToolMessage) and "card9" in str(told.content)


async def _calculate(work: TurnWork, **args: Any) -> Any:
    tool = calculate_tool()
    assert tool.handler is not None
    return await tool.handler(tool.args.model_validate(args), None, work)  # type: ignore[arg-type]


async def test_a_sum_is_worked_out_from_facts_and_becomes_a_figure() -> None:
    work = _work()

    outcome = await _calculate(
        work,
        op="area_m2",
        inputs=["{room.size.room_length.cm}", "room.size.room_width.cm"],
        what="floor area",
    )
    work.record(ToolRecord(agent=SARA, tool="calculate", status=ToolStatus.OK, facts=outcome.facts))

    assert outcome.facts["result"] == "20"
    assert figure_table(work)["calculate.result"] == Decimal(20)


async def test_a_sum_starts_only_from_facts_or_the_customers_words() -> None:
    work = _work(said=("we are 10 people",))

    four_never_said = await _calculate(work, op="subtract", inputs=["10", "4"], what="seats")
    invented = await _calculate(work, op="subtract", inputs=["10", "3"], what="seats")

    assert four_never_said.status is ToolStatus.REFUSED
    assert invented.code == "unknown_input"
    allowed = await _calculate(
        _work(said=("we are 10 people", "the first seats 4")),
        op="subtract",
        inputs=["10", "4"],
        what="seats left",
    )
    assert allowed.facts["result"] == "6"


def test_a_stray_figure_costs_only_its_sentence() -> None:
    work = _work()
    reply = RespondArgs(
        message="Card 2 is 3,600 SAR. Your room is 37 m². Shall I show matching rugs?"
    )

    kept = repaired(reply, work, "hi", ConversationContext(), work.views)

    assert kept is not None
    assert kept.message == "Card 2 is 3,600 SAR. Shall I show matching rugs?"
    nothing = repaired(RespondArgs(message="It is 37 m²."), work, "hi", ConversationContext(), ())
    assert nothing is None


def test_two_objects_of_one_kind_never_share_a_key() -> None:
    keys = flatten(
        {
            "budgets": [
                {"applies_to": "search", "currency": "SAR", "at_most": "5000"},
                {"applies_to": "room", "currency": "SAR", "at_most": "20000"},
            ],
            "size": [
                {"measures": "usable_wall", "cm": "400", "label": "TV wall"},
                {"measures": "usable_wall", "cm": "300", "label": "window wall"},
                {"measures": "usable_wall", "cm": "250", "label": "الجدار"},
                {"measures": "usable_wall", "cm": "200", "label": "الجدار"},
            ],
        }
    )

    assert keys["budgets.search.at_most"] == Decimal(5000)
    assert keys["budgets.room.at_most"] == Decimal(20000)
    assert keys["size.tv_wall.cm"] == Decimal(400)
    assert keys["size.window_wall.cm"] == Decimal(300)
    assert {keys["size.3.cm"], keys["size.4.cm"]} == {Decimal(250), Decimal(200)}


def test_a_new_list_retires_the_cards_it_replaced() -> None:
    work = _work()
    work.record(
        ToolRecord(
            agent=SARA,
            tool="search_products",
            status=ToolStatus.OK,
            facts={"products": [{"card": 2, "price": "990"}]},
        )
    )

    table = figure_table(work)

    assert "cards_on_screen.card2.price" not in table
    assert table["search_products.products.card2.price"] == Decimal(990)
