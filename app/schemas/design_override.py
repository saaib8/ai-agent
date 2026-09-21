"""Per-need search adjustments for one refinement turn.

**Application-only and transient.** A customer asking for a cheaper sofa has
not changed what the room needs - the role, its quantity and its seat count are
all the same - so none of this belongs on `DesignCategoryNeed`, which describes
the design. What changes is how *this one search* should run.

Nothing here is stored whole. A rejection survives the turn by joining the
durable need's own list; a price bound and a staged wording do not survive at
all, because they described a search rather than a room.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.discovery import MAX_EXCLUDED_PRODUCT_IDS, PriceConstraint
from app.schemas.refinement import SemanticIntentRefinement


class DesignNeedSearchOverride(BaseModel):
    """How one need's search differs from what the plan alone would produce."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    need_id: int = Field(ge=1)

    exclude_product_ids: tuple[int, ...] = ()
    """Products this role must not return, the rejected list plus whatever the
    customer is replacing right now. Applied at every widening: relaxation may
    loosen a bound, never reinstate something they turned down."""

    price: PriceConstraint | None = None
    """A bound derived from the product being replaced, never from the model.
    "Cheaper" is an exclusive ceiling at what the current one costs."""

    semantic_intent: SemanticIntentRefinement | None = None
    """New wording for this role, staged. `SET` replaces the persisted phrase
    for this search; `CLEAR` runs it with none."""

    @model_validator(mode="after")
    def _exclusions_are_bounded_and_distinct(self) -> Self:
        if len(self.exclude_product_ids) != len(set(self.exclude_product_ids)):
            raise ValueError("a product is excluded at most once")
        if len(self.exclude_product_ids) > MAX_EXCLUDED_PRODUCT_IDS:
            raise ValueError(
                f"a need may exclude at most {MAX_EXCLUDED_PRODUCT_IDS} products"
            )
        return self
