"""What the store carries for what the customer has already decided.

A kind's shelf says what the kind comes in - every corner sofa, whatever it
seats. Once the customer has decided something (4 seats, a budget, a size), the
answers worth offering are only those some product meeting it comes in: a
Powder Blue corner sofa that seats three is no answer for a 4-seat one
(CLAUDE.md 10.2). Read from the eligible products of the request so far - the
same read a search makes - with its colours and styles left out, since those are
what is being asked.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.agents.services import AgentServices
from app.core.exceptions import IntegrationUnavailableError
from app.schemas.discovery import ProductSearchRequest
from app.schemas.product import EligibleProduct
from app.schemas.retailer import RetailerContext


def with_kinds_beside(
    services: AgentServices, request: ProductSearchRequest
) -> ProductSearchRequest:
    """The request as a search would run it: the kinds shown beside it (a sofa
    search shows sets and corner sofas) included, unless it is for the type alone
    or carries a size those listings cannot answer (CLAUDE.md 10.2)."""
    if (
        services.seating is None
        or request.single_type
        or request.dimensions
        or request.planar_dimensions is not None
        or request.alongside_subcategories
    ):
        return request
    beside = services.seating.shown_with(request.commerce_subcategory)
    return request.model_copy(update={"alongside_subcategories": beside}) if beside else request


def decided(request: ProductSearchRequest) -> ProductSearchRequest | None:
    """The request narrowed to what the customer decided beyond the kind - seats,
    budget, size, weight, a sale or rating - with the colours and styles being
    asked about, the pages already seen and the order set aside. None when
    nothing beyond the kind is decided: the kind's shelf answers that."""
    if not (
        request.seating_capacity is not None
        or request.price is not None
        or request.dimensions
        or request.planar_dimensions is not None
        or request.weight is not None
        or request.on_discount
        or request.rating_at_least is not None
    ):
        return None
    return request.model_copy(
        update={"colors_any_of": (), "styles_all_of": (), "exclude_product_ids": ()}
    )


async def decided_pool(
    services: AgentServices, request: ProductSearchRequest, context: RetailerContext
) -> tuple[EligibleProduct, ...]:
    """Every product meeting what was decided; empty when there is none or the
    catalog cannot be read now - the kind's shelf answers then."""
    if services.discovery is None:
        return ()
    try:
        return await services.discovery.eligible_pool(request, context)
    except IntegrationUnavailableError:
        return ()


def looks_of(pool: Iterable[EligibleProduct]) -> tuple[list[str], list[str], list[str]]:
    """The colours, styles and main materials among these products, as stored,
    most-stocked first."""
    colours: dict[str, int] = {}
    styles: dict[str, int] = {}
    materials: dict[str, int] = {}
    for product in pool:
        if product.main_color:
            colours[product.main_color] = colours.get(product.main_color, 0) + 1
        for style in product.styles:
            styles[style] = styles.get(style, 0) + 1
        if product.material:
            materials[product.material] = materials.get(product.material, 0) + 1
    return _most_first(colours), _most_first(styles), _most_first(materials)


def _most_first(counts: dict[str, int]) -> list[str]:
    return sorted(counts, key=lambda value: -counts[value])
