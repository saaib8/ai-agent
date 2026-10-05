"""The moves the agent loop may make after a weak search (CLAUDE.md 14.8).

Each step is one structured answer: try another stocked type with every other
limit kept, or finish - keeping the original reply or presenting a type already
tried. The choices are built per step from what the store stocks and what was
tried, so a value outside them is unrepresentable rather than merely refused;
code checks the answer again anyway.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, create_model

ORIGINAL = "original"
"""Finish without a substitute: the original reply, with its own offers."""


class TryType(BaseModel):
    """Run the same search for another stocked type, every other limit kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["try_type"] = "try_type"
    commerce_subcategory: str


class Finish(BaseModel):
    """Stop: present a type already tried, or keep the original reply."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["finish"] = "finish"
    present: str


class LoopMove(BaseModel):
    """One step's answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    move: TryType | Finish


def build_move_schema(can_try: tuple[str, ...], can_present: tuple[str, ...]) -> type[LoopMove]:
    """`LoopMove` restricted to this step's choices.

    A plain union of models, each with a literal `kind`: structured output
    renders it as `anyOf`, which the strict schema converter accepts. With
    nothing left to try, the only move is to finish.
    """
    present = (ORIGINAL, *can_present)
    finish = create_model(
        "ConstrainedFinish",
        __base__=Finish,
        present=(Literal[present], ...),
    )
    if not can_try:
        return create_model("FinishOnlyMove", __base__=LoopMove, move=(finish, ...))
    try_type = create_model(
        "ConstrainedTryType",
        __base__=TryType,
        commerce_subcategory=(Literal[can_try], ...),
    )
    return create_model(
        "ConstrainedLoopMove",
        __base__=LoopMove,
        move=(try_type | finish, ...),
    )
