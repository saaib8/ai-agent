"""A turn's time limit: short for most turns, the room's while one is designed."""

from __future__ import annotations

import asyncio

import pytest
from app.agents.deadline import within


async def _takes(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "done"


async def test_work_inside_the_short_limit_finishes() -> None:
    assert await within(_takes(0.01), short_s=0.2, long_s=1, extended=lambda: False) == "done"


async def test_work_past_the_short_limit_stops_when_no_room_is_designed() -> None:
    with pytest.raises(TimeoutError):
        await within(_takes(0.5), short_s=0.05, long_s=1, extended=lambda: False)


async def test_a_room_being_designed_gets_the_longer_limit() -> None:
    assert await within(_takes(0.2), short_s=0.05, long_s=1, extended=lambda: True) == "done"


async def test_the_longer_limit_is_a_limit_too() -> None:
    with pytest.raises(TimeoutError):
        await within(_takes(1), short_s=0.05, long_s=0.15, extended=lambda: True)
