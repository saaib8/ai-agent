"""The boundary between the two reasoning agents.

Customer/Commerce Agent -> InteriorDesignRequest -> Interior Design Agent
Interior Design Agent   -> InteriorDesignResult  -> Customer/Commerce Agent

Typed structures, not a conversation (CLAUDE.md 3.5). Deliberately small: what
a room plan looks like, how products are chosen for it and how a layout is
scored all belong to M12, which will define them against real requirements.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.discovery import PriceConstraint
from app.schemas.query import SemanticPreference
from app.schemas.retailer import RetailerCatalogCapabilities
from app.taxonomy.registry import CommerceTaxonomy


class InteriorDesignRequest(BaseModel):
    """Everything the design specialist needs, and nothing it should invent.

    `catalog_capabilities` is required rather than optional: a plan built
    around product types the retailer cannot supply is worse than no plan, so
    the unsafe case must not be the default (CLAUDE.md 9).

    The customer's own requirements are passed through as they were captured.
    The design agent does not re-derive a room type or a style the customer
    already stated in plain words.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_type: str | None = None
    budget: PriceConstraint | None = None
    design_preferences: tuple[SemanticPreference, ...] = ()
    catalog_capabilities: RetailerCatalogCapabilities


class DesignPriority(StrEnum):
    """What a room can do without, when a budget forces a choice.

    Ordered by what may be dropped first, so budget work degrades optional
    items before core room requirements (CLAUDE.md 10).
    """

    REQUIRED = "required"
    RECOMMENDED = "recommended"
    OPTIONAL = "optional"


class DesignCategoryNeed(BaseModel):
    """One product type a room calls for, and how badly."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)
    priority: DesignPriority


class InteriorDesignResult(BaseModel):
    """What the design specialist concluded: which product types, at what priority.

    There is deliberately no `design_preferences` here. The customer's
    preferences already have one home, on the room project, and echoing them
    back from an agent that owns no state would create a second copy and a
    path by which a recommendation could quietly displace what the customer
    said. When M12 has genuinely agent-derived guidance, it gets a field whose
    name says so.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    needs: tuple[DesignCategoryNeed, ...] = ()

    def validate_against(self, taxonomy: CommerceTaxonomy) -> None:
        """Reject any need the approved vocabulary does not contain.

        Checked here rather than at construction, matching how
        :class:`~app.schemas.discovery.ProductSearchRequest` leaves its
        category to the discovery service: schemas in this service do not load
        the registry.
        """
        for need in self.needs:
            if need.commerce_subcategory is None:
                taxonomy.subcategories(need.commerce_category)  # raises if unapproved
            else:
                taxonomy.validate_pair(
                    need.commerce_category, need.commerce_subcategory
                )
