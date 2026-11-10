"""A chip that names a colour, style or material offers only what the store has.

Sara asks before she shows (CLAUDE.md 10.2), and her chips are answers the
customer can tap. A chip saying "Red" promises red is there to be had, so a
chip that names a known colour, style or material must name one the store
carries for what is being talked about - the kind she last asked about
(`ask_about`) or the one being searched, narrowed to what the customer already
decided of it (a 4-seat corner sofa's colours, not every corner sofa's) - read
from the store's own products, never from what the customer likes. Anything
else is sent back once, with what the store does carry, like a figure no tool
gave.

Plain spelling, never meaning: a chip is matched against the vocabulary by
case, spacing and separators only, whole or split on "," "/" "&" "or" "and".
A chip naming no known value ("No preference", "Warm neutrals") is not checked.
Arabic chips are read by the Arabic names the reviewed file gives.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from app.agents.context import ToolContext
from app.agents.decided_stock import decided, decided_pool, looks_of
from app.agents.work import TurnWork
from app.core.exceptions import IntegrationUnavailableError
from app.schemas.catalog_overview import SubcategoryShelf
from app.schemas.discovery import ProductSearchRequest
from app.schemas.text_choice import TextReplyChoice
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes, spelling_key

_PARTS = re.compile(r"\s*(?:,|/|&|\bor\b|\band\b|،|\bأو\b|\bو\b)\s*", re.IGNORECASE)


async def chips_not_in_stock(
    choices: Sequence[TextReplyChoice], work: TurnWork, context: ToolContext
) -> list[str]:
    """Each chip value the store does not carry for the kind talked about, as
    `family: value`. Nothing is sent back when the shelf cannot be read now."""
    attributes = context.services.attributes
    known = _vocabulary(attributes)
    named = [
        hit
        for choice in choices
        for text in (choice.label, choice.value)
        for part in _parts(text)
        if (hit := known.get(spelling_key(part))) is not None
    ]
    if not named:
        return []
    carried = await _carried(work, context)
    if carried is None:
        return []
    off: list[str] = []
    for family, value in named:
        entry = f"{family}: {value}"
        if spelling_key(value) not in carried and entry not in off:
            off.append(entry)
    return off


async def _carried(work: TurnWork, context: ToolContext) -> set[str] | None:
    """Every value the store carries for what is being talked about, as spelling
    keys - None when the catalog cannot be read now.

    Each kind talked about - the one last asked about, the one being searched -
    counts the products meeting what the customer decided of it (4 seats, a
    budget), and its whole shelf, with the kinds shown beside it, only when
    nothing is decided or nothing meets it."""
    services = context.services
    carried: set[str] = set()
    whole_kinds: set[tuple[str, str | None]] = set()
    for category, subcategory, chosen in _talked_of(work):
        pool = await decided_pool(services, chosen, context.retailer) if chosen else ()
        if pool:
            for values in looks_of(pool):
                carried.update(spelling_key(v) for v in values)
            continue
        whole_kinds.add((category, subcategory))
        if services.seating is not None:
            # What a search for the kind shows beside it is on offer too.
            whole_kinds.update((category, b) for b in services.seating.shown_with(subcategory))
    if whole_kinds:
        try:
            overview = await services.capabilities.overview(context.retailer)
        except IntegrationUnavailableError:
            return None
        carried.update(
            spelling_key(value)
            for shelf in overview.shelves
            if _talked_about(shelf, whole_kinds)
            for value in (*shelf.colours, *shelf.styles, *shelf.materials)
        )
    return carried


def _talked_of(work: TurnWork) -> list[tuple[str, str | None, ProductSearchRequest | None]]:
    """The kinds being talked about, each with what is decided of it: the one
    last asked about and the one being searched, with the types searched beside
    it."""
    state = work.staged
    talked: list[tuple[str, str | None, ProductSearchRequest | None]] = []
    if state.kind_asked is not None:
        asked = state.kind_asked
        talked.append((asked.commerce_category, asked.commerce_subcategory, asked.decided))
    if state.active_search is not None:
        request = state.active_search.request
        chosen = decided(request)
        talked.append((request.commerce_category, request.commerce_subcategory, chosen))
        if chosen is None:
            talked.extend(
                (request.commerce_category, kind, None) for kind in request.alongside_subcategories
            )
    return talked


def _talked_about(shelf: SubcategoryShelf, kinds: set[tuple[str, str | None]]) -> bool:
    return any(
        shelf.commerce_category == category
        and (subcategory is None or shelf.commerce_subcategory == subcategory)
        for category, subcategory in kinds
    )


def _vocabulary(attributes: CatalogAttributes) -> dict[str, tuple[AttributeFamily, str]]:
    known: dict[str, tuple[AttributeFamily, str]] = {}
    for family in AttributeFamily:
        for value in attributes.values(family):
            known.setdefault(spelling_key(value), (family, value))
            arabic = attributes.arabic(family, value)
            if arabic:
                known.setdefault(spelling_key(arabic), (family, value))
    return known


def _parts(text: str) -> Iterator[str]:
    yield text
    pieces = [p for p in _PARTS.split(text) if p and p.strip()]
    if len(pieces) > 1:
        yield from pieces
