"""How a customer points at a product, without naming one.

The closed set of ways a turn may refer to something already on screen. Every
member describes *how to find* a product; none can carry one. That is the
whole authority argument: a model that could emit `165645` could emit
`165646`, and no membership check afterwards distinguishes a real reference
from a plausible one.

Its own module because two contracts need it - a product interaction and a
relative price - and neither should have to import the other.

Resolution is deterministic and belongs elsewhere. A selector that matches
several products, or none, is a question for the customer, never a guess:
"the beige one" over two beige products clarifies, and a tied cheapest
clarifies rather than quietly taking the first.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.taxonomy.attributes import AttributeFamily


class ExtremumDirection(StrEnum):
    LOWEST = "lowest"
    HIGHEST = "highest"


class PresentedOrdinal(BaseModel):
    """"the second one" - a position in what the customer is looking at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["presented_ordinal"] = "presented_ordinal"
    position: int = Field(ge=1)


class ComparedOrdinal(BaseModel):
    """"the second one" - a column in the comparison they are looking at.

    A comparison creates a numbering of its own. Sofas three and five of a list
    of five are columns one and two, and a customer who has been reading a
    two-column table counts in that table. Without this, "the second one" could
    only mean the second of the underlying list - which is how a customer who
    said "I meant the second one from the comparison" was given a product they
    had not compared (M15 1).

    Its positions are the comparison's, never the result list's, so the two can
    never be confused by whoever resolves them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["compared_ordinal"] = "compared_ordinal"
    position: int = Field(ge=1)


class FocusedProduct(BaseModel):
    """"it", "that one", "this sofa"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["focused_product"] = "focused_product"


class SoleSelectedProduct(BaseModel):
    """"the one I liked" - resolvable only when exactly one is selected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["sole_selected_product"] = "sole_selected_product"


class PresentedAttributeMatch(BaseModel):
    """"the beige one" - resolved against freshly hydrated presented products."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["presented_attribute_match"] = "presented_attribute_match"
    family: AttributeFamily
    value: str = Field(min_length=1)


class PresentedExtremum(BaseModel):
    """"the cheaper one" - an extreme of a verified fact, not a judgement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["presented_extremum"] = "presented_extremum"
    field: Literal["price"] = "price"
    direction: ExtremumDirection


def _must_declare_its_kind(value: Any) -> Any:
    """A raw selector states which variant it is, or it is not one.

    Every member defaults its own `kind`, which is what lets application code
    write `FocusedProduct()`. Without this check that same default would let an
    *untrusted* `{}` acquire a selector identity it never claimed - and
    `FocusedProduct` resolves to whatever the customer is currently looking at,
    so an empty object would silently become a real product.

    Only mappings are inspected. An already-constructed instance arrived
    through the type system and has a tag by construction.
    """
    if isinstance(value, dict) and "kind" not in value:
        raise ValueError("a product reference must declare its kind")
    return value


ProductReferenceSelector = Annotated[
    PresentedOrdinal
    | ComparedOrdinal
    | FocusedProduct
    | SoleSelectedProduct
    | PresentedAttributeMatch
    | PresentedExtremum,
    BeforeValidator(_must_declare_its_kind),
]
"""A closed, tagged set. Resolution to an actual product is the application's.

Deliberately **not** `Field(discriminator="kind")`. Pydantic renders a
discriminated union as JSON Schema `oneOf`, which the provider's strict
structured-output schema rejects outright (`"oneOf is not permitted"`), so the
decision contract could not be sent at all. The same five members render as
`anyOf` instead, which the provider accepts.

The tag still does the selecting: each variant carries a distinct `kind`
Literal, so an object tagged `presented_ordinal` cannot satisfy another
branch. `BeforeValidator` restores the one thing the discriminator also gave -
that a raw mapping must *carry* the tag - because a closed tagged union should
enforce that itself rather than depend on a provider's schema transformer
happening to mark the field required.

What is lost is only error *shape*: a malformed selector reports per-member
errors instead of one discriminator error.
"""
