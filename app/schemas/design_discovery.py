"""What the design-to-discovery bridge produces.

**Application-only.** Like `app/schemas/resolution.py`, and for the same
reason: these contracts carry verified product identity, which is exactly what
`app/schemas/design.py` must never hold. The design specialist reasons about
rooms and is never shown a product, so the two live in separate modules rather
than relying on anyone remembering which fields are safe.

The shape is per need, and deliberately not a flat basket. A room plan asks for
several product types, each with its own priority, and an optimiser has to be
able to tell "this retailer has no rug" from "this retailer has forty rugs".
Merging the pools would destroy exactly that distinction.

Nothing here selects. Every eligible product for every need is present, in
ranked order, and choosing among them is the optimiser's job.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.design import DesignCategoryNeed
from app.schemas.resolution import CandidatePoolResult


class DesignNeedSkipReason(StrEnum):
    """Why a need was not searched for at all."""

    RETAILER_CANNOT_SUPPLY = "retailer_cannot_supply"
    """The active retailer's live catalog holds no such product type.

    Distinct from zero results, and the distinction matters to an optimiser:
    zero results means the search ran and this catalog's rugs did not match,
    while this means there was nothing to search. A plan may legitimately want
    a product type the shop does not carry.

    Not an error. The specialist already drops unsupported needs, so reaching
    this means the catalog moved between the capability lookup and the search -
    an ordinary race, recorded rather than raised.
    """


class DesignNeedCandidates(BaseModel):
    """One design need, and the products that could satisfy it.

    Exactly one of `pool` and `skipped` is set. They are different outcomes
    and not two flavours of empty: a caller that only handles pools cannot
    silently read "nothing was searched" as "nothing matched".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    need_index: int = Field(ge=0)
    """Position in the plan the specialist emitted.

    Carried explicitly because needs are deliberately never deduplicated or
    reordered: two identical-looking needs are two needs, and an index is what
    keeps them distinguishable once multiplicity is designed (CLAUDE.md 27).
    """

    need: DesignCategoryNeed
    pool: CandidatePoolResult | None = None
    skipped: DesignNeedSkipReason | None = None

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> Self:
        if (self.pool is None) == (self.skipped is None):
            raise ValueError("a need was either searched or skipped, not both")
        return self

    @property
    def candidate_count(self) -> int:
        """Verified products available for this need. Zero is a valid answer."""
        return 0 if self.pool is None else len(self.pool.candidates)


class DesignDiscoveryResult(BaseModel):
    """Every need in a room plan, with its verified candidates.

    A partial room is an ordinary outcome, never an error. A required need with
    no candidates does not invalidate the others: whether the room is still
    worth proposing is a decision for the optimiser, which needs to see the gap
    in order to make it (CLAUDE.md 10).

    This is **not** a room, a bundle or a proposal. Nothing here totals a
    price, chooses a product or claims the plan is satisfiable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    needs: tuple[DesignNeedCandidates, ...] = ()
    """In the order the design plan emitted them. Never sorted, never merged."""

    @model_validator(mode="after")
    def _indices_describe_the_original_plan(self) -> Self:
        """Positions are the plan's own, in order and complete.

        A gap would mean a need was dropped without saying so, which is the one
        thing a partial room must never do quietly.
        """
        if tuple(entry.need_index for entry in self.needs) != tuple(
            range(len(self.needs))
        ):
            raise ValueError("need indices must be the plan's own positions, in order")
        return self

    @property
    def searched_count(self) -> int:
        return sum(1 for entry in self.needs if entry.pool is not None)
