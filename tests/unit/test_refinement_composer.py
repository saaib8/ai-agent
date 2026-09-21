"""Composing the next candidate search, deterministically.

The distinction almost every test here turns on: an axis the customer did not
mention must come through untouched, and an axis they explicitly gave up must
come through empty. Without both, "show me cheaper ones" would quietly lose a
colour requirement, or "any style is fine" would quietly keep one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.agent_decision import (
    BlockingClarificationReason,
)
from app.schemas.agent_state import ActiveSearchState
from app.schemas.composition import (
    ComposedSearch,
    CompositionDefect,
    CompositionFailed,
    CompositionNeedsClarification,
)
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PlanarDimensionConstraint,
    PriceConstraint,
    ProductSearchRequest,
    ProductSort,
    SeatingCapacityConstraint,
)
from app.schemas.product_reference import PresentedOrdinal
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
    PriceRelation,
    ProposedAttributeValue,
    RefinementOp,
    RelativePriceRefinement,
    SearchRefinementDelta,
    SemanticIntentOp,
    SemanticIntentRefinement,
    SortRefinement,
)
from app.services.refinement_composer import SearchRefinementComposer
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes
from app.taxonomy.dimensions import DimensionRole, load_dimension_semantics
from app.taxonomy.registry import load_taxonomy

SAR = "SAR"
LOCKED = ConstraintStrength.LOCKED
PREFERRED = ConstraintStrength.PREFERRED
APPROXIMATE = ConstraintStrength.APPROXIMATE
WIDTH = DimensionRole.OVERALL_WIDTH
DEPTH = DimensionRole.DEPTH
SET = RefinementOp.SET
CLEAR = RefinementOp.CLEAR


@pytest.fixture(scope="module")
def composer() -> SearchRefinementComposer:
    taxonomy = load_taxonomy()
    return SearchRefinementComposer(
        load_catalog_attributes(), load_dimension_semantics(taxonomy=taxonomy)
    )


def _state(
    *,
    subcategory: str | None = "sofa",
    price: PriceConstraint | None = None,
    capacity: SeatingCapacityConstraint | None = None,
    dimensions: tuple[DimensionConstraint, ...] = (),
    planar: PlanarDimensionConstraint | None = None,
    colors: tuple[str, ...] = (),
    styles: tuple[str, ...] = (),
    preferences: tuple[SemanticPreference, ...] = (),
    intent: str | None = None,
    sort: ProductSort = ProductSort.DEFAULT,
    semantics: ConstraintSemantics | None = None,
    revision: int = 3,
) -> ActiveSearchState:
    return ActiveSearchState(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory=subcategory,
            price=price,
            seating_capacity=capacity,
            dimensions=dimensions,
            planar_dimensions=planar,
            colors_any_of=colors,
            styles_all_of=styles,
            sort=sort,
        ),
        semantics=semantics
        or ConstraintSemantics(
            subcategory=LOCKED if subcategory else None,
            price_max=LOCKED if price and price.max_amount else None,
            seating_min=LOCKED if capacity and capacity.min_capacity else None,
            seating_max=LOCKED if capacity and capacity.max_capacity else None,
            dimensions=tuple(
                DimensionConstraintSemantics(role=d.role, strength=LOCKED)
                for d in dimensions
            ),
            planar_dimension=PlanarDimensionSemantics(strength=LOCKED) if planar else None,
        ),
        semantic_preferences=preferences,
        semantic_intent=intent,
        revision=revision,
    )


def _ok(outcome: Any) -> ComposedSearch:
    assert isinstance(outcome, ComposedSearch), outcome
    return outcome


def _preference(family: AttributeFamily, value: str, canonical: str | None) -> SemanticPreference:
    return SemanticPreference(
        family=family, raw_value=value, canonical_value=canonical, strength=PREFERRED
    )


def _width(**kwargs: Any) -> DimensionConstraint:
    payload: dict[str, Any] = {
        "role": WIDTH,
        "kind": DimensionConstraintKind.MAX,
        "max_cm": Decimal("220"),
        "source_value": "220",
        "source_unit": "cm",
    }
    payload.update(kwargs)
    return DimensionConstraint(**payload)


# ══ A. price ════════════════════════════════════════════════════════════════


def test_an_omitted_price_is_preserved(composer: SearchRefinementComposer) -> None:
    price = PriceConstraint(currency=SAR, max_amount=Decimal("5000"))

    result = _ok(composer.refine(_state(price=price), SearchRefinementDelta()))

    assert result.candidate.request.price == price
    assert result.candidate.semantics.price_max is LOCKED


def test_a_price_set_replaces_the_bound(composer: SearchRefinementComposer) -> None:
    result = _ok(
        composer.refine(
            _state(price=PriceConstraint(currency=SAR, max_amount=Decimal("5000"))),
            SearchRefinementDelta(
                price=PriceRefinement(
                    op=PriceRefinementOp.SET, max_amount="3000", currency=SAR
                )
            ),
        )
    )

    assert result.candidate.request.price is not None
    assert result.candidate.request.price.max_amount == Decimal("3000")


def test_a_price_clear_removes_the_bound_and_its_strength(
    composer: SearchRefinementComposer,
) -> None:
    result = _ok(
        composer.refine(
            _state(price=PriceConstraint(currency=SAR, max_amount=Decimal("5000"))),
            SearchRefinementDelta(price=PriceRefinement(op=PriceRefinementOp.CLEAR)),
        )
    )

    assert result.candidate.request.price is None
    assert result.candidate.semantics.price_max is None


def test_an_explicit_currency_is_used(composer: SearchRefinementComposer) -> None:
    result = _ok(
        composer.refine(
            _state(),
            SearchRefinementDelta(
                price=PriceRefinement(
                    op=PriceRefinementOp.SET, max_amount="3000", currency="USD"
                )
            ),
        )
    )

    assert result.candidate.request.price is not None
    assert result.candidate.request.price.currency == "USD"


def test_the_active_search_currency_is_inherited(
    composer: SearchRefinementComposer,
) -> None:
    """"Under 3000" after "SAR 5,000 max" is SAR."""
    result = _ok(
        composer.refine(
            _state(price=PriceConstraint(currency=SAR, max_amount=Decimal("5000"))),
            SearchRefinementDelta(
                price=PriceRefinement(op=PriceRefinementOp.SET, max_amount="3000")
            ),
        )
    )

    assert result.candidate.request.price is not None
    assert result.candidate.request.price.currency == SAR


def test_no_currency_anywhere_asks_rather_than_invents(
    composer: SearchRefinementComposer,
) -> None:
    """No retailer-currency mapping exists, so there is nothing to fall back on."""
    outcome = composer.refine(
        _state(),
        SearchRefinementDelta(
            price=PriceRefinement(op=PriceRefinementOp.SET, max_amount="3000")
        ),
    )

    assert isinstance(outcome, CompositionNeedsClarification)
    assert outcome.reason is BlockingClarificationReason.MISSING_REFINEMENT_CURRENCY


def test_exclusivity_survives_composition(composer: SearchRefinementComposer) -> None:
    result = _ok(
        composer.refine(
            _state(),
            SearchRefinementDelta(
                price=PriceRefinement(
                    op=PriceRefinementOp.SET,
                    max_amount="5000",
                    currency=SAR,
                    max_exclusive=True,
                )
            ),
        )
    )

    assert result.candidate.request.price is not None
    assert result.candidate.request.price.max_exclusive is True


def test_an_unqualified_bound_is_locked(composer: SearchRefinementComposer) -> None:
    result = _ok(
        composer.refine(
            _state(),
            SearchRefinementDelta(
                price=PriceRefinement(
                    op=PriceRefinementOp.SET, max_amount="3000", currency=SAR
                )
            ),
        )
    )

    assert result.candidate.semantics.price_max is LOCKED


def test_a_softened_bound_keeps_its_strength(
    composer: SearchRefinementComposer,
) -> None:
    result = _ok(
        composer.refine(
            _state(),
            SearchRefinementDelta(
                price=PriceRefinement(
                    op=PriceRefinementOp.SET,
                    max_amount="3000",
                    currency=SAR,
                    max_strength=APPROXIMATE,
                )
            ),
        )
    )

    assert result.candidate.semantics.price_max is APPROXIMATE


def test_an_amount_that_is_not_a_number_is_a_defect(
    composer: SearchRefinementComposer,
) -> None:
    outcome = composer.refine(
        _state(),
        SearchRefinementDelta(
            price=PriceRefinement(
                op=PriceRefinementOp.SET, max_amount="lots", currency=SAR
            )
        ),
    )

    assert isinstance(outcome, CompositionFailed)
    assert outcome.defect is CompositionDefect.MALFORMED_AMOUNT


# ══ B. relative price is not this phase's work ══════════════════════════════


def test_a_relative_price_is_refused_as_a_defect_not_a_question(
    composer: SearchRefinementComposer,
) -> None:
    """Nobody can be asked to fix an unresolved reference price."""
    outcome = composer.refine(
        _state(),
        SearchRefinementDelta(
            price=PriceRefinement(
                op=PriceRefinementOp.SET_RELATIVE,
                relative=RelativePriceRefinement(
                    relation=PriceRelation.CHEAPER_THAN,
                    reference=PresentedOrdinal(position=2),
                ),
            )
        ),
    )

    assert isinstance(outcome, CompositionFailed)
    assert outcome.defect is CompositionDefect.RELATIVE_PRICE_NOT_RESOLVED


def test_the_composer_computes_no_relative_price(
    composer: SearchRefinementComposer,
) -> None:
    from pathlib import Path

    source = (
        Path(__file__).parents[2] / "app/services/refinement_composer.py"
    ).read_text()

    for forbidden in ("PriceRelation.", "percent", "hydrate", "get_by_ids"):
        assert forbidden not in source, forbidden


# ══ C. seating ══════════════════════════════════════════════════════════════


def test_an_omitted_capacity_is_preserved(composer: SearchRefinementComposer) -> None:
    capacity = SeatingCapacityConstraint.exactly(3)

    result = _ok(composer.refine(_state(capacity=capacity), SearchRefinementDelta()))

    assert result.candidate.request.seating_capacity == capacity


def test_a_capacity_set_replaces_it(composer: SearchRefinementComposer) -> None:
    result = _ok(
        composer.refine(
            _state(capacity=SeatingCapacityConstraint.exactly(3)),
            SearchRefinementDelta(
                seating_capacity=CapacityRefinement(op=SET, min_capacity=4)
            ),
        )
    )

    assert result.candidate.request.seating_capacity is not None
    assert result.candidate.request.seating_capacity.min_capacity == 4
    assert result.candidate.semantics.seating_min is LOCKED
    assert result.candidate.semantics.seating_max is None


def test_a_capacity_clear_removes_it(composer: SearchRefinementComposer) -> None:
    result = _ok(
        composer.refine(
            _state(capacity=SeatingCapacityConstraint.exactly(3)),
            SearchRefinementDelta(seating_capacity=CapacityRefinement(op=CLEAR)),
        )
    )

    assert result.candidate.request.seating_capacity is None
    assert result.candidate.semantics.seating_min is None
    assert result.candidate.semantics.seating_max is None


# ══ D. dimensions ═══════════════════════════════════════════════════════════


def test_a_dimension_set_converts_through_the_shared_units(
    composer: SearchRefinementComposer,
) -> None:
    result = _ok(
        composer.refine(
            _state(),
            SearchRefinementDelta(
                dimensions=(
                    DimensionRefinement(
                        op=SET,
                        role=WIDTH,
                        kind=DimensionConstraintKind.MAX,
                        max_value="2.1",
                        unit="m",
                    ),
                )
            ),
        )
    )

    constraint = result.candidate.request.dimensions[0]
    assert constraint.max_cm == Decimal("210.0")
    assert constraint.source_unit == "m"


def test_a_dimension_clear_removes_it_and_its_strength(
    composer: SearchRefinementComposer,
) -> None:
    result = _ok(
        composer.refine(
            _state(dimensions=(_width(),)),
            SearchRefinementDelta(
                dimensions=(DimensionRefinement(op=CLEAR, role=WIDTH),)
            ),
        )
    )

    assert result.candidate.request.dimensions == ()
    assert result.candidate.semantics.dimensions == ()


def test_the_unit_is_inherited_from_the_same_role(
    composer: SearchRefinementComposer,
) -> None:
    """"around 220 cm wide" then "under 210" keeps centimetres."""
    result = _ok(
        composer.refine(
            _state(dimensions=(_width(),)),
            SearchRefinementDelta(
                dimensions=(
                    DimensionRefinement(
                        op=SET,
                        role=WIDTH,
                        kind=DimensionConstraintKind.MAX,
                        max_value="210",
                    ),
                )
            ),
        )
    )

    constraint = result.candidate.request.dimensions[0]
    assert constraint.max_cm == Decimal("210")
    assert constraint.source_unit == "cm"


def test_a_unit_is_never_inherited_across_roles(
    composer: SearchRefinementComposer,
) -> None:
    """A bare depth cannot borrow the width's centimetres."""
    outcome = composer.refine(
        _state(dimensions=(_width(),)),
        SearchRefinementDelta(
            dimensions=(
                DimensionRefinement(
                    op=SET, role=DEPTH, kind=DimensionConstraintKind.MAX, max_value="95"
                ),
            )
        ),
    )

    assert isinstance(outcome, CompositionNeedsClarification)
    assert outcome.reason is BlockingClarificationReason.MISSING_DIMENSION_UNIT


def test_no_unit_anywhere_asks_rather_than_assuming_centimetres(
    composer: SearchRefinementComposer,
) -> None:
    outcome = composer.refine(
        _state(),
        SearchRefinementDelta(
            dimensions=(
                DimensionRefinement(
                    op=SET, role=WIDTH, kind=DimensionConstraintKind.MAX, max_value="210"
                ),
            )
        ),
    )

    assert isinstance(outcome, CompositionNeedsClarification)
    assert outcome.reason is BlockingClarificationReason.MISSING_DIMENSION_UNIT


def test_an_unrecognised_unit_is_not_guessed(
    composer: SearchRefinementComposer,
) -> None:
    outcome = composer.refine(
        _state(),
        SearchRefinementDelta(
            dimensions=(
                DimensionRefinement(
                    op=SET,
                    role=WIDTH,
                    kind=DimensionConstraintKind.MAX,
                    max_value="210",
                    unit="cubits",
                ),
            )
        ),
    )

    assert isinstance(outcome, CompositionNeedsClarification)


def test_measurements_and_their_strengths_stay_aligned(
    composer: SearchRefinementComposer,
) -> None:
    """The correspondence `ResolvedSearch` validates, maintained by construction."""
    result = _ok(
        composer.refine(
            _state(dimensions=(_width(),)),
            SearchRefinementDelta(
                dimensions=(
                    DimensionRefinement(op=CLEAR, role=WIDTH),
                    DimensionRefinement(
                        op=SET,
                        role=DEPTH,
                        kind=DimensionConstraintKind.MAX,
                        max_value="95",
                        unit="cm",
                        strength=APPROXIMATE,
                    ),
                )
            ),
        )
    )

    assert [c.role for c in result.candidate.request.dimensions] == [DEPTH]
    assert [s.role for s in result.candidate.semantics.dimensions] == [DEPTH]
    assert result.candidate.semantics.strength_for_dimension(DEPTH) is APPROXIMATE


def test_another_role_is_untouched_by_a_refinement(
    composer: SearchRefinementComposer,
) -> None:
    depth = DimensionConstraint(
        role=DEPTH,
        kind=DimensionConstraintKind.MAX,
        max_cm=Decimal("95"),
        source_value="95",
        source_unit="cm",
    )

    result = _ok(
        composer.refine(
            _state(dimensions=(_width(), depth)),
            SearchRefinementDelta(
                dimensions=(DimensionRefinement(op=CLEAR, role=WIDTH),)
            ),
        )
    )

    assert [c.role for c in result.candidate.request.dimensions] == [DEPTH]


# ══ E. planar ═══════════════════════════════════════════════════════════════


def _rug(**kwargs: Any) -> ActiveSearchState:
    return _state(
        subcategory="carpet",
        planar=PlanarDimensionConstraint(
            first_cm=Decimal("200"), second_cm=Decimal("300"), source_unit="cm"
        ),
        **kwargs,
    )


def test_a_planar_set_uses_its_explicit_unit(
    composer: SearchRefinementComposer,
) -> None:
    result = _ok(
        composer.refine(
            _state(subcategory="carpet"),
            SearchRefinementDelta(
                planar_dimensions=PlanarRefinement(
                    op=SET, first_value="2", second_value="3", unit="m"
                )
            ),
        )
    )

    planar = result.candidate.request.planar_dimensions
    assert planar is not None
    assert planar.sides == (Decimal("200"), Decimal("300"))


def test_a_planar_unit_is_inherited_from_a_planar_pair(
    composer: SearchRefinementComposer,
) -> None:
    result = _ok(
        composer.refine(
            _rug(),
            SearchRefinementDelta(
                planar_dimensions=PlanarRefinement(
                    op=SET, first_value="160", second_value="230"
                )
            ),
        )
    )

    planar = result.candidate.request.planar_dimensions
    assert planar is not None
    assert planar.source_unit == "cm"


def test_a_role_unit_cannot_seed_a_planar_pair(
    composer: SearchRefinementComposer,
) -> None:
    """A sofa's width says nothing about a rug's sides."""
    outcome = composer.refine(
        _state(subcategory="carpet", dimensions=(_width(),)),
        SearchRefinementDelta(
            planar_dimensions=PlanarRefinement(
                op=SET, first_value="160", second_value="230"
            )
        ),
    )

    assert isinstance(outcome, CompositionNeedsClarification)
    assert outcome.reason is BlockingClarificationReason.MISSING_DIMENSION_UNIT


def test_a_planar_unit_cannot_seed_a_role(
    composer: SearchRefinementComposer,
) -> None:
    outcome = composer.refine(
        _rug(),
        SearchRefinementDelta(
            dimensions=(
                DimensionRefinement(
                    op=SET, role=WIDTH, kind=DimensionConstraintKind.MAX, max_value="210"
                ),
            )
        ),
    )

    assert isinstance(outcome, CompositionNeedsClarification)


def test_a_planar_clear_removes_the_pair_and_its_strength(
    composer: SearchRefinementComposer,
) -> None:
    result = _ok(
        composer.refine(
            _rug(), SearchRefinementDelta(planar_dimensions=PlanarRefinement(op=CLEAR))
        )
    )

    assert result.candidate.request.planar_dimensions is None
    assert result.candidate.semantics.planar_dimension is None


# ══ F. colour and style ═════════════════════════════════════════════════════


def _attribute(
    op: AttributeRefinementOp, family: AttributeFamily, *values: tuple[str, str | None]
) -> AttributeRefinement:
    return AttributeRefinement(
        op=op,
        family=family,
        values=tuple(
            ProposedAttributeValue(raw_value=raw, canonical_value=canonical)
            for raw, canonical in values
        ),
    )


def test_a_strict_requirement_becomes_an_exact_filter(
    composer: SearchRefinementComposer,
) -> None:
    result = _ok(
        composer.refine(
            _state(),
            SearchRefinementDelta(
                attributes=(
                    _attribute(
                        AttributeRefinementOp.SET_REQUIREMENT,
                        AttributeFamily.COLOR,
                        ("only beige", "Beige"),
                    ),
                )
            ),
        )
    )

    assert result.candidate.request.colors_any_of == ("Beige",)
    assert result.candidate.semantic_preferences == ()


def test_ordinary_wanting_becomes_a_preference_not_a_filter(
    composer: SearchRefinementComposer,
) -> None:
    """Filtering it would discard products they never ruled out."""
    result = _ok(
        composer.refine(
            _state(),
            SearchRefinementDelta(
                attributes=(
                    _attribute(
                        AttributeRefinementOp.SET_PREFERENCE,
                        AttributeFamily.COLOR,
                        ("beige", "Beige"),
                    ),
                )
            ),
        )
    )

    assert result.candidate.request.colors_any_of == ()
    assert [p.canonical_value for p in result.candidate.semantic_preferences] == ["Beige"]
    assert result.candidate.semantic_preferences[0].strength is PREFERRED


def test_a_clear_removes_the_requirement_and_the_preference(
    composer: SearchRefinementComposer,
) -> None:
    """"I don't care about style" must leave no stale leaning behind."""
    state = _state(
        styles=("Modern",),
        preferences=(_preference(AttributeFamily.STYLE, "modern", "Modern"),),
    )

    result = _ok(
        composer.refine(
            state,
            SearchRefinementDelta(
                attributes=(
                    AttributeRefinement(
                        op=AttributeRefinementOp.CLEAR, family=AttributeFamily.STYLE
                    ),
                )
            ),
        )
    )

    assert result.candidate.request.styles_all_of == ()
    assert result.candidate.semantic_preferences == ()


def test_an_unapproved_canonical_value_is_refused_not_filtered(
    composer: SearchRefinementComposer,
) -> None:
    """The schema says a field is canonical; only the registry says it is."""
    outcome = composer.refine(
        _state(),
        SearchRefinementDelta(
            attributes=(
                _attribute(
                    AttributeRefinementOp.SET_REQUIREMENT,
                    AttributeFamily.COLOR,
                    ("neon", "Neon"),
                ),
            )
        ),
    )

    assert isinstance(outcome, CompositionFailed)
    assert outcome.defect is CompositionDefect.UNAPPROVED_ATTRIBUTE_VALUE


def test_a_requirement_without_a_canonical_value_is_refused(
    composer: SearchRefinementComposer,
) -> None:
    """"It must be warm neutral" names no approved colour."""
    outcome = composer.refine(
        _state(),
        SearchRefinementDelta(
            attributes=(
                _attribute(
                    AttributeRefinementOp.SET_REQUIREMENT,
                    AttributeFamily.COLOR,
                    ("warm neutral", None),
                ),
            )
        ),
    )

    assert isinstance(outcome, CompositionFailed)


def test_a_preference_keeps_an_out_of_vocabulary_value_verbatim(
    composer: SearchRefinementComposer,
) -> None:
    """"warm neutral" is preserved for ranking, not mapped onto Beige."""
    result = _ok(
        composer.refine(
            _state(),
            SearchRefinementDelta(
                attributes=(
                    _attribute(
                        AttributeRefinementOp.SET_PREFERENCE,
                        AttributeFamily.COLOR,
                        ("warm neutral", None),
                    ),
                )
            ),
        )
    )

    preference = result.candidate.semantic_preferences[0]
    assert preference.raw_value == "warm neutral"
    assert preference.canonical_value is None


def test_an_omitted_family_is_preserved(composer: SearchRefinementComposer) -> None:
    state = _state(colors=("Beige",), styles=("Modern",))

    result = _ok(composer.refine(state, SearchRefinementDelta()))

    assert result.candidate.request.colors_any_of == ("Beige",)
    assert result.candidate.request.styles_all_of == ("Modern",)


def test_the_families_are_independent(composer: SearchRefinementComposer) -> None:
    state = _state(colors=("Beige",), styles=("Modern",))

    result = _ok(
        composer.refine(
            state,
            SearchRefinementDelta(
                attributes=(
                    AttributeRefinement(
                        op=AttributeRefinementOp.CLEAR, family=AttributeFamily.COLOR
                    ),
                )
            ),
        )
    )

    assert result.candidate.request.colors_any_of == ()
    assert result.candidate.request.styles_all_of == ("Modern",)


def test_a_requirement_replaces_rather_than_narrows(
    composer: SearchRefinementComposer,
) -> None:
    """Adding Japandi to Modern would demand both and return nothing."""
    result = _ok(
        composer.refine(
            _state(styles=("Modern",)),
            SearchRefinementDelta(
                attributes=(
                    _attribute(
                        AttributeRefinementOp.SET_REQUIREMENT,
                        AttributeFamily.STYLE,
                        ("only japandi", "Japandi"),
                    ),
                )
            ),
        )
    )

    assert result.candidate.request.styles_all_of == ("Japandi",)


# ══ G/H. semantic intent and execution text ═════════════════════════════════


def test_semantic_intent_can_be_set_cleared_or_preserved(
    composer: SearchRefinementComposer,
) -> None:
    state = _state(intent="cosy")

    preserved = _ok(composer.refine(state, SearchRefinementDelta()))
    replaced = _ok(
        composer.refine(
            state,
            SearchRefinementDelta(
                semantic_intent=SemanticIntentRefinement(
                    op=SemanticIntentOp.SET, value="sleek"
                )
            ),
        )
    )
    cleared = _ok(
        composer.refine(
            state,
            SearchRefinementDelta(
                semantic_intent=SemanticIntentRefinement(op=SemanticIntentOp.CLEAR)
            ),
        )
    )

    assert preserved.candidate.semantic_intent == "cosy"
    assert replaced.candidate.semantic_intent == "sleek"
    assert cleared.candidate.semantic_intent is None


def test_a_refinement_executes_on_the_durable_intent(
    composer: SearchRefinementComposer,
) -> None:
    result = _ok(composer.refine(_state(intent="cosy"), SearchRefinementDelta()))

    assert result.resolved.semantic_text == "cosy"


def test_a_refinement_with_no_intent_executes_with_no_text(
    composer: SearchRefinementComposer,
) -> None:
    result = _ok(composer.refine(_state(), SearchRefinementDelta()))

    assert result.resolved.semantic_text is None


def test_a_new_search_executes_on_m7_wording(
    composer: SearchRefinementComposer,
) -> None:
    """M7 read this turn's words; that is what this search runs on."""
    resolved = ResolvedSearch(
        request=ProductSearchRequest(commerce_category="seating"),
        semantic_text="cosy modern sofa",
    )

    result = composer.seed_new_task(
        resolved,
        semantic_intent=SemanticIntentRefinement(
            op=SemanticIntentOp.SET, value="cosy"
        ),
    )

    assert result.resolved.semantic_text == "cosy modern sofa"
    assert result.candidate.semantic_intent == "cosy"


def test_a_durable_intent_is_never_concatenated_into_m7_wording(
    composer: SearchRefinementComposer,
) -> None:
    """"cosy" + "cosy modern sofa" would embed the word twice."""
    resolved = ResolvedSearch(
        request=ProductSearchRequest(commerce_category="seating"),
        semantic_text="cosy modern sofa",
    )

    result = composer.seed_new_task(
        resolved,
        semantic_intent=SemanticIntentRefinement(op=SemanticIntentOp.SET, value="cosy"),
    )

    assert result.resolved.semantic_text == "cosy modern sofa"
    assert "cosy cosy" not in (result.resolved.semantic_text or "")


def test_a_taxonomy_refinement_ignores_upstream_m7_wording(
    composer: SearchRefinementComposer,
) -> None:
    """A contextless call made to read "make them sectionals" must not
    replace the conversation's durable intent."""
    result = _ok(
        composer.refine_taxonomy(
            _state(intent="cosy"),
            commerce_category="seating",
            commerce_subcategory="sectional-sofa",
        )
    )

    assert result.resolved.semantic_text == "cosy"
    assert result.candidate.request.commerce_subcategory == "sectional-sofa"


def test_the_taxonomy_method_cannot_see_m7_wording_at_all(
    composer: SearchRefinementComposer,
) -> None:
    """Its input is a validated pair, so there is nothing else to leak."""
    import inspect

    parameters = set(inspect.signature(composer.refine_taxonomy).parameters)

    assert parameters == {"state", "commerce_category", "commerce_subcategory", "delta"}


# ══ I. sort ═════════════════════════════════════════════════════════════════


def test_sort_can_be_set_cleared_or_preserved(
    composer: SearchRefinementComposer,
) -> None:
    state = _state(sort=ProductSort.PRICE_DESC)

    preserved = _ok(composer.refine(state, SearchRefinementDelta()))
    replaced = _ok(
        composer.refine(
            state,
            SearchRefinementDelta(
                sort=SortRefinement(op=SET, value=ProductSort.PRICE_ASC)
            ),
        )
    )
    cleared = _ok(
        composer.refine(state, SearchRefinementDelta(sort=SortRefinement(op=CLEAR)))
    )

    assert preserved.candidate.request.sort is ProductSort.PRICE_DESC
    assert replaced.candidate.request.sort is ProductSort.PRICE_ASC
    assert cleared.candidate.request.sort is ProductSort.DEFAULT
