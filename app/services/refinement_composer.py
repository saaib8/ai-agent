"""Turning a typed refinement into the next candidate search.

Pure. No database, no provider, no registry loaded from disk, no clock, no
state written. It takes the search in progress and what the customer asked to
change, and returns what the next search would be.

Three entry points, because three situations genuinely differ:

* :meth:`refine` - the search continues, with some axes changed.
* :meth:`seed_new_task` - a new search begins, and defaults may seed it once.
* :meth:`refine_taxonomy` - the product type changed within the same family,
  and compatible constraints survive.

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

from decimal import Decimal, InvalidOperation

from app.schemas.agent_decision import (
    BlockingClarificationReason,
    CustomerStateProposal,
    PreferenceProposal,
    PreferenceProposalOp,
)
from app.schemas.agent_state import ActiveSearchState
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
from app.taxonomy.dimensions import DimensionSemantics, UnsupportedDimensionReason

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
        return Decimal(raw.strip())
    except (InvalidOperation, ValueError) as exc:
        raise _defect(CompositionDefect.MALFORMED_AMOUNT) from exc


class SearchRefinementComposer:
    """Deterministic search composition. Injected registries, no file I/O."""

    def __init__(
        self, attributes: CatalogAttributes, dimensions: DimensionSemantics
    ) -> None:
        # Named apart from the `_attributes` and `_dimensions` methods below:
        # the registries are collaborators, the methods are composition steps.
        self._catalog_attributes = attributes
        self._dimension_semantics = dimensions

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
    ) -> CompositionOutcome:
        """Change the product type, keeping what the new one can support.

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
        revision: int = 0,
    ) -> ComposedSearch:
        """Begin a new search, letting defaults fill the axes it left open.

        `resolved` is M7's: the customer's explicit request, already validated
        against the taxonomy. Its structured constraints are untouched, and its
        `semantic_text` is the text this search executes with - a durable
        intent proposed alongside it is a different concept and is not
        appended to it.
        """
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
            ),
        )

    # ── composition ─────────────────────────────────────────────────────────

    def _compose(
        self,
        state: ActiveSearchState,
        delta: SearchRefinementDelta,
        *,
        subcategory: str | None | _Unset = _UNSET,
    ) -> ComposedSearch:
        request, semantics = state.request, state.semantics
        dropped: tuple[DroppedConstraint, ...] = ()

        if not isinstance(subcategory, _Unset):
            request, semantics, dropped = self._retype(request, semantics, subcategory)

        price, price_min_s, price_max_s = self._price(request, semantics, delta.price)
        capacity, seat_min_s, seat_max_s = self._capacity(
            request, semantics, delta.seating_capacity
        )
        dimensions, dimension_semantics = self._dimensions(
            request, semantics, delta.dimensions
        )
        planar, planar_semantics = self._planar(
            request, semantics, delta.planar_dimensions
        )
        colors, styles, preferences = self._attributes(
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
            exclude_product_ids=request.exclude_product_ids,
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
            ),
            dropped_constraints=dropped,
        )

    # ── the product type ────────────────────────────────────────────────────

    def _retype(
        self,
        request: ProductSearchRequest,
        semantics: ConstraintSemantics,
        subcategory: str | None,
    ) -> tuple[ProductSearchRequest, ConstraintSemantics, tuple[DroppedConstraint, ...]]:
        """Re-point the search at a product type in the same family.

        Measurements are the only thing that can stop being answerable: the
        registry maps a role per subcategory, so a sofa's width has no meaning
        for a type whose stored axes were never established. Price, capacity,
        colour and style are subcategory-independent and always survive.
        """
        dropped: list[DroppedConstraint] = []
        kept: list[DimensionConstraint] = []
        for constraint in request.dimensions:
            if self._dimension_semantics.source_axis(subcategory, constraint.role) is None:
                dropped.append(
                    DroppedConstraint(
                        role=constraint.role,
                        reason=self._dimension_semantics.unsupported_reason(
                            subcategory, constraint.role
                        ),
                    )
                )
                continue
            kept.append(constraint)

        planar = request.planar_dimensions
        if planar is not None and not self._dimension_semantics.supports_planar(subcategory):
            dropped.append(
                DroppedConstraint(
                    role=None,
                    reason=UnsupportedDimensionReason.UNSUPPORTED_PRODUCT_GEOMETRY,
                )
            )
            planar = None

        kept_roles = {c.role for c in kept}
        return (
            request.model_copy(
                update={
                    "commerce_subcategory": subcategory,
                    "dimensions": tuple(kept),
                    "planar_dimensions": planar,
                }
            ),
            semantics.model_copy(
                update={
                    # They named the new type outright, so it is a requirement.
                    "subcategory": None if subcategory is None else _DEFAULT_STRENGTH,
                    "dimensions": tuple(
                        s for s in semantics.dimensions if s.role in kept_roles
                    ),
                    "planar_dimension": (
                        semantics.planar_dimension if planar is not None else None
                    ),
                }
            ),
            tuple(dropped),
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

        minimum = _decimal(refinement.min_amount) if refinement.min_amount else None
        maximum = _decimal(refinement.max_amount) if refinement.max_amount else None
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
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[SemanticPreference, ...]]:
        """One family's whole position at a time: requirement and preference.

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

        for refinement in refinements:
            family = refinement.family
            exact[family] = ()
            kept = [p for p in kept if p.family is not family]
            if refinement.op is AttributeRefinementOp.CLEAR:
                continue
            if refinement.op is AttributeRefinementOp.SET_REQUIREMENT:
                exact[family] = tuple(
                    self._approved(family, value.canonical_value)
                    for value in refinement.values
                )
                continue
            kept.extend(
                SemanticPreference(
                    family=family,
                    raw_value=value.raw_value,
                    canonical_value=value.canonical_value,
                    strength=ConstraintStrength.PREFERRED,
                )
                for value in refinement.values
            )

        return exact[AttributeFamily.COLOR], exact[AttributeFamily.STYLE], tuple(kept)

    def _approved(self, family: AttributeFamily, value: str | None) -> str:
        """The registry decides, not the schema that called a field canonical."""
        if value is None or not self._catalog_attributes.is_value(family, value):
            raise _defect(CompositionDefect.UNAPPROVED_ATTRIBUTE_VALUE)
        return value

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
