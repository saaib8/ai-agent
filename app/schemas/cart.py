"""The customer's cart, as a client draws it and copies it into the store's.

The cart is their picks with how many of each (CLAUDE.md 36): selecting a
product adds it, and every way of selecting - a tap, "add the 7th one", a
seating combination, a whole room - fills the same list. The server keeps it
and says what is in it on every reply, so the agent can read it and a client
never keeps a copy of its own.

**Prices are read fresh, never remembered.** Each line's price and the totals
come from the catalog when the cart is drawn, so a price that changed since
they added the product shows today's price (CLAUDE.md 61).

**The store's own cart is the client's to fill.** Only the storefront page can
write a shopper's cart - on Salla, `salla.cart.addItem` - so every reply that
changed the cart lists what changed (`changes`), and each line names its
product the way the store knows it (`product`) for the client to copy across.
Requests still never carry a product id: a line is named by its place in the
cart (`pick`), as a pick is (CLAUDE.md 20.2).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.agent_state import MAX_CART_QUANTITY
from app.schemas.furniture_finder import SESSION_ID_PATTERN
from app.schemas.product import ProductDisplay


class CartProductRef(BaseModel):
    """The product as the store knows it, for copying it into the store's cart.

    Never sent back to this service: requests name a line by its place.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    uuid: str = Field(min_length=1)
    """The catalogue product's public id - what Zory's
    `POST /api/widgets/cart/salla-item/` takes as `product`."""

    salla_id: str | None = None
    """The product's id on Salla, where the store syncs from Salla; None for a
    product the store did not get from Salla."""

    url: str = Field(min_length=1)
    """Its page on the store, with Zory's `zory_click_id` (the `uuid`) - the
    link to open (`app/services/click_tracking.py`)."""


class CartLineView(BaseModel):
    """One product in the cart and how many, read fresh from the catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pick: int = Field(ge=1)
    """Its place in the cart - the number a quantity change or a removal names,
    the same number as its pick. A product the catalog no longer returns is
    left out without renumbering the rest."""

    quantity: int = Field(ge=1, le=MAX_CART_QUANTITY)
    name_english: str
    image_url: str
    unit_price: Decimal
    price_unit: str
    line_total: Decimal
    """`unit_price` times `quantity`."""

    kind: str | None = None
    """What it is, in customer words ("nightstand")."""

    product: CartProductRef
    display: ProductDisplay | None = Field(default=None, exclude_if=lambda v: v is None)
    """Its text, ready to show in the conversation's language."""

    line_total_display: str | None = Field(default=None, exclude_if=lambda v: v is None)
    """`line_total`, worded in the conversation's language: "7,200 SAR"."""


class CartTotal(BaseModel):
    """What the cart comes to in one currency. Never converted: a cart in two
    currencies has two totals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    price_unit: str
    amount: Decimal
    display: str | None = Field(default=None, exclude_if=lambda v: v is None)
    """`amount`, worded in the conversation's language: "14,350 SAR"."""


class CartChange(BaseModel):
    """One product whose quantity in the cart changed this turn - added
    (from 0), removed (to 0), or more or fewer of it - for the client to make
    the same change in the store's own cart."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    product: CartProductRef
    name: str = Field(min_length=1)
    """Its name in the conversation's language, for a message about it."""

    quantity_before: int = Field(ge=0)
    quantity_after: int = Field(ge=0)


class CartView(BaseModel):
    """The cart as it stands after this turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: tuple[CartLineView, ...] = ()
    item_count: int = Field(default=0, ge=0)
    """How many items, counting quantities: two armchairs and a sofa is 3."""

    totals: tuple[CartTotal, ...] = ()
    """What the lines come to, one total per currency; empty when the cart is."""

    unavailable: int = Field(default=0, ge=0)
    """Products in the cart the catalog no longer returns - left out of the
    lines and the totals, never priced from memory."""

    changes: tuple[CartChange, ...] = ()
    """What this turn changed, oldest line first. Empty when nothing changed,
    and always empty on a plain read of the cart."""


class CartResponse(BaseModel):
    """`GET /v1/cart`: the cart, without a turn of the conversation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    session_revision: int = Field(ge=0)
    """The session's revision; 0 for a conversation that has not started."""

    cart: CartView


class CartQuery(BaseModel):
    """Whose cart: one conversation, in one store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, pattern=SESSION_ID_PATTERN)
    store_id: int = Field(ge=1)
