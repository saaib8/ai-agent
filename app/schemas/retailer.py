"""Retailer/store scope contracts.

``RetailerContext`` is the single carrier of catalog scope. It is resolved once
per request by application code and is immutable, so no downstream component —
least of all a model-driven one — can widen or redirect it (CLAUDE.md 8).
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.exceptions import UnknownCommerceCategoryError
from app.taxonomy.registry import CommerceTaxonomy


class StoreRow(BaseModel):
    """Internal projection of a ``core_store`` row."""

    model_config = ConfigDict(frozen=True)

    id: int
    uuid: UUID
    name_english: str
    active_status: bool


class RetailerContext(BaseModel):
    """Server-controlled catalog scope for one request.

    Intentionally minimal. Currency and supported categories are derived from
    live catalog data by the capability service and are added when that service
    exists, rather than declared here as fields nothing can populate.
    """

    model_config = ConfigDict(frozen=True)

    store_id: int = Field(gt=0)


class RetailerCatalogCapability(BaseModel):
    """One product type the active retailer can actually supply.

    A category with no subcategory asserts the category and **nothing about its
    children**: "this retailer supports seating, at unstated granularity". It
    does not mean every seating subcategory is available, which is why a
    category cannot be described both ways at once (see
    :class:`RetailerCatalogCapabilities`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)


class RetailerCatalogCapabilities(BaseModel):
    """What the active retailer can supply, for whole-room planning.

    Application-supplied context, parallel to :class:`RetailerContext`: derived
    from live catalog data by a deterministic service, never proposed by an
    agent and never part of agent state. It carries capability, not inventory -
    no products, no prices, no counts.

    Taxonomy validity is checked by :meth:`validate_against` rather than at
    construction, matching how :class:`~app.schemas.discovery.ProductSearchRequest`
    leaves its category to be validated by the discovery service. Schemas in
    this service do not load the registry; the layer that holds it validates
    against it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    capabilities: tuple[RetailerCatalogCapability, ...] = ()

    @model_validator(mode="after")
    def _check_structure(self) -> Self:
        seen: set[tuple[str, str | None]] = set()
        broad: set[str] = set()
        narrow: set[str] = set()
        for entry in self.capabilities:
            key = (entry.commerce_category, entry.commerce_subcategory)
            if key in seen:
                raise ValueError("duplicate catalog capability entry")
            seen.add(key)
            if entry.commerce_subcategory is None:
                broad.add(entry.commerce_category)
            else:
                narrow.add(entry.commerce_category)
        # Describing one category both ways is a contradiction, not a union:
        # the broad form asserts nothing about children, so pairing it with
        # children leaves a reader unable to say what is supported.
        both = broad & narrow
        if both:
            raise ValueError(
                "a category stated without a subcategory cannot also appear "
                f"with subcategories: {sorted(both)}"
            )
        return self

    def validate_against(self, taxonomy: CommerceTaxonomy) -> None:
        """Reject any entry the approved vocabulary does not contain."""
        for entry in self.capabilities:
            if entry.commerce_subcategory is None:
                if not taxonomy.is_category(entry.commerce_category):
                    raise UnknownCommerceCategoryError(category=entry.commerce_category)
            else:
                taxonomy.validate_pair(
                    entry.commerce_category, entry.commerce_subcategory
                )

    def supports(self, category: str, subcategory: str | None = None) -> bool:
        """Whether a planner may rely on this product type.

        A subcategory is supported only when named. A category-only entry
        asserts the category alone, so asking about one of its children
        answers False rather than guessing.
        """
        for entry in self.capabilities:
            if entry.commerce_category != category:
                continue
            if entry.commerce_subcategory == subcategory:
                return True
        return False
