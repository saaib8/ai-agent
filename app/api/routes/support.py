"""The store's support: what to hand it when a shopper's request is sent.

Transport only (CLAUDE.md 3.2). Called by the platform, never by a model; the
platform does the sending, and reading changes nothing.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import RetailerContextProviderDep, SupportHandoverServiceDep
from app.core.exceptions import SupportHandoverOffError
from app.schemas.support import SupportHandover, SupportHandoverRequest

router = APIRouter(prefix="/support", tags=["support"])


@router.post("/handover", response_model=SupportHandover)
async def support_handover(
    request: SupportHandoverRequest,
    service: SupportHandoverServiceDep,
    retailers: RetailerContextProviderDep,
) -> SupportHandover:
    """The shopper's recent messages, for the request sent to the store."""
    if service is None:
        raise SupportHandoverOffError()
    context = await retailers.resolve(request.store_id)
    return await service.handover(context, request.session_id)
