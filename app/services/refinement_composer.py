"""Turning a typed refinement into the next candidate search.

Pure. No database, no provider, no registry loaded from disk, no clock, no
state written. It takes the search in progress and what the customer asked to
change, and returns what the next search would be.

Three entry points, because three situations genuinely differ:

* :meth:`refine` - the search continues, with some axes changed.
* :meth:`seed_new_task` - a new search begins, and defaults may seed it once.
* :meth:`refine_taxonomy` - the product type changed within the same family,
  and everything but its sizes survives.

**Sizes belong to a product type.** 60 cm for a side table says nothing about a
coffee table, so a measurement never follows the customer to a different type.
Each type's sizes are saved in state after its search runs, and a search for
that type which states no size of its own gets them back.

Two rules run through all of them.

**Omission preserves.** An axis the delta does not mention provably cannot
change, which is what lets "show me cheaper ones" keep a colour requirement
from three turns ago. Clearing is something the customer asked for, and has
its own operation.

**Defaults never become filters.** A customer who generally likes Modern has
not excluded everything else, so a default can seed a semantic preference and
nothing more (CLAUDE.md 12.4).

Nothing here commits. The revision is carried through untouched: changing
criteria is not executing a search, and only a committed result set moves the
lineage (CLAUDE.md 13.2).
"""

from __future__ import annotations

from decimal import Decimal

from app.core.numbers import parse_stated_amount, parse_stated_decimal
from app.schemas.agent_decision import (
    BlockingClarificationReason,
    CustomerStateProposal,
    PreferenceProposal,
    PreferenceProposalOp,
)
from app.schemas.agent_state import ActiveSearchState, SavedMeasurements
from app.schemas.composition import (
    ComposedSearch,
    CompositionDefect,
    CompositionFailed,
    CompositionNeedsClarification,
    CompositionOutcome,
    NewTaskRequired,
)
from app.schemas.dimensions import parse_unit, to_centimetres
from app.schemas.discovery import (
    DimensionConstraint,
    PlanarDimensionConstraint,
    PriceConstraint,
    ProductSearchRequest,
    ProductSort,
    SeatingCapacityConstraint,
)
from app.schemas.grounding import DroppedConstraint
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    PlanarDimensionSemantics,
    ResolvedSearch,
    SemanticPreference,
    UnresolvedAttribute,
)
from app.schemas.refinement import (
    AttributeRefinement,
    AttributeRefinementOp,
    CapacityRefinement,
    DimensionRefinement,
    PlanarRefinement,
    PriceRefinement,
    PriceRefinementOp,
    RefinementOp,
    SearchRefinementDelta,
    SemanticIntentOp,
    SemanticIntentRefinement,
    SortRefinement,
)
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.dimensions import DimensionRole, DimensionSemantics, UnsupportedDimensionReason
from app.taxonomy.seating import SeatingRules

# An unqualified requirement is a requirement (CLAUDE.md 13.1). A bound the
# customer stated without softening it is locked, here as in M7.
_DEFAULT_STRENGTH = ConstraintStrength.LOCKED

# Seeding a preference records how the customer expressed it: a default is a
# leaning by definition, never a rule.
_SEEDED_STRENGTH = ConstraintStrength.PREFERRED


class _Unset:
    """Distinguishes "no subcategory change" from "clear the subcategory".

    A refinement may legitimately move a search to no subcategory at all,
    so `None` cannot also mean "leave it alone".
    """


_UNSET = _Unset()


class _Refused(Exception):  # noqa: N818 - an internal control signal, not an error
    """Carries a typed outcome out of a nested step.

    Private to this module and never escapes it: every public method converts
    it back into a returned value. Threading an outcome through a dozen
    partial results by hand would bury the composition logic that matters.
    """

    def __init__(self, outcome: CompositionOutcome) -> None:
        super().__init__()
        self.outcome = outcome


def _clarify(reason: BlockingClarificationReason) -> _Refused:
    return _Refused(CompositionNeedsClarification(reason=reason))


def _defect(defect: CompositionDefect) -> _Refused:
    return _Refused(CompositionFailed(defect=defect))


def _decimal(raw: str) -> Decimal:
    try:
        return parse_stated_decimal(raw)
    except ValueError as exc:
        raise _defect(CompositionDefect.MALFORMED_AMOUNT) from exc


def _money(raw: str) -> Decimal:
    try:
        return parse_stated_amount(raw)
    except ValueError as exc:
        raise _defect(CompositionDefect.MALFORMED_AMOUNT) from exc


class SearchRefinementComposer:
    """Deterministic search composition. Injected registries, no file I/O."""

    def __init__(
        self,
        attributes: CatalogAttributes,
        dimensions: DimensionSemantics,
        seating: SeatingRules | None = None,
    ) -> None:
        # Named apart from the `_attributes` and `_dimensions` methods below:
        # the registries are collaborators, the methods are composition steps.
        self._catalog_attributes = attributes
        self._dimension_semantics = dimensions
        self._seating = seating

    # ── entry points ────────────────────────────────────────────────────────

    def refine(
        self, state: ActiveSearchState | None, delta: SearchRefinementDelta
    ) -> CompositionOutcome:
        """Apply a delta to the search in progress."""
        if state is None:
            return CompositionFailed(defect=CompositionDefect.NO_ACTIVE_SEARCH)
        try:
            return self._compose(state, delta)
        except _Refused as refused:
            return refused.outcome

    def refine_taxonomy(
        self,
        state: ActiveSearchState | None,
        *,
        commerce_category: str,
        commerce_subcategory: str | None,
        delta: SearchRefinementDelta | None = None,
        saved_measurements: SavedMeasurements | None = None,
    ) -> CompositionOutcome:
        """Change the product type, keeping everything but its sizes.

        The old type's sizes stay behind; `saved_measurements` - what the
        customer gave for the new type earlier - take their place, and any size
        stated in this same message is applied on top.

        Takes the approved pair and nothing else. Accepting M7's whole result
        here would put its price, its measurements and above all its transient
        `semantic_text` within reach - and a contextless call made only to read
        "make them sectionals" must not replace the conversation's durable
        fuzzy intent.
        """
        if state is None:
            return CompositionFailed(defect=CompositionDefect.NO_ACTIVE_SEARCH)
        if commerce_category != state.request.commerce_category:
            return NewTaskRequired(commerce_category=commerce_category)
        try:
            return self._compose(
                state,
                delta or SearchRefinementDelta(),
                subcategory=commerce_subcategory,
                saved_measurements=saved_measurements,
            )
        except _Refused as refused:
            return refused.outcome

    def seed_new_task(
        self,
        resolved: ResolvedSearch,
        *,
        proposal: CustomerStateProposal | None = None,
        room_preferences: tuple[SemanticPreference, ...] = (),
        customer_defaults: tuple[SemanticPreference, ...] = (),
        semantic_intent: SemanticIntentRefinement | None = None,
        saved_measurements: SavedMeasurements | None = None,
        revision: int = 0,
    ) -> ComposedSearch:
        """Begin a new search, letting defaults fill the axes it left open.

        `resolved` is M7's: the customer's explicit request, already validated
        against the taxonomy. Its structured constraints are untouched, and its
        `semantic_text` is the text this search executes with - a durable
        intent proposed alongside it is a different concept and is not
        appended to it.

        `saved_measurements` are the sizes the customer gave for this product
        type earlier. They apply only when this request states no size at all:
        a customer who names a size now gets exactly that size, not a blend
        with an old one.
        """
        if self._seats_one(resolved.request.commerce_subcategory) and (
            resolved.request.seating_capacity is not None
        ):
            resolved = resolved.model_copy(
                update={
                    "request": resolved.request.model_copy(update={"seating_capacity": None}),
                    "semantics": resolved.semantics.model_copy(
                        update={"seating_min": None, "seating_max": None}
                    ),
                }
            )
        restored = False
        if not resolved.request.dimensions and resolved.request.planar_dimensions is None:
            request, semantics = self._restore(
                resolved.request, resolved.semantics, saved_measurements
            )
            restored = request is not resolved.request
            resolved = resolved.model_copy(update={"request": request, "semantics": semantics})
        preferences = self._seed_preferences(
            resolved.semantic_preferences, proposal, room_preferences, customer_defaults
        )
        intent = _apply_intent(None, semantic_intent)
        candidate = ActiveSearchState(
            request=resolved.request,
            semantics=resolved.semantics,
            semantic_preferences=preferences,
            semantic_intent=intent,
            revision=revision,
        )
        return ComposedSearch(
            candidate=candidate,
            resolved=ResolvedSearch(
                request=resolved.request,
                semantics=resolved.semantics,
                semantic_preferences=preferences,
                # M7's wording, for this execution only.
                semantic_text=resolved.semantic_text,
                unmatched_strict=resolved.unmatched_strict,
            ),
            earlier_sizes_applied=restored,
        )

    # ── composition ─────────────────────────────────────────────────────────

    def _compose(
        self,
        state: ActiveSearchState,
        delta: SearchRefinementDelta,
        *,
        subcategory: str | None | _Unset = _UNSET,
        saved_measurements: SavedMeasurements | None = None,
    ) -> ComposedSearch:
        request, semantics = state.request, state.semantics
        dropped: tuple[DroppedConstraint, ...] = ()
        restored: ProductSearchRequest | None = None

        if not isinstance(subcategory, _Unset):
            request, semantics, dropped, restored = self._retype(
                request, semantics, subcategory, saved_measurements
            )

        price, price_min_s, price_max_s = self._price(request, semantics, delta.price)
        capacity, seat_min_s, seat_max_s = self._capacity(
            request, semantics, delta.seating_capacity
        )
        if self._seats_one(request.commerce_subcategory):
            # Every one of them seats one person, and the catalog records no
            # count for them, so a seat filter could only ever hide them all.
            capacity, seat_min_s, seat_max_s = None, None, None
        elif (
            delta.seating_capacity is not None
            or request.commerce_subcategory != state.request.commerce_subcategory
        ) and self._one_seat_of_several(request.commerce_subcategory, capacity):
            # Only when this turn set the seat count or the type. A search that
            # already holds one (an older session, a misread new search) must
            # stay refinable - it finds nothing, and the reply offers what
            # setting the seat count aside would find.
            raise _defect(CompositionDefect.ONE_SEAT_ON_MULTI_SEAT_TYPE)
        dimensions, dimension_semantics = self._dimensions(
            request, semantics, delta.dimensions
        )
        planar, planar_semantics = self._planar(
            request, semantics, delta.planar_dimensions
        )
        colors, styles, preferences, unmatched = self._attributes(
            request, state.semantic_preferences, delta.attributes
        )
        intent = _apply_intent(state.semantic_intent, delta.semantic_intent)

        composed = ProductSearchRequest(
            commerce_category=request.commerce_category,
            commerce_subcategory=request.commerce_subcategory,
            price=price,
            seating_capacity=capacity,
            dimensions=dimensions,
            planar_dimensions=planar,
            colors_any_of=colors,
            styles_all_of=styles,
            # Criteria changed, so this is a fresh set of results: products
            # left out while paging through the old criteria ("show more") may
            # be exactly what the new ones find. Keeping them hidden made
            # "show more" then "under 3000" return nothing.
            exclude_product_ids=(),
            limit=request.limit,
            sort=_apply_sort(request.sort, delta.sort),
        )
        composed_semantics = ConstraintSemantics(
            subcategory=semantics.subcategory,
            price_min=price_min_s,
            price_max=price_max_s,
            seating_min=seat_min_s,
            seating_max=seat_max_s,
            dimensions=dimension_semantics,
            planar_dimension=planar_semantics,
        )
        candidate = ActiveSearchState(
            request=composed,
            semantics=composed_semantics,
            semantic_preferences=preferences,
            semantic_intent=intent,
            # Criteria changing is not a search executing (CLAUDE.md 13.2).
            revision=state.revision,
        )
        return ComposedSearch(
            candidate=candidate,
            resolved=ResolvedSearch(
                request=composed,
                semantics=composed_semantics,
                semantic_preferences=preferences,
                # A refinement executes on the durable intent, never on a
                # contextless M7 reading of this turn's words.
                semantic_text=intent,
                unmatched_strict=unmatched,
            ),
            dropped_constraints=_not_restated(dropped, composed),
            earlier_sizes_applied=_still_applied(restored, composed),
        )

    # ── the product type ────────────────────────────────────────────────────

    def _retype(
        self,
        request: ProductSearchRequest,
        semantics: ConstraintSemantics,
        subcategory: str | None,
        saved: SavedMeasurements | None,
    ) -> tuple[
        ProductSearchRequest,
        ConstraintSemantics,
        tuple[DroppedConstraint, ...],
        ProductSearchRequest | None,
    ]:
        """Re-point the search at a product type in the same family.

        The old type's sizes are left behind - state keeps them for that type -
        and the new type starts from its own saved sizes, or none. Price,
        capacity, colour and style are subcategory-independent and always
        survive. Returns the restored request too, when sizes came back, so the
        caller can tell whether they are still in force after this turn's own
        changes.
        """
        if subcategory == request.commerce_subcategory:
            return request, semantics, (), None

        retyped = request.model_copy(
            update={
                "commerce_subcategory": subcategory,
                "dimensions": (),
                "planar_dimensions": None,
            }
        )
        bare = semantics.model_copy(
            update={
                # They named the new type outright, so it is a requirement.
                "subcategory": None if subcategory is None else _DEFAULT_STRENGTH,
                "dimensions": (),
                "planar_dimension": None,
            }
        )
        restored_request, restored_semantics = self._restore(retyped, bare, saved)
        restored_roles = {c.role for c in restored_request.dimensions}

        dropped = [
            DroppedConstraint(role=c.role, reason=self._unsupported(subcategory, c.role))
            for c in request.dimensions
            if c.role not in restored_roles
        ]
        if request.planar_dimensions is not None and restored_request.planar_dimensions is None:
            dropped.append(
                DroppedConstraint(
                    role=None,
                    reason=(
                        None
                        if self._dimension_semantics.supports_planar(subcategory)
                        else UnsupportedDimensionReason.UNSUPPORTED_PRODUCT_GEOMETRY
                    ),
                )
            )
        return (
            restored_request,
            restored_semantics,
            tuple(dropped),
            restored_request if restored_request is not retyped else None,
        )

    def _seats_one(self, subcategory: str | None) -> bool:
        return self._seating is not None and self._seating.seats_one(subcategory)

    def _one_seat_of_several(
        self, subcategory: str | None, capacity: SeatingCapacityConstraint | None
    ) -> bool:
        """"A sofa for one" - a seat count no product of this type can meet.

        Refused rather than executed, because it is a misreading and not a
        search: a piece for one person is its own product type, and the
        decision is corrected to change type instead.
        """
        return (
            self._seating is not None
            and self._seating.seats_several(subcategory)
            and capacity is not None
            and capacity.max_capacity is not None
            and capacity.max_capacity < 2
        )

    def _unsupported(
        self, subcategory: str | None, role: DimensionRole
    ) -> UnsupportedDimensionReason | None:
        if self._dimension_semantics.source_axis(subcategory, role) is not None:
            return None
        return self._dimension_semantics.unsupported_reason(subcategory, role)

    def _restore(
        self,
        request: ProductSearchRequest,
        semantics: ConstraintSemantics,
        saved: SavedMeasurements | None,
    ) -> tuple[ProductSearchRequest, ConstraintSemantics]:
        """Apply the sizes saved for this request's product type.

        Returns the very same objects when nothing applies, which is how a
        caller tells. Each size is re-checked against the registry, so a
        mapping withdrawn since it was saved is not applied from memory.
        """
        subcategory = request.commerce_subcategory
        if saved is None or subcategory is None or saved.commerce_subcategory != subcategory:
            return request, semantics
        dimensions = tuple(
            c
            for c in saved.dimensions
            if self._dimension_semantics.source_axis(subcategory, c.role) is not None
        )
        roles = {c.role for c in dimensions}
        planar_ok = (
            saved.planar_dimensions is not None
            and self._dimension_semantics.supports_planar(subcategory)
        )
        if not dimensions and not planar_ok:
            return request, semantics
        return (
            request.model_copy(
                update={
                    "dimensions": dimensions,
                    "planar_dimensions": saved.planar_dimensions if planar_ok else None,
                }
            ),
            semantics.model_copy(
                update={
                    "dimensions": tuple(
                        s for s in saved.dimension_semantics if s.role in roles
                    ),
                    "planar_dimension": saved.planar_semantics if planar_ok else None,
                }
            ),
        )

    # ── price ───────────────────────────────────────────────────────────────

    def _price(
        self,
        request: ProductSearchRequest,
        semantics: ConstraintSemantics,
        refinement: PriceRefinement | None,
    ) -> tuple[PriceConstraint | None, ConstraintStrength | None, ConstraintStrength | None]:
        if refinement is None:
            return request.price, semantics.price_min, semantics.price_max
        if refinement.op is PriceRefinementOp.CLEAR:
            return None, None, None
        if refinement.op is PriceRefinementOp.SET_RELATIVE:
            # The bound depends on a price only PostgreSQL holds, so the layer
            # above must resolve it before composition can run.
            raise _defect(CompositionDefect.RELATIVE_PRICE_NOT_RESOLVED)

        currency = (refinement.currency or "").strip() or _inherited_currency(request)
        if currency is None:
            raise _clarify(BlockingClarificationReason.MISSING_REFINEMENT_CURRENCY)

        minimum = _money(refinement.min_amount) if refinement.min_amount else None
        maximum = _money(refinement.max_amount) if refinement.max_amount else None
        try:
            constraint = PriceConstraint(
                currency=currency,
                min_amount=minimum,
                max_amount=maximum,
                min_exclusive=refinement.min_exclusive,
                max_exclusive=refinement.max_exclusive,
            )
        except ValueError as exc:
            raise _defect(CompositionDefect.MALFORMED_AMOUNT) from exc
        return (
            constraint,
            _strength(refinement.min_strength, present=minimum is not None),
            _strength(refinement.max_strength, present=maximum is not None),
        )

    # ── seating ─────────────────────────────────────────────────────────────

    @staticmethod
    def _capacity(
        request: ProductSearchRequest,
        semantics: ConstraintSemantics,
        refinement: CapacityRefinement | None,
    ) -> tuple[
        SeatingCapacityConstraint | None,
        ConstraintStrength | None,
        ConstraintStrength | None,
    ]:
        if refinement is None:
            return request.seating_capacity, semantics.seating_min, semantics.seating_max
        if refinement.op is RefinementOp.CLEAR:
            return None, None, None
        minimum, maximum = refinement.min_capacity, refinement.max_capacity
        try:
            constraint = SeatingCapacityConstraint(
                min_capacity=minimum, max_capacity=maximum
            )
        except ValueError as exc:
            raise _defect(CompositionDefect.MALFORMED_AMOUNT) from exc
        return (
            constraint,
            _strength(refinement.min_strength, present=minimum is not None),
            _strength(refinement.max_strength, present=maximum is not None),
        )

    # ── measurements ────────────────────────────────────────────────────────

    def _dimensions(
        self,
        request: ProductSearchRequest,
        semantics: ConstraintSemantics,
        refinements: tuple[DimensionRefinement, ...],
    ) -> tuple[tuple[DimensionConstraint, ...], tuple[DimensionConstraintSemantics, ...]]:
        """Per role, so a measurement and its strength always move together."""
        current = {c.role: c for c in request.dimensions}
        strengths = {s.role: s for s in semantics.dimensions}

        for refinement in refinements:
            role = refinement.role
            if refinement.op is RefinementOp.CLEAR:
                current.pop(role, None)
                strengths.pop(role, None)
                continue
            constraint = self._dimension(refinement, current.get(role))
            current[role] = constraint
            strengths[role] = DimensionConstraintSemantics(
                role=role, strength=refinement.strength or _DEFAULT_STRENGTH
            )

        ordered = tuple(current.values())
        return ordered, tuple(strengths[c.role] for c in ordered)

    def _dimension(
        self, refinement: DimensionRefinement, existing: DimensionConstraint | None
    ) -> DimensionConstraint:
        """One measurement, converted to centimetres through the shared units.

        The unit is the customer's, this turn or the last time they gave one
        for **this same role**. A depth with no unit cannot borrow the width's
        centimetres: they are separate statements, and assuming otherwise
        would silently answer a question nobody asked.
        """
        source_unit = refinement.unit or (existing.source_unit if existing else None)
        unit = parse_unit(source_unit)
        if unit is None:
            raise _clarify(BlockingClarificationReason.MISSING_DIMENSION_UNIT)

        values = {
            "min_cm": refinement.min_value,
            "max_cm": refinement.max_value,
            "target_cm": refinement.target_value,
        }
        converted = {
            name: to_centimetres(_decimal(raw), unit) if raw is not None else None
            for name, raw in values.items()
        }
        stated = (
            refinement.max_value or refinement.min_value or refinement.target_value
        )
        try:
            return DimensionConstraint(
                role=refinement.role,
                kind=refinement.kind,
                source_value=stated,
                source_unit=source_unit,
                **converted,
            )
        except ValueError as exc:
            raise _defect(CompositionDefect.MALFORMED_AMOUNT) from exc

    def _planar(
        self,
        request: ProductSearchRequest,
        semantics: ConstraintSemantics,
        refinement: PlanarRefinement | None,
    ) -> tuple[PlanarDimensionConstraint | None, PlanarDimensionSemantics | None]:
        """A pair inherits only from a pair.

        A rug's sides and a sofa's width are different measurements of
        different things; neither unit says anything about the other.
        """
        if refinement is None:
            return request.planar_dimensions, semantics.planar_dimension
        if refinement.op is RefinementOp.CLEAR:
            return None, None

        existing = request.planar_dimensions
        source_unit = refinement.unit or (existing.source_unit if existing else None)
        unit = parse_unit(source_unit)
        if unit is None:
            raise _clarify(BlockingClarificationReason.MISSING_DIMENSION_UNIT)
        assert refinement.first_value is not None
        assert refinement.second_value is not None
        try:
            constraint = PlanarDimensionConstraint(
                first_cm=to_centimetres(_decimal(refinement.first_value), unit),
                second_cm=to_centimetres(_decimal(refinement.second_value), unit),
                source_unit=source_unit,
            )
        except ValueError as exc:
            raise _defect(CompositionDefect.MALFORMED_AMOUNT) from exc
        return constraint, PlanarDimensionSemantics(
            strength=refinement.strength or _DEFAULT_STRENGTH
        )

    # ── colour and style ────────────────────────────────────────────────────

    def _attributes(
        self,
        request: ProductSearchRequest,
        preferences: tuple[SemanticPreference, ...],
        refinements: tuple[AttributeRefinement, ...],
    ) -> tuple[
        tuple[str, ...],
        tuple[str, ...],
        tuple[SemanticPreference, ...],
        tuple[UnresolvedAttribute, ...],
    ]:
        """One family's whole position at a time: requirement and preference.

        A strict value no approved value expresses ("only reddish") comes back
        as unmatched rather than refused: the search runs without it, their
        words rank the closest first, and the reply says plainly that nothing
        matched. Approved values in the same operation still filter.

        An operation replaces the axis rather than adding to it. `styles_all_of`
        is a conjunction, so adding Japandi to Modern would narrow the search to
        products carrying both and the customer would see nothing.

        A clear removes both, because "I don't care about style" must not leave
        a stale leaning steering the ranking.
        """
        exact: dict[AttributeFamily, tuple[str, ...]] = {
            AttributeFamily.COLOR: request.colors_any_of,
            AttributeFamily.STYLE: request.styles_all_of,
        }
        kept = list(preferences)
        unmatched: list[UnresolvedAttribute] = []

        for refinement in refinements:
            family = refinement.family
            exact[family] = ()
            kept = [p for p in kept if p.family is not family]
            if refinement.op is AttributeRefinementOp.CLEAR:
                continue
            if refinement.op is AttributeRefinementOp.SET_REQUIREMENT:
                named = [value for value in refinement.values if value.canonical_value]
                exact[family] = tuple(
                    self._approved(family, value.canonical_value) for value in named
                )
                # Colours are alternatives, so an unexpressible one beside an
                # approved one is just a further wish. Styles are all required,
                # so an unexpressible style means no exact match is possible
                # even beside approved ones.
                lifts = family is AttributeFamily.STYLE or not named
                for value in refinement.values:
                    if value.canonical_value:
                        continue
                    if lifts:
                        unmatched.append(
                            UnresolvedAttribute(family=family, raw_value=value.raw_value)
                        )
                    kept.append(
                        SemanticPreference(
                            family=family,
                            raw_value=value.raw_value,
                            strength=ConstraintStrength.LOCKED,
                        )
                    )
                continue
            kept.extend(
                SemanticPreference(
                    family=family,
                    raw_value=value.raw_value,
                    # A preference only steers ranking, so a spelling the
                    # registry does not know is kept as their words alone
                    # rather than refused.
                    canonical_value=(
                        self._catalog_attributes.canonical(family, value.canonical_value)
                        if value.canonical_value
                        else None
                    ),
                    strength=ConstraintStrength.PREFERRED,
                )
                for value in refinement.values
            )

        return (
            exact[AttributeFamily.COLOR],
            exact[AttributeFamily.STYLE],
            tuple(kept),
            tuple(unmatched),
        )

    def _approved(self, family: AttributeFamily, value: str | None) -> str:
        """The registry decides, not the schema that called a field canonical.

        Returned in the registry's own spelling, so "beige" filters on `Beige`
        rather than failing an exact match against the stored value.
        """
        approved = self._catalog_attributes.canonical(family, value) if value else None
        if approved is None:
            raise _defect(CompositionDefect.UNAPPROVED_ATTRIBUTE_VALUE)
        return approved

    # ── seeding ─────────────────────────────────────────────────────────────

    @staticmethod
    def _seed_preferences(
        explicit: tuple[SemanticPreference, ...],
        proposal: CustomerStateProposal | None,
        room: tuple[SemanticPreference, ...],
        defaults: tuple[SemanticPreference, ...],
    ) -> tuple[SemanticPreference, ...]:
        """Fill each axis from the highest level that has anything to say.

        Per family, not per whole list: a room's warm neutral can seed the
        colour axis while this turn's explicit request owns the style axis.

        Only preferences flow downward. A default never becomes an exact
        filter, because a customer who generally likes Modern has not excluded
        everything else (CLAUDE.md 12.4).
        """
        levels: tuple[tuple[SemanticPreference, ...], ...] = (
            explicit,
            _proposed(proposal.design_preferences) if proposal else (),
            _proposed(proposal.customer_preferences) if proposal else (),
            room,
            defaults,
        )
        seeded: list[SemanticPreference] = []
        for family in AttributeFamily:
            for level in levels:
                matching = [p for p in level if p.family is family]
                if matching:
                    seeded.extend(
                        # A copy, not a transfer: clearing this search's style
                        # later must not delete the customer's own preference.
                        p.model_copy(update={"strength": _SEEDED_STRENGTH})
                        if level is not explicit
                        else p
                        for p in matching
                    )
                    break
        return tuple(seeded)


def _proposed(proposal: PreferenceProposal | None) -> tuple[SemanticPreference, ...]:
    """What a proposal would add. A removal seeds nothing."""
    if proposal is None or proposal.op is PreferenceProposalOp.REMOVE:
        return ()
    return proposal.preferences


def _inherited_currency(request: ProductSearchRequest) -> str | None:
    """The currency the search is already denominated in, if any.

    There is no other source. `RetailerContext` carries only a store id, and
    `price_unit` is a per-product column written by an unvalidated import, so
    neither establishes a retailer's currency (CLAUDE.md 12.1).
    """
    return request.price.currency if request.price is not None else None


def _strength(
    stated: ConstraintStrength | None, *, present: bool
) -> ConstraintStrength | None:
    """Absent bound -> no strength. Present and unqualified -> locked."""
    if not present:
        return None
    return stated or _DEFAULT_STRENGTH


def _apply_intent(
    current: str | None, refinement: SemanticIntentRefinement | None
) -> str | None:
    if refinement is None:
        return current
    if refinement.op is SemanticIntentOp.CLEAR:
        return None
    return refinement.value


def _apply_sort(current: ProductSort, refinement: SortRefinement | None) -> ProductSort:
    if refinement is None:
        return current
    if refinement.op is RefinementOp.CLEAR:
        return ProductSort.DEFAULT
    assert refinement.value is not None  # the contract requires it for a set
    return refinement.value


def _still_applied(
    restored: ProductSearchRequest | None, composed: ProductSearchRequest
) -> bool:
    """Whether any size brought back from memory is still in the search.

    A size stated in the same message replaces the saved one for its role, and
    then nothing restored is left to mention.
    """
    if restored is None:
        return False
    if any(c in composed.dimensions for c in restored.dimensions):
        return True
    return (
        restored.planar_dimensions is not None
        and restored.planar_dimensions == composed.planar_dimensions
    )


def _not_restated(
    dropped: tuple[DroppedConstraint, ...], composed: ProductSearchRequest
) -> tuple[DroppedConstraint, ...]:
    """Left-behind sizes, minus any the customer stated again in this message.

    "Coffee tables under 100 cm wide" after a side-table width applies 100 cm;
    reporting the width as dropped would have the reply say the cards are not
    held to a width they are held to.
    """
    applied = {c.role for c in composed.dimensions}
    return tuple(
        d
        for d in dropped
        if not (
            (d.role is not None and d.role in applied)
            or (d.role is None and composed.planar_dimensions is not None)
        )
    )
