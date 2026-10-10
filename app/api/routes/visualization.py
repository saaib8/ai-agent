"""Room visualisation endpoints: render a room, and upload the customer's own.

Transport only, like the chat route: validate the request, resolve the
retailer scope, hand it to the turn runtime. Nothing here reads the room,
builds a prompt or calls an image model (CLAUDE.md 3.2).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile

from app.api.dependencies import (
    RetailerContextProviderDep,
    RoomPhotoServiceDep,
    VisualizationTurnRuntimeDep,
)
from app.schemas.chat import ChatResponse
from app.schemas.furniture_finder import SESSION_ID_PATTERN
from app.schemas.room_photo import RoomPhotoResponse
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


@router.post("/room-photos", response_model=RoomPhotoResponse)
async def upload_room_photo(
    session_id: Annotated[str, Form(min_length=1, max_length=128, pattern=SESSION_ID_PATTERN)],
    store_id: Annotated[int, Form(ge=1)],
    image: Annotated[UploadFile, File()],
    service: RoomPhotoServiceDep,
    retailers: RetailerContextProviderDep,
) -> RoomPhotoResponse:
    """Upload a photo of the customer's room: checked, emptied and kept for
    the session. Its id is then passed to a render to place the pieces in it.

    Reads one byte past the limit so an oversized upload is recognised without
    reading the whole of it into memory.
    """
    context = await retailers.resolve(store_id)
    data = await image.read(service.max_upload_bytes + 1)
    return await service.upload(data, session_id=session_id, context=context)
