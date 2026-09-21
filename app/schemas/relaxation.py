"""Controlled relaxation contracts.

These record what was broadened and where each product became eligible. They
are deterministic policy metadata, not reasoning and not scores.

The original request is preserved untouched alongside the final one, so a later
layer can always tell what the customer asked for from what ZORY widened.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.discovery import DimensionConstraintKind, ProductSearchRequest
from app.schemas.product import EligibleProduct
from app.schemas.query import ConstraintStrength
from app.taxonomy.dimensions import DimensionRole


class RelaxableField(StrEnum):
    """The only fields V1 policy may widen.

    `commerce_category` is absent because it is never relaxable, and
    `commerce_subcategory` because widening it needs an approved alternative
    policy that does not exist yet (CLAUDE.md 13.3).
    """

    PRICE_MIN = "price_min"
    PRICE_MAX = "price_max"
    SEATING_MIN = "seating_min"
    SEATING_MAX = "seating_max"
    DIMENSION = "dimension"
    """One measurement. Which one is carried by `DimensionRole`, not by a member
    per role: the registry already owns that vocabulary, and duplicating it into
    an enum would mean editing two places to add a role."""

    # There is deliberately no PLANAR_DIMENSION. Carpet pairs are not relaxable
    # in V1, and a member for a widening that cannot happen would suggest it can.


class RelaxationChange(BaseModel):
    """One scalar bound moved, and the permission that allowed it."""

    model_config = ConfigDict(frozen=True)

    field: RelaxableField
    from_value: Decimal | int
    to_value: Decimal | int
    strength: ConstraintStrength

    @model_validator(mode="after")
    def _check_not_a_dimension(self) -> Self:
        """A measurement needs its role and its kind; this shape carries neither."""
        if self.field is RelaxableField.DIMENSION:
            raise ValueError("a dimension change must use DimensionRelaxationChange")
        return self


class DimensionRelaxationChange(BaseModel):
    """One measurement widened, with enough to explain it without prose.

    `kind` is the customer's ORIGINAL kind and never changes. A widened TARGET
    executes as a band, but it does not become a RANGE: they asked to sit near a
    figure, and the band is how that is executed, not what it meant. Keeping the
    original kind here is what lets a later layer say "slightly wider than the
    220 cm you wanted" rather than inventing an interval they never gave.

    `applied_*` are the bounds actually executed at this stage; `original_*` are
    what the customer said. Both are present so nothing has to be recomputed -
    and recomputing is exactly where a compounding bug would hide.
    """

    model_config = ConfigDict(frozen=True)

    field: Literal[RelaxableField.DIMENSION] = RelaxableField.DIMENSION
    role: DimensionRole
    kind: DimensionConstraintKind
    """The kind the customer expressed. Never rewritten by relaxation."""

    strength: ConstraintStrength
    stage: int = Field(ge=1)
    """Which widening step this was. 1 is the mildest; there is no stage 0."""

    original_min_cm: Decimal | None = None
    original_max_cm: Decimal | None = None
    original_target_cm: Decimal | None = None
    applied_min_cm: Decimal | None = None
    applied_max_cm: Decimal | None = None

    @model_validator(mode="after")
    def _check_shape_and_direction(self) -> Self:
        """The recorded shape must match the kind, and every bound must move outward."""
        required = {
            DimensionConstraintKind.MIN: ("original_min_cm", "applied_min_cm"),
            DimensionConstraintKind.MAX: ("original_max_cm", "applied_max_cm"),
            DimensionConstraintKind.RANGE: (
                "original_min_cm", "original_max_cm", "applied_min_cm", "applied_max_cm",
            ),
            DimensionConstraintKind.TARGET: (
                "original_target_cm", "applied_min_cm", "applied_max_cm",
            ),
        }[self.kind]
        for name in (
            "original_min_cm", "original_max_cm", "original_target_cm",
            "applied_min_cm", "applied_max_cm",
        ):
            value = getattr(self, name)
            if name in required and value is None:
                raise ValueError(f"a {self.kind} dimension change needs {name}")
            if name not in required and value is not None:
                raise ValueError(f"a {self.kind} dimension change must not set {name}")

        ceiling = (self.original_max_cm, self.applied_max_cm)
        if None not in ceiling and self.applied_max_cm < self.original_max_cm:  # type: ignore[operator]
            raise ValueError("a ceiling may only move outward")
        floor = (self.original_min_cm, self.applied_min_cm)
        if None not in floor and self.applied_min_cm > self.original_min_cm:  # type: ignore[operator]
            raise ValueError("a floor may only move outward")
        if self.kind is DimensionConstraintKind.TARGET:
            assert self.original_target_cm is not None
            assert self.applied_min_cm is not None and self.applied_max_cm is not None
            below = self.original_target_cm - self.applied_min_cm
            above = self.applied_max_cm - self.original_target_cm
            if below != above:
                raise ValueError("a target band must stay symmetric about the target")
            if below < 0:
                raise ValueError("a target band may only widen")
        if self.applied_min_cm is not None and self.applied_min_cm <= 0:
            raise ValueError("a widened floor must stay above zero")
        return self


AppliedRelaxation = RelaxationChange | DimensionRelaxationChange
"""What one attempt recorded. Scalar bounds and measurements record differently
because they carry different facts; a shared shape would blur both."""


class RelaxationAttempt(BaseModel):
    """One executed search."""

    model_config = ConfigDict(frozen=True)

    depth: int = Field(ge=0)
    """0 is the customer's exact request; 1 and above are widened."""

    request: ProductSearchRequest
    changes: tuple[AppliedRelaxation, ...]
    eligible_count: int = Field(ge=0)
    """Every product this attempt made eligible, not a page of them.

    There is deliberately no `truncated` flag: the attempt reads the complete
    eligible pool, so there is nothing a bound could have cut off.
    """


class RelaxedCandidate(BaseModel):
    """A product, and the attempt at which it first became eligible.

    `relaxation_depth` is provenance, not a score. Depth 0 satisfies the
    original request; depth 1 or more required a widening. Nothing in V1 ranks
    on it, and it must never be presented as relevance.
    """

    model_config = ConfigDict(frozen=True)

    product: EligibleProduct
    """Ranking's minimum, not a full row. Customer-visible values are re-read
    from PostgreSQL after presentation selection, so carrying them here would
    duplicate the row the hydrator fetches."""

    relaxation_depth: int = Field(ge=0)


class StopReason(StrEnum):
    EXACT_SUFFICIENT = "exact_sufficient"
    """The customer's own request already met the target. Nothing was widened."""

    TARGET_REACHED = "target_reached"
    """A widened attempt brought the unique pool up to the target."""

    NO_RELAXABLE_CONSTRAINTS = "no_relaxable_constraints"
    """Below target, but every constraint was locked, absent or not
    automatically relaxable. A valid outcome, not a failure."""

    POLICY_EXHAUSTED = "policy_exhausted"
    """Every permitted widening was tried and the target was still not met.
    Also a valid outcome: the catalog simply does not hold enough."""


class ControlledSearchResult(BaseModel):
    """The outcome of an exact search plus any permitted widening."""

    model_config = ConfigDict(frozen=True)

    original_request: ProductSearchRequest
    """Exactly what the customer asked for. Never mutated."""

    final_request: ProductSearchRequest
    """The last request executed. Equals the original when nothing was widened."""

    candidates: tuple[RelaxedCandidate, ...]
    """Unique products in first-seen order. Not ranked."""

    exact_candidate_count: int = Field(ge=0)
    """How many products the unwidened request made eligible.

    The true count, not a page: the exact attempt reads the complete eligible
    pool like every other attempt.
    """

    target_candidates: int = Field(ge=1)
    target_reached: bool
    stop_reason: StopReason
    attempts: tuple[RelaxationAttempt, ...]

    @property
    def relaxation_attempt_count(self) -> int:
        """Widened attempts, excluding the exact search."""
        return max(0, len(self.attempts) - 1)

    @property
    def was_relaxed(self) -> bool:
        return self.relaxation_attempt_count > 0
