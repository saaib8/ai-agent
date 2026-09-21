"""The one place a verified product becomes something a reply may cite.

Pure mapping, no derivation. Every field is copied from the allowlisted
candidate the repository produced; nothing is computed, inferred or filled in.
A builder that calculated anything would be a second source of product truth.

`relaxation_depth` defaults to None and is supplied only by a caller that
actually ran the search. A comparison or a detail question grounds a product
no search returned this turn, and passing zero there would assert
`matched_exactly` about a search nobody ran.
"""

from __future__ import annotations

from app.schemas.grounding import GroundedProduct
from app.schemas.product import ProductCandidate


def to_grounded_product(
    product: ProductCandidate,
    *,
    grounding_ref: int,
    presented_ordinal: int | None = None,
    relaxation_depth: int | None = None,
) -> GroundedProduct:
    """One verified product, as this turn may talk about it.

    `grounding_ref` is turn-local and assigned by the caller; it is what the
    response model cites, and it is deliberately not the product id.

    `presented_ordinal` is supplied only when the product really occupies that
    position in this turn's result list. Inferring one from the order ids
    happened to arrive in would claim a place on screen it does not have.
    """
    return GroundedProduct(
        grounding_ref=grounding_ref,
        presented_ordinal=presented_ordinal,
        name_english=product.name_english,
        price_amount=product.price_amount,
        price_unit=product.price_unit,
        image_url=product.image_url,
        product_url=product.product_url,
        commerce=product.commerce,
        dimensions=product.dimensions,
        main_color=product.main_color,
        styles=product.styles,
        relaxation_depth=relaxation_depth,
    )
