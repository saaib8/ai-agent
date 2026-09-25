"""What the active retailer's shelf actually holds - the agent's inventory sense.

A salesperson answers well because they *know the shop*: not every SKU, but the
shape of it - "sofas seat up to five, sets go to seven, everything's between one
and eight thousand riyals, mostly in neutrals". :class:`CatalogOverview` is that
knowledge, made structural.

It is the richer sibling of :class:`~app.schemas.retailer.RetailerCatalogCapabilities`.
Capabilities answer *does this store stock this type*; the overview answers the
next questions a salesperson reasons with before they decide what to offer:

* can a *single* piece meet this need, or must it be composed of several?
  (the seat ceiling per seating type)
* is what they asked for even in this store's price world? (the range)
* if we can't match the colour, what *do* we have? (the palette)

**This is capability, never inventory** - the same line
:class:`RetailerCatalogCapabilities` draws (CLAUDE.md 9). It carries counts,
ranges and the set of colours a type comes in. It carries no product, no id and
no individual product's price: a *range* like 990-8,000 is a fact about the
shelf, not a claim about any one sofa. Everything a customer is finally told
still comes from a real search through the guarded pipeline; the overview only
tells the agent which move is worth making.

Derived by a deterministic service from live, store-scoped catalog data. The
model reads it; it never builds it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SeatingSpread(BaseModel):
    """The range of confirmed seat counts within one seating type.

    Present only where the catalog actually records seat counts. In store 50
    the sofa rows carry them but every chair and single-seater is ``NULL``, so a
    type can be stocked and still have no spread here - which is itself the
    signal that its capacity is unverified rather than any number
    (CLAUDE.md 6.2, 31).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    known_count: int = Field(ge=1)
    """How many active products of this type have a confirmed seat count.

    At least one, because a spread with nothing behind it should be absent, not
    zero. It is not the type's total: the difference between ``known_count`` and
    the shelf's ``active_count`` is how many pieces have an unverified capacity.
    """

    minimum: int = Field(ge=1)
    maximum: int = Field(ge=1)
    """The smallest and largest confirmed seat count. Equal when every counted
    piece seats the same number. ``maximum`` is the seat ceiling a *single*
    piece of this type can reach - the figure that decides whether a request for
    more seats needs a combination rather than one product."""

    @model_validator(mode="after")
    def _min_within_max(self) -> Self:
        if self.minimum > self.maximum:
            raise ValueError("minimum seat count cannot exceed the maximum")
        return self


class SubcategoryShelf(BaseModel):
    """One product type as it sits on this store's shelf.

    Keyed by the commerce pair, so a reader always knows which family it belongs
    to. A ``None`` subcategory is a category-level row - products classified to a
    category but not to a child - and asserts nothing about the children, the
    same rule :class:`~app.schemas.retailer.RetailerCatalogCapability` follows.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)

    active_count: int = Field(ge=1)
    """How many active products of this type the store holds. At least one by
    definition - a shelf exists because the catalog put something on it."""

    price_minimum: Decimal
    price_maximum: Decimal
    """The cheapest and dearest active product of this type. Always present:
    ``price_amount`` is non-null in the catalog. A range, never a product's
    price - it answers "is a 5,000 budget even in play for sofas here", not
    "this sofa costs X"."""

    seating: SeatingSpread | None = None
    """Confirmed seat counts for this type, or ``None`` where the catalog
    records none. Only meaningful for seating; a table shelf leaves it ``None``."""

    implied_seats: int | None = None
    """The reviewed seat count for one piece of this type when the catalog
    records none - one for a chair or a single-seater sofa. Reviewed domain data
    from the seating registry, kept distinct from :attr:`seating` on purpose: a
    confirmed count and a reviewed default are different kinds of knowledge, and
    a reply must never present the second as the first (CLAUDE.md 6.2, 31). It
    is what lets a chair count as a seat in a combination even though its
    ``seating_capacity`` column is blank."""

    colours: tuple[str, ...] = ()
    """The distinct ``main_color`` values this type comes in, in stored form.

    Deduplicated and sorted, so the palette is stable between reads. Empty when
    no product of the type has a recorded colour - said as "unknown", never
    guessed. This is what lets the agent answer "we don't have it in that
    colour, but here's what we do have" instead of a blank search.
    """

    @model_validator(mode="after")
    def _price_range_is_ordered(self) -> Self:
        if self.price_minimum > self.price_maximum:
            raise ValueError("price minimum cannot exceed the maximum")
        if self.seating is not None and self.seating.known_count > self.active_count:
            raise ValueError("more pieces have a seat count than exist on the shelf")
        return self

    @property
    def max_seats(self) -> int | None:
        """The seat ceiling one piece of this type reaches, or ``None`` when
        neither a confirmed count nor a reviewed default is known.

        A confirmed count wins over the reviewed default: a sofa uses its real
        maximum, a chair falls back to its implied one. The single number the
        combination decision turns on - and the reason a chair can now fill a
        seat in a bundle where before it counted for nothing.
        """
        if self.seating is not None:
            return self.seating.maximum
        return self.implied_seats


class CatalogOverview(BaseModel):
    """Everything a store's shelf tells the agent, before it decides a move.

    Store-scoped by construction: it is built for one ``store_id`` from that
    store's live catalog, and carries the id so a stale overview cannot be read
    against the wrong shop. The helpers are the questions a salesperson asks the
    shop in their head - what do we carry in this family, how high does a single
    piece go, is this even our price world - answered against real data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    store_id: int = Field(gt=0)

    currency: str | None = None
    """The store's currency, when its catalog names exactly one cleanly.

    ``None`` when a store's ``price_unit`` column is mixed or unreliable, which
    outside store 50 it often is (Arabic nouns, numbers, "test"). Absent rather
    than guessed: a wrong currency stated as fact is worse than none
    (CLAUDE.md 21)."""

    shelves: tuple[SubcategoryShelf, ...] = ()

    @model_validator(mode="after")
    def _shelves_are_distinct(self) -> Self:
        seen: set[tuple[str, str | None]] = set()
        for shelf in self.shelves:
            key = (shelf.commerce_category, shelf.commerce_subcategory)
            if key in seen:
                raise ValueError("duplicate shelf for one commerce pair")
            seen.add(key)
        return self

    def stocks(self, category: str, subcategory: str | None = None) -> bool:
        """Whether the store holds this exact type. A subcategory is stocked only
        when named; a category-level query is not answered by its children."""
        return any(
            shelf.commerce_category == category and shelf.commerce_subcategory == subcategory
            for shelf in self.shelves
        )

    def shelves_in(self, category: str) -> tuple[SubcategoryShelf, ...]:
        """Every shelf in a commerce family, for offering the nearest type when
        the exact one is absent ("no chandeliers - here's what lighting we do
        carry")."""
        return tuple(shelf for shelf in self.shelves if shelf.commerce_category == category)

    def max_seats_in(self, category: str) -> int | None:
        """The most seats any *single* piece in a family reaches, across its
        types, or ``None`` when the family records no seat counts.

        The figure the combination move turns on: when a customer needs more
        seats than this, no one product can do it and the agent must compose
        several - it is never a reason to drop the requirement.
        """
        ceilings = [
            shelf.max_seats for shelf in self.shelves_in(category) if shelf.max_seats is not None
        ]
        return max(ceilings) if ceilings else None
