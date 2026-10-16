"""Browse Catalogue endpoints.

Transport only (CLAUDE.md 3.2): validate the request, resolve the retailer
scope, hand it to the catalog service or the render runtime. The store a page
is read from is the resolved context's, never a filter value.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import (
    CatalogServiceDep,
    CatalogVisualizationRuntimeDep,
    ClickTrackingServiceDep,
    RetailerContextProviderDep,
)
from app.schemas.catalog import CatalogFacets, CatalogPage, CatalogQuery, CatalogVisualizeRequest
from app.schemas.chat import ChatResponse
from app.schemas.language import ReplyLanguage

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/products", response_model=CatalogPage)
async def browse_catalog(
    query: Annotated[CatalogQuery, Query()],
    catalog: CatalogServiceDep,
    retailers: RetailerContextProviderDep,
    tracking: ClickTrackingServiceDep,
) -> CatalogPage:
    """One page of the store's active products, filtered and sorted."""
    context = await retailers.resolve(query.store_id)
    return await tracking.attach(await catalog.browse(query, context), context)


@router.get("/facets", response_model=CatalogFacets)
async def catalog_facets(
    store_id: Annotated[int, Query(ge=1)],
    catalog: CatalogServiceDep,
    retailers: RetailerContextProviderDep,
    language: Annotated[ReplyLanguage | None, Query()] = None,
) -> CatalogFacets:
    """The filter values this store's catalog holds, and what a room render
    allows - labelled in `language`, the conversation's as the client saw it."""
    context = await retailers.resolve(store_id)
    return await catalog.facets(context, language)


@router.post("/visualizations", response_model=ChatResponse)
async def visualize_selection(
    request: CatalogVisualizeRequest,
    runtime: CatalogVisualizationRuntimeDep,
    retailers: RetailerContextProviderDep,
    tracking: ClickTrackingServiceDep,
) -> ChatResponse:
    """Render a room from picked products, as a chat turn."""
    context = await retailers.resolve(request.store_id)
    return await tracking.attach(await runtime.visualize(request, context), context)
