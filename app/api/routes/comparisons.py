"""Comparing two checked cards in a pop-up, and which cards can be compared.

Transport only (CLAUDE.md 3.2): validate, resolve the retailer scope, hand
over. The comparison is read-only - no chat turn, nothing saved.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import (
    CardComparisonServiceDep,
    ResourcesDep,
    RetailerContextProviderDep,
)
from app.schemas.card_comparison import (
    CardComparisonRequest,
    CardComparisonResponse,
    CompareGroupsResponse,
)

router = APIRouter(tags=["comparisons"])


@router.post("/comparisons", response_model=CardComparisonResponse)
async def compare_cards(
    request: CardComparisonRequest,
    service: CardComparisonServiceDep,
    retailers: RetailerContextProviderDep,
) -> CardComparisonResponse:
    """Two cards on screen, side by side, with a short take on what differs."""
    context = await retailers.resolve(request.store_id)
    return await service.compare(request, context)


@router.get("/compare-groups", response_model=CompareGroupsResponse)
async def compare_groups(app_resources: ResourcesDep) -> CompareGroupsResponse:
    """The reviewed families products compare within."""
    groups = app_resources.compare_groups
    if groups is None:
        return CompareGroupsResponse(version="none")
    return CompareGroupsResponse(version=groups.version, groups=groups.grouped)
