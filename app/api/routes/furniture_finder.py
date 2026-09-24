"""Furniture Finder endpoints.

Transport only, like the chat route. The upload validates the form, resolves
the retailer scope and hands the bytes to the finder; the pick resolves scope
and hands the request to the turn runtime. Nothing here crops, embeds, searches
or touches a session (CLAUDE.md 3.2).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile

from app.api.dependencies import (
    FinderTurnRuntimeDep,
    FurnitureFinderServiceDep,
    RetailerContextProviderDep,
)
from app.schemas.chat import ChatResponse
from app.schemas.furniture_finder import (
    SESSION_ID_PATTERN,
    FinderPhotoResponse,
    FinderPickRequest,
)

router = APIRouter(prefix="/furniture-finder", tags=["furniture-finder"])


@router.post("/photos", response_model=FinderPhotoResponse)
async def upload_photo(
    session_id: Annotated[str, Form(min_length=1, max_length=128, pattern=SESSION_ID_PATTERN)],
    store_id: Annotated[int, Form(ge=1)],
    image: Annotated[UploadFile, File()],
    finder: FurnitureFinderServiceDep,
    retailers: RetailerContextProviderDep,
) -> FinderPhotoResponse:
    """Upload a photo; get back the objects in it this catalog can match.

    Read one byte past the limit so an oversized upload is recognised without
    reading the whole of it into memory.
    """
    context = await retailers.resolve(store_id)
    data = await image.read(finder.max_upload_bytes + 1)
    return await finder.upload(data, session_id=session_id, context=context)


@router.post("/picks", response_model=ChatResponse)
async def pick_object(
    request: FinderPickRequest,
    runtime: FinderTurnRuntimeDep,
    retailers: RetailerContextProviderDep,
) -> ChatResponse:
    """Pick one object; get back matching products as a chat turn.

    The answer has the same shape as a message's, and its products become the
    list the next message's "the second one" resolves against.
    """
    context = await retailers.resolve(request.store_id)
    return await runtime.pick(request, context)
