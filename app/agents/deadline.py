"""One turn's time limit, longer while a room is being designed.

Most turns - a search, a question, the cart - keep the short limit. Designing
a whole room is one long careful step (CLAUDE.md 10.1), so once Nora is
designing one the limit stretches to the room's; a stuck call still ends,
because the longer limit is a limit too.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def within[T](
    work: Awaitable[T], *, short_s: float, long_s: float, extended: Callable[[], bool]
) -> T:
    """The work's result, or TimeoutError past `short_s` - or past `long_s`
    when `extended()` says the turn is designing a room by then."""
    task = asyncio.ensure_future(work)
    done, _ = await asyncio.wait({task}, timeout=short_s)
    if not done and extended() and long_s > short_s:
        done, _ = await asyncio.wait({task}, timeout=long_s - short_s)
    if not done:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise TimeoutError
    return task.result()
