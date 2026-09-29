"""The picks tray: ticking and unticking products on screen.

Transport only (CLAUDE.md 3.2): validate the request, resolve the retailer
scope, hand it to the picks runtime. A tick changes the session silently - no
chat turn, no reply - and answers with the picks as they now stand.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import PicksRuntimeDep, RetailerContextProviderDep
from app.schemas.picks import PicksRequest, PicksResponse

router = APIRouter(tags=["picks"])


@router.post("/picks", response_model=PicksResponse)
async def change_picks(
    request: PicksRequest,
    runtime: PicksRuntimeDep,
    retailers: RetailerContextProviderDep,
) -> PicksResponse:
    """Tick a card, or untick a pick; get the picks back."""
    context = await retailers.resolve(request.store_id)
    return await runtime.apply(request, context)
