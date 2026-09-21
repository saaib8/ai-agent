"""Seeding a new task, and the defaults that must not follow it around.

Two rules do most of the work here. A default may seed a **preference** and
never a filter, because a customer who generally likes Modern has not excluded
everything else. And defaults seed a new task **once** - so "any style is
fine" stays true for the rest of that task, instead of the persisted Modern
quietly walking back in on the next refinement.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.schemas.agent_decision import (
    CustomerStateProposal,
    PreferenceProposal,
    PreferenceProposalOp,
)
from app.schemas.agent_state import ActiveSearchState
from app.schemas.composition import ComposedSearch, CompositionFailed, NewTaskRequired
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PriceConstraint,
    ProductSearchRequest,
)
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
    SearchRefinementDelta,
)
from app.services.refinement_composer import SearchRefinementComposer
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes
from app.taxonomy.dimensions import DimensionRole, load_dimension_semantics
from app.taxonomy.registry import load_taxonomy

APP = Path(__file__).parents[2] / "app"
COLOR, STYLE = AttributeFamily.COLOR, AttributeFamily.STYLE
PREFERRED = ConstraintStrength.PREFERRED
LOCKED = ConstraintStrength.LOCKED


@pytest.fixture(scope="module")
def composer() -> SearchRefinementComposer:
    taxonomy = load_taxonomy()
    return SearchRefinementComposer(
        load_catalog_attributes(), load_dimension_semantics(taxonomy=taxonomy)
    )


def _pref(family: AttributeFamily, canonical: str) -> SemanticPreference:
    return SemanticPreference(
        family=family,
        raw_value=canonical.lower(),
        canonical_value=canonical,
        strength=PREFERRED,
    )


def _proposal(**kwargs: Any) -> CustomerStateProposal:
    return CustomerStateProposal(**kwargs)


def _add(*preferences: SemanticPreference) -> PreferenceProposal:
    return PreferenceProposal(op=PreferenceProposalOp.ADD, preferences=preferences)


def _m7(
    *, preferences: tuple[SemanticPreference, ...] = (), text: str | None = None
) -> ResolvedSearch:
    return ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating", commerce_subcategory="sofa"
        ),
        semantic_preferences=preferences,
        semantic_text=text,
    )


def _canonical(result: ComposedSearch, family: AttributeFamily) -> list[str | None]:
    return [
        p.canonical_value
        for p in result.candidate.semantic_preferences
        if p.family is family
    ]


# ══ J. four-level precedence ════════════════════════════════════════════════


def test_the_explicit_request_wins_over_every_default(
    composer: SearchRefinementComposer,
) -> None:
    result = composer.seed_new_task(
        _m7(preferences=(_pref(STYLE, "Japandi"),)),
        proposal=_proposal(customer_preferences=_add(_pref(STYLE, "Modern"))),
        room_preferences=(_pref(STYLE, "Scandinavian"),),
        customer_defaults=(_pref(STYLE, "Rustic"),),
    )

    assert _canonical(result, STYLE) == ["Japandi"]


def test_a_current_turn_proposal_beats_persisted_defaults(
    composer: SearchRefinementComposer,
) -> None:
    """"I usually prefer Modern. Show me sofas." - both in one turn."""
    result = composer.seed_new_task(
        _m7(),
        proposal=_proposal(customer_preferences=_add(_pref(STYLE, "Modern"))),
        room_preferences=(_pref(STYLE, "Scandinavian"),),
        customer_defaults=(_pref(STYLE, "Rustic"),),
    )

    assert _canonical(result, STYLE) == ["Modern"]


def test_a_room_project_preference_beats_a_customer_default(
    composer: SearchRefinementComposer,
) -> None:
    result = composer.seed_new_task(
        _m7(),
        room_preferences=(_pref(STYLE, "Scandinavian"),),
        customer_defaults=(_pref(STYLE, "Rustic"),),
    )

    assert _canonical(result, STYLE) == ["Scandinavian"]


def test_a_customer_default_seeds_when_nothing_else_speaks(
    composer: SearchRefinementComposer,
) -> None:
    result = composer.seed_new_task(_m7(), customer_defaults=(_pref(STYLE, "Rustic"),))

    assert _canonical(result, STYLE) == ["Rustic"]


def test_each_axis_is_resolved_independently(
    composer: SearchRefinementComposer,
) -> None:
    """A room's colour can seed while this turn's request owns the style."""
    result = composer.seed_new_task(
        _m7(preferences=(_pref(STYLE, "Japandi"),)),
        room_preferences=(_pref(COLOR, "Beige"),),
        customer_defaults=(_pref(COLOR, "Ruby"), _pref(STYLE, "Rustic")),
    )

    assert _canonical(result, STYLE) == ["Japandi"]
    assert _canonical(result, COLOR) == ["Beige"]


def test_a_removal_proposal_seeds_nothing(composer: SearchRefinementComposer) -> None:
    result = composer.seed_new_task(
        _m7(),
        proposal=_proposal(
            customer_preferences=PreferenceProposal(
                op=PreferenceProposalOp.REMOVE, preferences=(_pref(STYLE, "Modern"),)
            )
        ),
        customer_defaults=(_pref(STYLE, "Rustic"),),
    )

    assert _canonical(result, STYLE) == ["Rustic"]


def test_a_seeded_default_is_a_preference_never_a_filter(
    composer: SearchRefinementComposer,
) -> None:
    """Filtering on it would hide products the customer never excluded."""
    result = composer.seed_new_task(_m7(), customer_defaults=(_pref(STYLE, "Modern"),))

    assert result.candidate.request.styles_all_of == ()
    assert result.candidate.request.colors_any_of == ()
    assert _canonical(result, STYLE) == ["Modern"]


def test_a_seeded_default_is_recorded_as_a_leaning(
    composer: SearchRefinementComposer,
) -> None:
    result = composer.seed_new_task(_m7(), customer_defaults=(_pref(STYLE, "Modern"),))

    assert result.candidate.semantic_preferences[0].strength is PREFERRED


def test_seeding_creates_no_structured_constraint(
    composer: SearchRefinementComposer,
) -> None:
    """Price, capacity, measurements and sort come only from the request."""
    result = composer.seed_new_task(
        _m7(),
        proposal=_proposal(design_preferences=_add(_pref(COLOR, "Beige"))),
        room_preferences=(_pref(STYLE, "Scandinavian"),),
        customer_defaults=(_pref(COLOR, "Ruby"),),
    )

    request = result.candidate.request
    assert request.price is None
    assert request.seating_capacity is None
    assert request.dimensions == ()
    assert request.planar_dimensions is None
    assert request.sort.value == "default"


def test_the_explicit_requests_own_strength_is_not_rewritten(
    composer: SearchRefinementComposer,
) -> None:
    """A locked colour the customer stated stays locked; only seeds are leanings."""
    strict = SemanticPreference(
        family=COLOR, raw_value="beige", canonical_value="Beige", strength=LOCKED
    )

    result = composer.seed_new_task(_m7(preferences=(strict,)))

    assert result.candidate.semantic_preferences[0].strength is LOCKED


# ══ K/L. scope separation and explicit clear ════════════════════════════════


def test_seeding_is_a_copy_not_a_transfer(
    composer: SearchRefinementComposer,
) -> None:
    """Clearing this search's style must not touch the customer's own."""
    defaults = (_pref(STYLE, "Modern"),)

    seeded = composer.seed_new_task(_m7(), customer_defaults=defaults)
    cleared = composer.refine(
        seeded.candidate,
        SearchRefinementDelta(
            attributes=(
                AttributeRefinement(op=AttributeRefinementOp.CLEAR, family=STYLE),
            )
        ),
    )

    assert isinstance(cleared, ComposedSearch)
    assert cleared.candidate.semantic_preferences == ()
    assert defaults[0].canonical_value == "Modern"  # the source is untouched


def test_a_cleared_axis_stays_clear_through_later_refinements(
    composer: SearchRefinementComposer,
) -> None:
    """"Any style is fine", then "under 3000" - Modern does not return."""
    seeded = composer.seed_new_task(_m7(), customer_defaults=(_pref(STYLE, "Modern"),))
    cleared = composer.refine(
        seeded.candidate,
        SearchRefinementDelta(
            attributes=(
                AttributeRefinement(op=AttributeRefinementOp.CLEAR, family=STYLE),
            )
        ),
    )
    assert isinstance(cleared, ComposedSearch)

    later = composer.refine(cleared.candidate, SearchRefinementDelta())

    assert isinstance(later, ComposedSearch)
    assert later.candidate.semantic_preferences == ()


def test_the_same_default_may_seed_a_genuinely_new_task(
    composer: SearchRefinementComposer,
) -> None:
    """They cleared it for the sofa search, not for ever."""
    defaults = (_pref(STYLE, "Modern"),)
    composer.seed_new_task(_m7(), customer_defaults=defaults)

    lamps = composer.seed_new_task(
        ResolvedSearch(request=ProductSearchRequest(commerce_category="lighting")),
        customer_defaults=defaults,
    )

    assert _canonical(lamps, STYLE) == ["Modern"]


def test_refinement_never_consults_defaults(
    composer: SearchRefinementComposer,
) -> None:
    """The composer's refine path takes no defaults at all."""
    import inspect

    parameters = set(inspect.signature(composer.refine).parameters)

    assert parameters == {"state", "delta"}


# ══ M. taxonomy refinement ══════════════════════════════════════════════════


def _sofa_state(**kwargs: Any) -> ActiveSearchState:
    dimensions = kwargs.pop("dimensions", ())
    planar = kwargs.pop("planar", None)
    return ActiveSearchState(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sofa",
            price=PriceConstraint(currency="SAR", max_amount=Decimal("5000")),
            colors_any_of=("Beige",),
            styles_all_of=("Modern",),
            dimensions=dimensions,
            planar_dimensions=planar,
            **kwargs,
        ),
        semantics=ConstraintSemantics(
            subcategory=LOCKED,
            price_max=LOCKED,
            dimensions=tuple(
                DimensionConstraintSemantics(role=d.role, strength=LOCKED)
                for d in dimensions
            ),
            planar_dimension=PlanarDimensionSemantics(strength=LOCKED) if planar else None,
        ),
        semantic_intent="cosy",
        revision=4,
    )


def test_a_same_family_change_preserves_compatible_constraints(
    composer: SearchRefinementComposer,
) -> None:
    """Modern, Beige and the budget survive "make them sectionals"."""
    result = composer.refine_taxonomy(
        _sofa_state(),
        commerce_category="seating",
        commerce_subcategory="sectional-sofa",
    )

    assert isinstance(result, ComposedSearch)
    request = result.candidate.request
    assert request.commerce_subcategory == "sectional-sofa"
    assert request.styles_all_of == ("Modern",)
    assert request.colors_any_of == ("Beige",)
    assert request.price is not None and request.price.max_amount is not None
    assert result.dropped_constraints == ()


def test_a_different_family_is_a_new_task(composer: SearchRefinementComposer) -> None:
    """Someone asking for tables has not asked for seating."""
    result = composer.refine_taxonomy(
        _sofa_state(), commerce_category="dining", commerce_subcategory="dining-table"
    )

    assert isinstance(result, NewTaskRequired)
    assert result.commerce_category == "dining"


def test_an_incompatible_measurement_is_dropped_and_reported(
    composer: SearchRefinementComposer,
) -> None:
    """A sofa's width has no established meaning for a sectional."""
    width = DimensionConstraint(
        role=DimensionRole.OVERALL_WIDTH,
        kind=DimensionConstraintKind.MAX,
        max_cm=Decimal("220"),
        source_value="220",
        source_unit="cm",
    )

    result = composer.refine_taxonomy(
        _sofa_state(dimensions=(width,)),
        commerce_category="seating",
        commerce_subcategory="sectional-sofa",
    )

    assert isinstance(result, ComposedSearch)
    assert result.candidate.request.dimensions == ()
    assert len(result.dropped_constraints) == 1
    assert result.dropped_constraints[0].role is DimensionRole.OVERALL_WIDTH


def test_a_dropped_measurement_takes_its_strength_with_it(
    composer: SearchRefinementComposer,
) -> None:
    """A strength with no measurement would break the correspondence."""
    width = DimensionConstraint(
        role=DimensionRole.OVERALL_WIDTH,
        kind=DimensionConstraintKind.MAX,
        max_cm=Decimal("220"),
        source_value="220",
        source_unit="cm",
    )

    result = composer.refine_taxonomy(
        _sofa_state(dimensions=(width,)),
        commerce_category="seating",
        commerce_subcategory="sectional-sofa",
    )

    assert isinstance(result, ComposedSearch)
    assert result.candidate.semantics.dimensions == ()


def test_a_supported_measurement_survives_the_change(
    composer: SearchRefinementComposer,
) -> None:
    """The guard is worthless if every measurement were dropped."""
    height = DimensionConstraint(
        role=DimensionRole.HEIGHT,
        kind=DimensionConstraintKind.MAX,
        max_cm=Decimal("85"),
        source_value="85",
        source_unit="cm",
    )
    state = _sofa_state(dimensions=(height,))
    assert state.request.dimensions

    result = composer.refine_taxonomy(
        state, commerce_category="seating", commerce_subcategory="sofa"
    )

    assert isinstance(result, ComposedSearch)
    assert [c.role for c in result.candidate.request.dimensions] == [DimensionRole.HEIGHT]
    assert result.dropped_constraints == ()


def test_the_new_product_type_is_recorded_as_a_requirement(
    composer: SearchRefinementComposer,
) -> None:
    """They named it outright, so it is not a soft preference."""
    result = composer.refine_taxonomy(
        _sofa_state(),
        commerce_category="seating",
        commerce_subcategory="sectional-sofa",
    )

    assert isinstance(result, ComposedSearch)
    assert result.candidate.semantics.subcategory is LOCKED


def test_a_taxonomy_change_can_carry_a_delta(
    composer: SearchRefinementComposer,
) -> None:
    result = composer.refine_taxonomy(
        _sofa_state(),
        commerce_category="seating",
        commerce_subcategory="sectional-sofa",
        delta=SearchRefinementDelta(
            attributes=(
                AttributeRefinement(op=AttributeRefinementOp.CLEAR, family=STYLE),
            )
        ),
    )

    assert isinstance(result, ComposedSearch)
    assert result.candidate.request.styles_all_of == ()
    assert result.candidate.request.commerce_subcategory == "sectional-sofa"


# ══ N. revision ═════════════════════════════════════════════════════════════


def test_the_candidate_carries_the_revision_unchanged(
    composer: SearchRefinementComposer,
) -> None:
    """Changing criteria is not executing a search."""
    result = composer.refine(_sofa_state(), SearchRefinementDelta())

    assert isinstance(result, ComposedSearch)
    assert result.candidate.revision == 4


def test_a_taxonomy_change_carries_the_revision_unchanged(
    composer: SearchRefinementComposer,
) -> None:
    result = composer.refine_taxonomy(
        _sofa_state(),
        commerce_category="seating",
        commerce_subcategory="sectional-sofa",
    )

    assert isinstance(result, ComposedSearch)
    assert result.candidate.revision == 4


def test_a_new_task_starts_with_nothing_committed(
    composer: SearchRefinementComposer,
) -> None:
    result = composer.seed_new_task(_m7())

    assert result.candidate.revision == 0


def test_the_composer_never_commits_anything() -> None:
    source = (APP / "services/refinement_composer.py").read_text()

    assert "commit_search_results" not in source
    assert "apply_update" not in source


def test_refining_without_a_search_is_a_defect(
    composer: SearchRefinementComposer,
) -> None:
    outcome = composer.refine(None, SearchRefinementDelta())

    assert isinstance(outcome, CompositionFailed)
    assert outcome.defect.value == "no_active_search"


# ══ O. purity and phase boundary ════════════════════════════════════════════

PURE_MODULES = ("services/refinement_composer.py", "schemas/composition.py")


@pytest.mark.parametrize("name", PURE_MODULES)
def test_composition_performs_no_io(name: str) -> None:
    """Parsed, not grepped: a docstring mentioning PostgreSQL is not a query."""
    tree = ast.parse((APP / name).read_text())

    for node in ast.walk(tree):
        assert not isinstance(node, ast.AsyncFunctionDef), name
        assert not isinstance(node, ast.Await), name
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots = {(node.module or "").split(".")[0]}
        else:
            continue
        assert not roots & {
            "openai",
            "sqlalchemy",
            "httpx",
            "requests",
            "redis",
            "boto3",
            "pinecone",
        }, f"{name}: {roots}"


@pytest.mark.parametrize("name", PURE_MODULES)
def test_composition_imports_no_repository_or_integration(name: str) -> None:
    tree = ast.parse((APP / name).read_text())

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        assert "app.repositories" not in module, name
        assert "app.integrations" not in module, name


def test_the_composer_loads_no_registry_from_disk() -> None:
    """Registries are injected, so a test can substitute one."""
    source = (APP / "services/refinement_composer.py").read_text()

    for forbidden in ("load_taxonomy", "load_catalog_attributes", "load_dimension", "open("):
        assert forbidden not in source, forbidden


def test_the_composer_knows_nothing_of_the_layers_above_it() -> None:
    """Purity is what makes it testable without a database or a provider.

    The whole M11B-3 stack now exists, so the meaningful guard is no longer
    "these have not been built" but "composition does not reach for them".
    """
    source = (APP / "services/refinement_composer.py").read_text()

    for symbol in (
        "ProductSearchPipeline",
        "presentation_limit",
        "ProductReferenceResolver",
        "RelativePriceResolver",
        "ProductComparisonService",
        "SimilarSearchBuilder",
        "CustomerTurnCoordinator",
    ):
        assert symbol not in source, symbol
