"""Arithmetic an agent needs, done in code.

The room's floor area, the seats still to find, how far over budget, the gap
between two prices: true figures an agent would otherwise work out in its head,
where a slip reads exactly like an invented number. Each input is a figure key
(`app/agents/figures.py`) or a number the customer said, so a calculation can
only start from facts; its result becomes a figure the reply cites as
`{calculate.result}`.
"""

from __future__ import annotations

from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Literal

from pydantic import Field

from app.agents.context import ToolContext
from app.agents.figures import REFERENCE, figure_table
from app.agents.tools.base import ToolArgs, ToolOutcome, ZoryTool
from app.agents.tools.common import EVERY_AGENT
from app.agents.work import ToolStatus, TurnWork
from app.services.numeric_guard import canonical_number, numbers_in

Operation = Literal["add", "subtract", "multiply", "divide", "area_m2", "percent_of"]


class CalculateArgs(ToolArgs):
    op: Operation = Field(
        description=(
            "add or multiply all inputs; subtract or divide the first by the rest; area_m2 "
            "multiplies two lengths in cm into square metres; percent_of is the first as a "
            "percentage of the second."
        )
    )
    inputs: list[str] = Field(
        min_length=2,
        max_length=6,
        description=(
            "Each a figure key from figures (cards_on_screen.card2.price) or a number the "
            "customer said."
        ),
    )
    what: str = Field(min_length=1, max_length=80, description="What the result is, in words.")


def calculate_tool() -> ZoryTool[CalculateArgs]:
    async def calculate(args: CalculateArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        table = figure_table(work)
        said = {figure for words in work.said for figure in numbers_in(words)}
        values: list[Decimal] = []
        for raw in args.inputs:
            text = raw.strip()
            matched = REFERENCE.fullmatch(text)
            key = matched.group(1) if matched else text
            if key in table:
                values.append(table[key])
                continue
            number = canonical_number(text)
            if number is not None and number in said:
                values.append(Decimal(number))
                continue
            # Neither a fact nor their words: a figure nobody gave.
            return ToolOutcome(
                status=ToolStatus.REFUSED, code="unknown_input", facts={"input": text[:60]}
            )
        try:
            result = _apply(args.op, values)
        except (DivisionByZero, InvalidOperation, ZeroDivisionError):
            return ToolOutcome(status=ToolStatus.REFUSED, code="cannot_divide_by_zero")
        if result is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="takes_two_inputs")
        # Only the figure: the agent's own description stays out of the facts,
        # where a number in it would pass for one a tool gave.
        return ToolOutcome(facts={"result": _rounded(result)})

    return ZoryTool(
        name="calculate",
        description=(
            "Work out a figure from facts - a room's floor area, seats still to find, how far "
            "over budget, the difference between two prices. Never do sums yourself: cite "
            "the result by the key its figures show."
        ),
        args=CalculateArgs,
        owners=EVERY_AGENT,
        handler=calculate,
    )


def _apply(op: Operation, values: list[Decimal]) -> Decimal | None:
    first, rest = values[0], values[1:]
    if op == "add":
        return sum(values, Decimal(0))
    if op == "multiply":
        total = Decimal(1)
        for value in values:
            total *= value
        return total
    if op == "subtract":
        return first - sum(rest, Decimal(0))
    if op == "divide":
        for value in rest:
            first /= value
        return first
    if len(values) != 2:
        return None
    if op == "area_m2":
        return first * rest[0] / Decimal(10_000)
    return first / rest[0] * Decimal(100)


def _rounded(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01")).normalize()
    return format(quantized, "f")
