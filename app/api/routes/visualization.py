"""Room visualisation endpoint.

Transport only, like the chat route: validate the request, resolve the
retailer scope, hand it to the turn runtime. Nothing here reads the room,
builds a prompt or calls an image model (CLAUDE.md 3.2).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import RetailerContextProviderDep, VisualizationTurnRuntimeDep
from app.schemas.chat import ChatResponse
from app.schemas.visualization import VisualizeRequest

router = APIRouter(tags=["visualization"])


@router.post("/visualizations", response_model=ChatResponse)
async def visualize_room(
    request: VisualizeRequest,
    runtime: VisualizationTurnRuntimeDep,
    retailers: RetailerContextProviderDep,
) -> ChatResponse:
    """Render the session's room package from the chosen view, as a chat turn."""
    context = await retailers.resolve(request.store_id)
    return await runtime.visualize(request, context)
