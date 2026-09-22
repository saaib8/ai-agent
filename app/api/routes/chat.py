"""The chat endpoint.

Transport only. It validates the request shape, resolves the retailer scope,
hands the exchange to the runtime and returns what comes back. There is no
search, no decision, no response generation and no session handling here: every
one of those is a service, and a route that reached into them would be the
place business logic quietly accumulates (CLAUDE.md 2.2).

Failures map themselves. Every deliberate error in this service carries its own
status and public message, so the registered handler answers them uniformly and
nothing here catches or re-words an exception.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import (
    ChatGraphDep,
    ChatRuntimeDep,
    RetailerContextProviderDep,
)
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    graph: ChatGraphDep,
    runtime: ChatRuntimeDep,
    retailers: RetailerContextProviderDep,
) -> ChatResponse:
    """One customer message, one answer.

    The retailer scope is resolved **here, from the request**, and travels as
    `RetailerContext` from this point on. No model is ever given a store id and
    none can choose one: by the time reasoning happens, scope is already fixed
    by application code (CLAUDE.md 8, 20.2).
    """
    context = await retailers.resolve(request.store_id)
    return await graph.run(runtime, request, context)
