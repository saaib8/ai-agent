"""Which measurements M8 is permitted to widen.

This is **search policy**, not product semantics, and the distinction is the
reason it lives here rather than in `dimension_semantics_v1.yaml`. That registry
answers a question about the catalog - which stored axis a customer's physical
role can be trusted to mean. This answers a question about recovery behaviour -
whether widening that measurement produces a useful result instead of a cliff.
The two change for different reasons and on different evidence.

The V1 allowlist was derived from the reviewed store-50 dataset (1,036 active
products). It is deliberately narrow: measured on that catalog, percentage
widening grows the candidate pool smoothly only for the two pairs below. Every
other subcategory and role either has too few products to support a claim, or
has a distribution so concentrated that one step swallows it.

Nothing here is validated for another retailer, and the policy carries no
`store_id`: multi-retailer dimension conventions are a separate, deferred
problem (CLAUDE.md 15.2).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.taxonomy.dimensions import DimensionRole


class DimensionRelaxationPolicy(BaseModel):
    """The (subcategory, role) pairs a planner may widen.

    Immutable and injected, so a test can substitute a different allowlist
    without reaching into the planner, and so no `if subcategory == "sofa"`
    appears anywhere in the relaxation logic.
    """

    model_config = ConfigDict(frozen=True)

    relaxable: frozenset[tuple[str, DimensionRole]]

    def is_relaxable(self, subcategory: str | None, role: DimensionRole) -> bool:
        """Fails closed: an absent subcategory, or any pair not listed, is a no.

        A request with no subcategory cannot be judged - the same role means
        different things for different product types - so it is never widened.
        """
        if subcategory is None:
            return False
        return (subcategory, role) in self.relaxable


V1_DIMENSION_RELAXATION_POLICY = DimensionRelaxationPolicy(
    relaxable=frozenset(
        {
            ("sofa", DimensionRole.OVERALL_WIDTH),
            ("service-table", DimensionRole.OVERALL_WIDTH),
        }
    )
)
"""The approved V1 allowlist. Everything absent from it is non-relaxable."""
