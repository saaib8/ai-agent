"""Stock and fit, checked before a new search asks its card or runs.

A salesperson looks at the shelf before answering. Before this check a request
for something the store does not carry still got that thing's card of
questions, and "a bunk bed" was answered as if any bed were one. Here the
resolved request is checked against the live catalog first (CLAUDE.md 14.7):

- a type the store does not stock is replaced by the closest type it does -
  same category first, then any - or left alone when nothing is close;
- a kind narrower than any approved type ("bunk bed" under `bed`) is looked up
  by name within that type, and its absence is recorded for the reply.

The model only judges *which* stocked type is closest. Whether the store
carries something is always a catalog fact: an overview, or a name count. The
check is fail-open - a catalog or provider that cannot answer leaves the search
exactly as it was, and nothing is claimed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from app.core.exceptions import (
    IntegrationUnavailableError,
    LLMRequestError,
    LLMResponseInvalidError,
)
from app.core.logging import get_logger
from app.repositories.products import ProductRepository
from app.schemas.catalog_overview import CatalogOverview
from app.schemas.discovery import ProductSearchRequest
from app.schemas.grounding import DroppedConstraint
from app.schemas.query import ResolvedSearch
from app.schemas.retailer import RetailerContext
from app.services.catalog_capability import CatalogCapabilityService
from app.services.closest_type import ClosestTypeResolver
from app.services.retype import as_type, dropped_sizes
from app.taxonomy.seating import SeatingSemantics

logger = get_logger(__name__)

_HANDLED_FAILURES = (IntegrationUnavailableError, LLMRequestError, LLMResponseInvalidError)

_WORD = re.compile(r"[a-z]+")

MIN_NAME_WORD = 3
"""Shorter words ("a", "of") would match nearly any name and prove nothing."""


class StockFit(StrEnum):
    FITS = "fits"
    """Stocked, and what they asked for - or nothing could be checked."""

    SUBSTITUTED = "substituted"
    """Not stocked; the closest stocked type replaces it, to be disclosed."""

    NOT_CARRIED = "not_carried"
    """Not stocked and nothing close: no card, the honest zero result."""

    KIND_NOT_FOUND = "kind_not_found"
    """The type is stocked, but nothing in it is named as the narrower kind."""


@dataclass(frozen=True, slots=True)
class StockFitOutcome:
    fit: StockFit
    resolved: ResolvedSearch
    """What to search: the request as asked, or the substitute type's."""

    asked: str | None = None
    """The type (a registry key) or kind (their words) the reply must name as
    not carried or not found; None when the request fits."""

    dropped: tuple[DroppedConstraint, ...] = ()
    """Sizes the customer gave for the asked type, which the substitute does not
    take (CLAUDE.md 13.5) - reported, never dropped quietly."""


class StockFitCheck:
    """Checks one resolved request against the store's live catalog."""

    def __init__(
        self,
        capabilities: CatalogCapabilityService,
        products: ProductRepository,
        closest_type: ClosestTypeResolver,
        seating: SeatingSemantics | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._products = products
        self._closest_type = closest_type
        self._seating = seating
        """Reviewed seat counts: a request for several seats is never offered a
        one-seat type in place of the one it asked for (CLAUDE.md 7, 27.1)."""

    async def check(self, resolved: ResolvedSearch, context: RetailerContext) -> StockFitOutcome:
        try:
            overview = await self._capabilities.overview(context)
            resolved = _broad_if_catch_all(resolved, overview)
            request = resolved.request
            if not _stocked(overview, request.commerce_category, request.commerce_subcategory):
                return await self._substitute(resolved, overview, context)
            if resolved.asked_kind is not None:
                return await self._find_kind(resolved, resolved.asked_kind, context)
        except _HANDLED_FAILURES as exc:
            logger.warning(
                "stock_fit_unavailable", store_id=context.store_id, error=type(exc).__name__
            )
        return StockFitOutcome(fit=StockFit.FITS, resolved=resolved)

    async def _substitute(
        self, resolved: ResolvedSearch, overview: CatalogOverview, context: RetailerContext
    ) -> StockFitOutcome:
        request = resolved.request
        category = request.commerce_category
        asked = request.commerce_subcategory or category
        if resolved.kind_required:
            # "Only recliners, nothing else": the closest thing is not what
            # they asked for, and they said so. The honest answer stands.
            logger.info("stock_fit_not_carried", store_id=context.store_id, asked=asked)
            return StockFitOutcome(fit=StockFit.NOT_CARRIED, resolved=resolved, asked=asked)
        # One choice over every stocked type, its own family first: the
        # nearest thing is usually a sibling, but a family the store does not
        # carry at all still has neighbours elsewhere (CLAUDE.md 14.7).
        family_first = sorted(
            overview.shelves, key=lambda shelf: shelf.commerce_category != category
        )
        several = seats_wanted(request) > 1
        offered = tuple(
            (shelf.commerce_category, shelf.commerce_subcategory)
            for shelf in family_first
            if shelf.commerce_subcategory is not None
            and shelf.commerce_subcategory != asked
            and not (several and self._seats_one(shelf.commerce_subcategory))
        )
        picked = await self._closest_type.closest_anywhere(
            asked=asked, offered=offered, context=context
        )
        if picked is None:
            logger.info("stock_fit_not_carried", store_id=context.store_id, asked=asked)
            return StockFitOutcome(fit=StockFit.NOT_CARRIED, resolved=resolved, asked=asked)
        logger.info(
            "stock_fit_substituted",
            store_id=context.store_id,
            asked=asked,
            offered=picked[1],
            across_categories=picked[0] != category,
        )
        return StockFitOutcome(
            fit=StockFit.SUBSTITUTED,
            resolved=as_type(resolved, *picked),
            asked=asked,
            dropped=dropped_sizes(resolved),
        )

    def _seats_one(self, subcategory: str) -> bool:
        return self._seating is not None and self._seating.seats_one(subcategory)

    async def _find_kind(
        self, resolved: ResolvedSearch, kind: str, context: RetailerContext
    ) -> StockFitOutcome:
        words = _name_words(kind)
        if not words:
            # Nothing specific enough to look up: claim nothing.
            return StockFitOutcome(fit=StockFit.FITS, resolved=resolved)
        named = await self._products.count_named(words, context)
        logger.info(
            "stock_fit_kind_checked", store_id=context.store_id, found=named > 0, words=len(words)
        )
        if named:
            return StockFitOutcome(fit=StockFit.FITS, resolved=resolved)
        return StockFitOutcome(fit=StockFit.KIND_NOT_FOUND, resolved=resolved, asked=kind)


def seats_wanted(request: ProductSearchRequest) -> int:
    capacity = request.seating_capacity
    if capacity is None or capacity.min_capacity is None:
        return 0
    return capacity.min_capacity


def _broad_if_catch_all(resolved: ResolvedSearch, overview: CatalogOverview) -> ResolvedSearch:
    """A category's same-named catch-all child, read as the category itself.

    `lighting/lighting` holds stock that fits no specific lighting type; a
    store with none of it but plenty of chandeliers still carries lighting, so
    "we don't carry lighting" would be false. When the catch-all is not
    stocked but its family is, the request is the family's.
    """
    request = resolved.request
    category = request.commerce_category
    if (
        request.commerce_subcategory != category
        or overview.stocks(category, category)
        or not overview.shelves_in(category)
    ):
        return resolved
    return resolved.model_copy(
        update={
            "request": request.model_copy(update={"commerce_subcategory": None}),
            "semantics": resolved.semantics.model_copy(update={"subcategory": None}),
        }
    )


def _stocked(overview: CatalogOverview, category: str, subcategory: str | None) -> bool:
    """A named type is stocked when its shelf exists; a whole category when any
    shelf in it does."""
    if subcategory is not None:
        return overview.stocks(category, subcategory)
    return bool(overview.shelves_in(category))


def _name_words(kind: str) -> tuple[str, ...]:
    """The words a product's name must carry to be that kind.

    Spelling only: lower case and letters (CLAUDE.md 14.2). A trailing plural
    "s" is dropped so "bunk beds" finds "Bunk Bed"; the substring match then
    finds the singular and the plural alike.
    """
    words = []
    for word in _WORD.findall(kind.lower()):
        if len(word) > MIN_NAME_WORD and word.endswith("s"):
            word = word[:-1]
        if len(word) >= MIN_NAME_WORD:
            words.append(word)
    return tuple(dict.fromkeys(words))
