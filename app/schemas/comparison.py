"""Comparing products on verified facts.

The application computes the difference; the model explains what it means. A
model that computed the diff itself would be asserting facts, and "these two
are the same width" is exactly the kind of claim that sounds harmless and is
checkable.

The contract's job is to make one mistake unrepresentable: **unknown is not
equal**. Two products that both record no seating capacity are not "the same";
nobody has established anything about either. A boolean `differs` could not
tell those cases apart, so the status is a third value and the row validates
its own consistency.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.grounding import GroundedProduct

MIN_COMPARED_PRODUCTS = 2


class ComparisonStatus(StrEnum):
    SAME = "same"
    DIFFERENT = "different"
    UNKNOWN = "unknown"
    """At least one product records nothing for this field."""


class ComparisonField(StrEnum):
    """Authoritative fields only.

    Measurements are named by customer-facing role, never by stored column: a
    sofa's along-wall span lives in `length` and a bed's planar axes are not
    reliable at all, so comparing columns across product types would state a
    difference that does not exist (CLAUDE.md 15.1).

    `material` is absent because the catalog cannot yet answer it.
    """

    PRICE = "price"
    COMMERCE_SUBCATEGORY = "commerce_subcategory"
    SEATING_CAPACITY = "seating_capacity"
    MAIN_COLOR = "main_color"
    STYLES = "styles"
    LENGTH = "length"
    OVERALL_WIDTH = "overall_width"
    DEPTH = "depth"
    HEIGHT = "height"


class ComparisonCell(BaseModel):
    """One product's value for one field, or an honest absence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    known: bool
    value: str | None = None

    @model_validator(mode="after")
    def _known_means_a_value(self) -> Self:
        if self.known and self.value is None:
            raise ValueError("a known cell needs a value")
        if not self.known and self.value is not None:
            raise ValueError("an unknown cell cannot carry a value")
        return self


class ComparisonRow(BaseModel):
    """One field across the compared products, with a self-checked status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: ComparisonField
    cells: tuple[ComparisonCell, ...]
    status: ComparisonStatus

    @model_validator(mode="after")
    def _status_follows_from_the_cells(self) -> Self:
        """Unknown wins, then equality. Computed here so it cannot be claimed."""
        if len(self.cells) < MIN_COMPARED_PRODUCTS:
            raise ValueError("a comparison row needs a cell per compared product")
        if any(not cell.known for cell in self.cells):
            expected = ComparisonStatus.UNKNOWN
        elif len({cell.value for cell in self.cells}) == 1:
            expected = ComparisonStatus.SAME
        else:
            expected = ComparisonStatus.DIFFERENT
        if self.status is not expected:
            raise ValueError(f"status must be {expected} for these cells")
        return self


class ProductComparisonResult(BaseModel):
    """A typed factual diff. Input order is preserved, because "the first two"
    is how the customer referred to them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    products: tuple[GroundedProduct, ...]
    rows: tuple[ComparisonRow, ...] = ()

    @model_validator(mode="after")
    def _rows_line_up_with_the_products(self) -> Self:
        if len(self.products) < MIN_COMPARED_PRODUCTS:
            raise ValueError(
                f"a comparison needs at least {MIN_COMPARED_PRODUCTS} products"
            )
        for row in self.rows:
            if len(row.cells) != len(self.products):
                raise ValueError("every row needs one cell per compared product")
        fields = [row.field for row in self.rows]
        if len(set(fields)) != len(fields):
            raise ValueError("a comparison field appears at most once")
        return self
