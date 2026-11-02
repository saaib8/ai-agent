"""A product's reduction from its old price, worked out here and nowhere else.

The store lists an old price (`regular_price`) beside the current one. Code says
whether that is a real reduction and by how much; an agent only quotes it
(CLAUDE.md 3.3, 17.1). Both prices are the product's own, in its one currency.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from app.schemas.product import ProductDiscount, ProductListing


def discount_of(
    price: Decimal, listing: ProductListing, max_share: float
) -> ProductDiscount | None:
    """The reduction, or None when there is none worth believing.

    None when the store lists no old price, when it is not above the current
    one, when the current price is not positive, when the reduction is under
    one percent (no sale worth saying), or when it is more than `max_share` of
    the old price - an old price that far above the current one is a data
    error, and saying "90% off" on it would mislead.
    """
    old = listing.regular_price
    if old is None or price <= 0 or old <= price:
        return None
    saving = old - price
    if saving > old * Decimal(str(max_share)):
        return None
    percent = int((saving * 100 / old).to_integral_value(rounding=ROUND_FLOOR))
    if percent < 1:
        return None
    return ProductDiscount(old_price=old, saving=saving, percent=percent)
