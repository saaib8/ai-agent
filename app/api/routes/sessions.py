"""A guest's chat, moved under the account they signed in to.

Transport only (CLAUDE.md 3.2). Called by the platform once it has checked the
guest's own sign-in - never by a model, and never by the widget directly.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import ConversationAdoptionServiceDep, RetailerContextProviderDep
from app.core.exceptions import ConversationAdoptionOffError
from app.schemas.session_adoption import ConversationAdoption, ConversationAdoptionRequest

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("/adopt", response_model=ConversationAdoption)
async def adopt_conversation(
    request: ConversationAdoptionRequest,
    service: ConversationAdoptionServiceDep,
    retailers: RetailerContextProviderDep,
) -> ConversationAdoption:
    """Move the guest's chat to the account's name and add their cart to its cart."""
    if service is None:
        raise ConversationAdoptionOffError()
    context = await retailers.resolve(request.store_id)
    return await service.adopt(context, request)
