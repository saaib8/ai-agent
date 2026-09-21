"""How a customer names a piece of the room they can see.

Model-facing, and deliberately as small as `ProductReferenceSelector`. The
model says which card it means; the application works out which lines that is.
No product id, no line id, no need id, no revision - a selector that carried
identity would be the model choosing a target rather than describing one
(CLAUDE.md 20.2).

Both members carry a `kind`, for the same two reasons the product selectors do:
the provider's strict schema rejects `oneOf`, and without a tag an empty object
would validate as whichever member happened to have all-optional fields.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


class BundleReferenceKind(StrEnum):
    ORDINAL = "bundle_ordinal"
    CATEGORY = "bundle_category"


class BundleItemOrdinal(BaseModel):
    """"The second one" - a position among the visible cards.

    Counted over what is shown, so a room of four lines rendered as three cards
    has a third and no fourth.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[BundleReferenceKind.ORDINAL] = BundleReferenceKind.ORDINAL
    ordinal: int = Field(ge=1)


class BundleCategoryMatch(BaseModel):
    """"The lamp" - the piece of a given kind, when there is only one.

    Values are approved taxonomy, validated by the resolver against the
    registry: a type the vocabulary does not contain names nothing, and is
    refused rather than matched loosely.

    With no subcategory it means any card in that category, which is how "the
    lamp" works when the customer does not know the shop's word for it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[BundleReferenceKind.CATEGORY] = BundleReferenceKind.CATEGORY
    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)


def _must_declare_its_kind(value: Any) -> Any:
    if isinstance(value, dict) and "kind" not in value:
        raise ValueError("a bundle reference must declare its kind")
    return value


BundleReferenceSelector = Annotated[
    BundleItemOrdinal | BundleCategoryMatch,
    BeforeValidator(_must_declare_its_kind),
]
"""One way of naming a visible card.

There is deliberately no focused-bundle member: nothing tracks a focused card,
and a selector the application cannot resolve should not be offerable.
"""


class DesignNeedCategoryMatch(BaseModel):
    """A furnishing role in the plan, named by what kind of thing it is.

    Distinct from a card selector because a role need not be filled: a room may
    want a floor lamp it never found one for, and "take the lamp out of the
    plan" has to reach a need with no product behind it.

    Values are approved taxonomy, validated by the resolver.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)
