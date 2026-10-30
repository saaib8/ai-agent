"""The chat endpoint.

Transport only. It validates the request shape, resolves the retailer scope,
hands the exchange to the runtime and returns what comes back - whole, or as a
stream of progress ending in the reply. There is no search, no agent and no
session handling here: every one of those is a service.

Failures map themselves. Every deliberate error in this service carries its own
status and public message, so the registered handler answers them uniformly and
nothing here catches or re-words an exception.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.agents.progress import progress_of
from app.agents.trace import TraceEvent
from app.api.dependencies import (
    ChatGraphDep,
    ChatRuntimeDep,
    ClickTrackingServiceDep,
    RetailerContextProviderDep,
)
from app.core.exceptions import ZoryError
from app.core.logging import get_logger
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])
logger = get_logger(__name__)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    graph: ChatGraphDep,
    runtime: ChatRuntimeDep,
    retailers: RetailerContextProviderDep,
    tracking: ClickTrackingServiceDep,
) -> ChatResponse:
    """One customer message, one answer.

    The retailer scope is resolved **here, from the request**, and travels as
    `RetailerContext` from this point on. No model is ever given a store id and
    none can choose one: by the time reasoning happens, scope is already fixed
    by application code (CLAUDE.md 8, 20.2).
    """
    context = await retailers.resolve(request.store_id)
    return await tracking.attach(await graph.run(runtime, request, context), context)


_STREAM_BUFFER = 64
"""Progress events held for a slow client; past it the oldest news is dropped,
never the reply."""


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    graph: ChatGraphDep,
    runtime: ChatRuntimeDep,
    retailers: RetailerContextProviderDep,
    tracking: ClickTrackingServiceDep,
) -> StreamingResponse:
    """The same exchange as `/chat`, run once, with live progress.

    Server-sent events: `progress` while the agents work - which agent, and a
    word for what it is doing - then exactly one `reply` carrying the
    committed `ChatResponse`, or one `error` with the public message. The
    reply is sent only after the session was committed, so a client that sees
    no reply must re-read the session rather than replay the request.
    """
    context = await retailers.resolve(request.store_id)
    events: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=_STREAM_BUFFER)

    def offer(event: TraceEvent) -> None:
        news = progress_of(event)
        if news is None:
            return
        if events.full():
            events.get_nowait()
        events.put_nowait(("progress", json.dumps(news)))

    runtime.on_event = offer

    async def turn() -> None:
        try:
            reply = await tracking.attach(await graph.run(runtime, request, context), context)
            outcome = ("reply", reply.model_dump_json())
        except ZoryError as exc:
            outcome = (
                "error",
                json.dumps(
                    {"code": exc.code, "message": exc.public_message, "status": exc.http_status}
                ),
            )
        except Exception:
            # A defect: logged in full here, and the client told only that it
            # failed - the stream must still end.
            logger.exception("chat_stream_failed", store_id=context.store_id)
            outcome = (
                "error",
                json.dumps({"code": ZoryError.code, "message": ZoryError.public_message}),
            )
        while events.full():
            events.get_nowait()
        await events.put(outcome)

    async def stream() -> AsyncIterator[str]:
        task = asyncio.create_task(turn())
        try:
            while True:
                kind, data = await events.get()
                yield f"event: {kind}\ndata: {data}\n\n"
                if kind != "progress":
                    break
        finally:
            if not task.done():
                # The client left: nothing was committed unless the turn
                # finished, and the compare-and-set keeps a half turn out.
                task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
