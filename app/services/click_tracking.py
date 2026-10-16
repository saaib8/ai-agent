"""Zory's click id on every product link a customer can follow.

Every `product_url` a response carries leaves as the store's page with
`zory_click_id=<core_product.uuid>`, which the store's page reads and reports
back to Zory's analytics. It is the same id, in the same field, that Zory's own
product links carry (the main backend's `Product.tracking_product_url`), so a
click from the assistant counts like any other and a client opens the link as
it is.

The id is attached here, once the response is finished, from one store-scoped
lookup by the catalog's page. Inside this service `product_url` stays the page
as stored - ranking, grounding, the session and every prompt see it untouched,
and a model never sees the id (CLAUDE.md 20.4). A client that matches products
by page (Zory's widget cart and favourites) drops `zory_click_id` first.

A failed lookup leaves the links untracked rather than failing the turn: the
turn is already committed, and a link without a click id still opens the
product.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, TypeVar, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import get_logger
from app.schemas.retailer import RetailerContext

logger = get_logger(__name__)

CLICK_ID_PARAM = "zory_click_id"

_URL_FIELD = "product_url"

ResponseT = TypeVar("ResponseT", bound=BaseModel)


def tracking_url(product_url: str, click_id: str) -> str:
    """`product_url` with `zory_click_id` set to `click_id`, every other
    query parameter kept as it was."""
    parts = urlsplit(product_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != CLICK_ID_PARAM
    ]
    query.append((CLICK_ID_PARAM, click_id))
    return urlunsplit(parts._replace(query=urlencode(query)))


class ClickIdSource(Protocol):
    async def click_ids_by_url(
        self, product_urls: Sequence[str], context: RetailerContext
    ) -> dict[str, str]: ...


class ClickTrackingService:
    """Puts the click id on every `product_url` of a finished response."""

    def __init__(self, source: ClickIdSource) -> None:
        self._source = source

    async def attach(self, response: ResponseT, context: RetailerContext) -> ResponseT:
        urls: set[str] = set()
        _collect(response, urls)
        if not urls:
            return response
        try:
            click_ids = await self._source.click_ids_by_url(sorted(urls), context)
        except (SQLAlchemyError, OSError) as exc:
            logger.warning(
                "click_ids_unavailable",
                store_id=context.store_id,
                error_type=type(exc).__name__,
            )
            return response
        if not click_ids:
            return response
        return cast(ResponseT, _fill(response, click_ids))


def _page(model: BaseModel) -> str | None:
    """The model's own `product_url`, where it has one."""
    if _URL_FIELD not in type(model).model_fields:
        return None
    url = getattr(model, _URL_FIELD)
    return url if isinstance(url, str) and url else None


def _collect(value: Any, urls: set[str]) -> None:
    if isinstance(value, BaseModel):
        page = _page(value)
        if page is not None:
            urls.add(page)
        for name in type(value).model_fields:
            _collect(getattr(value, name), urls)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _collect(item, urls)
    elif isinstance(value, Mapping):
        for item in value.values():
            _collect(item, urls)


def _fill(value: Any, click_ids: Mapping[str, str]) -> Any:
    """`value` with the click id on every `product_url` that has one.
    Frozen models are copied; anything unchanged is returned as it was."""
    if isinstance(value, BaseModel):
        updates: dict[str, Any] = {}
        for name in type(value).model_fields:
            child = getattr(value, name)
            filled = _fill(child, click_ids)
            if filled is not child:
                updates[name] = filled
        page = _page(value)
        click_id = click_ids.get(page) if page is not None else None
        if page is not None and click_id is not None:
            updates[_URL_FIELD] = tracking_url(page, click_id)
        return value.model_copy(update=updates) if updates else value
    if isinstance(value, (tuple, list)):
        items = [_fill(item, click_ids) for item in value]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return type(value)(items)
    if isinstance(value, dict):
        filled_map = {key: _fill(item, click_ids) for key, item in value.items()}
        if all(filled_map[key] is item for key, item in value.items()):
            return value
        return filled_map
    return value
