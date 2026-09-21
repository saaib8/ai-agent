"""The relaxation policy: which constraints may widen, by how much, in what order.

Pure and deterministic. No database, no repository, no LLM, no customer
language - it reads only the structured meaning M7 already produced.

Two rules carry most of the safety:

* Permission comes from :class:`ConstraintSemantics` alone. A `LOCKED` bound
  never moves, and a bound whose strength is missing is treated as locked -
  absent metadata is never read as consent.
* Every widened value is computed from the customer's ORIGINAL figure, never
  from the previous step's. Steps accumulate, percentages do not compound:
  5000 widens to 5500 then 6000, never 6600.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.config import RelaxationSettings
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PriceConstraint,
    ProductSearchRequest,
    SeatingCapacityConstraint,
)
from app.schemas.query import ConstraintSemantics, ConstraintStrength, ResolvedSearch
from app.schemas.relaxation import (
    AppliedRelaxation,
    DimensionRelaxationChange,
    RelaxableField,
    RelaxationChange,
)
from app.services.dimension_policy import (
    V1_DIMENSION_RELAXATION_POLICY,
    DimensionRelaxationPolicy,
)

# Widening is offered to explicitly approximate constraints before merely
# preferred ones: the customer who said "around 5000" invited the change more
# clearly than the one who said "I'd prefer under 5000".
_RELAXATION_ORDER: tuple[ConstraintStrength, ...] = (
    ConstraintStrength.APPROXIMATE,
    ConstraintStrength.PREFERRED,
)

_MIN_SEATS = 1
_NO_PRICE = Decimal(0)

# Every widened measurement is quantised to this, which is exactly the scale the
# repository casts to in SQL (``NUMERIC(12,4)``). Matching it means the value we
# compute is the value PostgreSQL compares, with no rounding hidden at the
# boundary. Decimal throughout: binary floats would make 220 * 1.05 a value that
# depends on the platform.
_CENTIMETRE_QUANTUM = Decimal("0.0001")


class PlannedRelaxation(BaseModel):
    """One widened request, with the changes that produced it."""

    model_config = ConfigDict(frozen=True)

    request: ProductSearchRequest
    changes: tuple[AppliedRelaxation, ...]


class RelaxationPlanner:
    """Turns a resolved search into the bounded sequence of widened requests.

    The sequence is finite by construction: one step per price fraction per
    strength, plus one seating step per strength. With two fractions and two
    strengths that is at most six, whatever the input.
    """

    def __init__(
        self,
        settings: RelaxationSettings,
        dimension_policy: DimensionRelaxationPolicy = V1_DIMENSION_RELAXATION_POLICY,
    ) -> None:
        self._settings = settings
        self._dimension_policy = dimension_policy

    def plan(self, resolved: ResolvedSearch) -> tuple[PlannedRelaxation, ...]:
        request, semantics = resolved.request, resolved.semantics
        original_price = request.price
        original_capacity = request.seating_capacity

        # Running values: later steps keep earlier widenings.
        price_min = original_price.min_amount if original_price else None
        price_max = original_price.max_amount if original_price else None
        seats_min = original_capacity.min_capacity if original_capacity else None
        seats_max = original_capacity.max_capacity if original_capacity else None
        dimensions = request.dimensions

        planned: list[PlannedRelaxation] = []
        for strength in _RELAXATION_ORDER:
            for fraction in self._settings.price_steps:
                changes: list[AppliedRelaxation] = []
                if self._may_relax(semantics.price_min, strength):
                    assert original_price is not None
                    widened = self._widen_price_floor(original_price.min_amount, fraction)
                    if widened is not None and widened != price_min:
                        changes.append(
                            RelaxationChange(
                                field=RelaxableField.PRICE_MIN,
                                from_value=price_min if price_min is not None else _NO_PRICE,
                                to_value=widened,
                                strength=strength,
                            )
                        )
                        price_min = widened
                if self._may_relax(semantics.price_max, strength):
                    assert original_price is not None
                    widened = self._widen_price_ceiling(original_price.max_amount, fraction)
                    if widened is not None and widened != price_max:
                        changes.append(
                            RelaxationChange(
                                field=RelaxableField.PRICE_MAX,
                                from_value=price_max if price_max is not None else _NO_PRICE,
                                to_value=widened,
                                strength=strength,
                            )
                        )
                        price_max = widened
                if changes:
                    planned.append(
                        self._build(
                            request, price_min, price_max, seats_min, seats_max,
                            dimensions, changes,
                        )
                    )

            changes = []
            delta = self._settings.seating_delta
            if self._may_relax(semantics.seating_min, strength):
                assert original_capacity is not None
                widened_seats = self._widen_seat_floor(original_capacity.min_capacity, delta)
                if widened_seats is not None and widened_seats != seats_min:
                    changes.append(
                        RelaxationChange(
                            field=RelaxableField.SEATING_MIN,
                            from_value=seats_min if seats_min is not None else _MIN_SEATS,
                            to_value=widened_seats,
                            strength=strength,
                        )
                    )
                    seats_min = widened_seats
            if self._may_relax(semantics.seating_max, strength):
                assert original_capacity is not None
                widened_seats = self._widen_seat_ceiling(original_capacity.max_capacity, delta)
                if widened_seats is not None and widened_seats != seats_max:
                    changes.append(
                        RelaxationChange(
                            field=RelaxableField.SEATING_MAX,
                            from_value=seats_max if seats_max is not None else _MIN_SEATS,
                            to_value=widened_seats,
                            strength=strength,
                        )
                    )
                    seats_max = widened_seats
            if changes:
                planned.append(
                    self._build(
                        request, price_min, price_max, seats_min, seats_max,
                        dimensions, changes,
                    )
                )

            # Measurements come last within a strength tier. Price and seating
            # keep the order and position they have had since M8; appending
            # rather than interleaving means no existing sequence changes.
            for stage, fraction in enumerate(self._settings.dimension_steps, start=1):
                dimensions, changes = self._widen_dimensions(
                    request, semantics, dimensions, strength, fraction, stage
                )
                if changes:
                    planned.append(
                        self._build(
                            request, price_min, price_max, seats_min, seats_max,
                            dimensions, changes,
                        )
                    )

        return tuple(planned)

    # ── measurements ────────────────────────────────────────────────────────

    def _widen_dimensions(
        self,
        request: ProductSearchRequest,
        semantics: ConstraintSemantics,
        running: tuple[DimensionConstraint, ...],
        strength: ConstraintStrength,
        fraction: Decimal,
        stage: int,
    ) -> tuple[tuple[DimensionConstraint, ...], list[AppliedRelaxation]]:
        """One stage, applied to every measurement the policy and strength allow.

        Widened from `request.dimensions` - the customer's own figures - while
        `running` only carries earlier stages forward for the untouched
        measurements. A stage computed from the running value would compound.
        """
        widened = list(running)
        changes: list[AppliedRelaxation] = []
        for index, original in enumerate(request.dimensions):
            if not self._dimension_policy.is_relaxable(
                request.commerce_subcategory, original.role
            ):
                continue
            if not self._may_relax(
                semantics.strength_for_dimension(original.role), strength
            ):
                continue
            constraint, change = self._widen_dimension(original, fraction, strength, stage)
            if constraint == widened[index]:
                continue
            widened[index] = constraint
            changes.append(change)
        return tuple(widened), changes

    def _widen_dimension(
        self,
        original: DimensionConstraint,
        fraction: Decimal,
        strength: ConstraintStrength,
        stage: int,
    ) -> tuple[DimensionConstraint, DimensionRelaxationChange]:
        """The executable constraint for this stage, and what it records.

        A TARGET executes as a band because that is the only way PostgreSQL can
        express "near 220". Its recorded `kind` stays TARGET: the customer named
        a figure to sit near, not an interval, and a later layer explaining the
        result needs to know which of the two they said.
        """
        low = self._outward(original.min_cm, fraction, downwards=True)
        high = self._outward(original.max_cm, fraction, downwards=False)
        record: dict[str, Decimal | None] = {
            "original_min_cm": original.min_cm,
            "original_max_cm": original.max_cm,
            "original_target_cm": original.target_cm,
        }

        if original.kind is DimensionConstraintKind.TARGET:
            assert original.target_cm is not None
            spread = self._quantise(original.target_cm * fraction)
            low, high = original.target_cm - spread, original.target_cm + spread
            executable = DimensionConstraintKind.RANGE
        else:
            executable = original.kind

        # Rebuilt through the contract rather than copied: a widened TARGET
        # executes as a band and must no longer carry a target figure, and only
        # the real validator enforces that a kind holds exactly its own values.
        constraint = DimensionConstraint(
            role=original.role,
            kind=executable,
            min_cm=low,
            max_cm=high,
            source_value=original.source_value,
            source_unit=original.source_unit,
        )
        change = DimensionRelaxationChange(
            role=original.role,
            kind=original.kind,
            strength=strength,
            stage=stage,
            applied_min_cm=low,
            applied_max_cm=high,
            **record,
        )
        return constraint, change

    @staticmethod
    def _quantise(value: Decimal) -> Decimal:
        return value.quantize(_CENTIMETRE_QUANTUM, rounding=ROUND_HALF_UP)

    @classmethod
    def _outward(
        cls, original: Decimal | None, fraction: Decimal, *, downwards: bool
    ) -> Decimal | None:
        """A bound moves away from the customer's figure, never towards it.

        A floor drops and a ceiling rises, both computed from the original, so a
        second stage widens further rather than widening the first stage again.
        """
        if original is None:
            return None
        step = Decimal(1) - fraction if downwards else Decimal(1) + fraction
        return cls._quantise(original * step)

    # ── permission ──────────────────────────────────────────────────────────

    @staticmethod
    def _may_relax(
        recorded: ConstraintStrength | None, strength: ConstraintStrength
    ) -> bool:
        """Only an explicitly recorded, matching, non-locked strength permits it.

        `None` means the constraint is absent or its strength was never
        recorded; neither is consent.
        """
        return recorded is not None and recorded is strength

    # ── widening arithmetic, always from the original value ─────────────────

    @staticmethod
    def _widen_price_ceiling(original: Decimal | None, fraction: Decimal) -> Decimal | None:
        """A ceiling rises. It is never removed."""
        if original is None:
            return None
        return original * (Decimal(1) + fraction)

    @staticmethod
    def _widen_price_floor(original: Decimal | None, fraction: Decimal) -> Decimal | None:
        """A floor drops, never past zero, and is never removed."""
        if original is None:
            return None
        widened = original * (Decimal(1) - fraction)
        return max(widened, _NO_PRICE)

    @staticmethod
    def _widen_seat_ceiling(original: int | None, delta: int) -> int | None:
        if original is None:
            return None
        return original + delta

    @staticmethod
    def _widen_seat_floor(original: int | None, delta: int) -> int | None:
        """Clamped at one seat: nobody shops for a nought-seater."""
        if original is None:
            return None
        return max(_MIN_SEATS, original - delta)

    # ── reconstruction through the validated contracts ──────────────────────

    @staticmethod
    def _build(
        request: ProductSearchRequest,
        price_min: Decimal | None,
        price_max: Decimal | None,
        seats_min: int | None,
        seats_max: int | None,
        dimensions: tuple[DimensionConstraint, ...],
        changes: Sequence[AppliedRelaxation],
    ) -> PlannedRelaxation:
        """Rebuild through the real contracts, so a widened request is as
        validated as the customer's own. Nothing is copied unvalidated.

        **Everything not widened is carried, by construction rather than by a
        list of field names.** Naming each field meant that adding one to
        `ProductSearchRequest` silently dropped it from every widened attempt -
        a product exclusion that held at depth 0 and lapsed at depth 1, or a
        strict price bound that quietly became inclusive. Overriding a dump
        cannot forget a field, and re-validating the result keeps the promise
        above.
        """
        price = request.price
        widened_price = (
            PriceConstraint(
                **{
                    **price.model_dump(),
                    "min_amount": price_min,
                    "max_amount": price_max,
                }
            )
            if price is not None
            else None
        )
        capacity = request.seating_capacity
        widened_capacity = (
            SeatingCapacityConstraint(
                **{
                    **capacity.model_dump(),
                    "min_capacity": seats_min,
                    "max_capacity": seats_max,
                }
            )
            if capacity is not None
            else None
        )
        widened: dict[str, Any] = {
            "price": widened_price,
            "seating_capacity": widened_capacity,
            # Measurements arrive already widened, or unchanged when the
            # policy and strength allowed nothing. A locked or non-relaxable
            # one is the identical frozen object the customer produced, so it
            # cannot drift (CLAUDE.md 13.5).
            "dimensions": dimensions,
        }
        # Colour, style, planar pairs, exclusions, the limit and the sort are
        # never widened; they arrive from the dump unchanged.
        return PlannedRelaxation(
            request=ProductSearchRequest(**{**request.model_dump(), **widened}),
            changes=tuple(changes),
        )
