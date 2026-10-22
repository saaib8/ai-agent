"""How many people a product type seats, as reviewed domain data.

Chairs, stools and single-seater sofas seat one, and the catalog records no
capacity for any of them - so a seat filter could only ever hide them all. A
sofa for one is a misreading, corrected into a change of product type.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
)
from app.schemas.composition import ComposedSearch, CompositionDefect, CompositionFailed
from app.schemas.discovery import ProductSearchRequest, SeatingCapacityConstraint
from app.schemas.query import ConstraintSemantics, ConstraintStrength, ResolvedSearch
from app.schemas.refinement import (
    CapacityRefinement,
    PriceRefinement,
    PriceRefinementOp,
    RefinementOp,
    SearchRefinementDelta,
)
from app.services.refinement_composer import SearchRefinementComposer
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.seating import SeatingSemantics, load_seating_semantics

from tests.unit.test_refinement_composer import _state

ONE = SeatingCapacityConstraint(min_capacity=1, max_capacity=1)
FOUR = SeatingCapacityConstraint(min_capacity=4, max_capacity=4)
LOCKED = ConstraintStrength.LOCKED


@pytest.fixture(scope="module")
def seating() -> SeatingSemantics:
    return load_seating_semantics(taxonomy=load_taxonomy())


@pytest.fixture(scope="module")
def composer(seating: SeatingSemantics) -> SearchRefinementComposer:
    taxonomy = load_taxonomy()
    return SearchRefinementComposer(
        load_catalog_attributes(), load_dimension_semantics(taxonomy=taxonomy), seating
    )


def _seats(minimum: int | None, maximum: int | None) -> SearchRefinementDelta:
    return SearchRefinementDelta(
        seating_capacity=CapacityRefinement(
            op=RefinementOp.SET, min_capacity=minimum, max_capacity=maximum
        )
    )


CHEAPER = SearchRefinementDelta(
    price=PriceRefinement(op=PriceRefinementOp.SET, max_amount="3000", currency="SAR")
)


# ── the registry ────────────────────────────────────────────────────────────


def test_the_reviewed_file_loads_against_the_taxonomy(seating: SeatingSemantics) -> None:
    assert seating.seats_one("single-seater-sofa")
    assert seating.seats_several("sofa")
    # Unconfirmed, so deliberately in neither list.
    assert not seating.seats_one("recliner") and not seating.seats_several("recliner")
    assert not seating.seats_one(None)


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (
            "version: v1\nimplied_capacity: {chair: 1}\nmulti_seat: [chair]\n",
            "cannot seat one and several",
        ),
        ("version: v1\nimplied_capacity: {not-a-type: 1}\n", "not an approved seating"),
        ("version: v1\nimplied_capacity: {console: 1}\n", "not an approved seating"),
        ("version: v1\nimplied_capacity: {chair: 1}\nmulti_seat: sofa\n", "must be a list"),
        (
            "version: v1\nimplied_capacity: {chair: 1}\nmulti_seat: [sofa, sofa]\n",
            "repeats a value",
        ),
        ("implied_capacity: {chair: 1}\n", "'version'"),
        ("- chair\n", "top level must be a mapping"),
        ("version: [\n", "not valid YAML"),
    ],
)
def test_a_malformed_file_stops_startup(tmp_path: Path, content: str, problem: str) -> None:
    path = tmp_path / "seating.yaml"
    path.write_text(content)

    with pytest.raises(TaxonomyConfigurationError) as raised:
        load_seating_semantics(path, taxonomy=load_taxonomy())

    assert problem in str(raised.value.context)


def test_a_missing_file_stops_startup(tmp_path: Path) -> None:
    with pytest.raises(TaxonomyConfigurationError):
        load_seating_semantics(tmp_path / "absent.yaml", taxonomy=load_taxonomy())


# ── one-seat types carry no seat filter ─────────────────────────────────────


def test_a_seat_count_never_follows_the_customer_to_a_one_seat_type(
    composer: SearchRefinementComposer,
) -> None:
    """Sofas for four, then armchairs: every chair would be hidden."""
    result = composer.refine_taxonomy(
        _state(capacity=FOUR), commerce_category="seating", commerce_subcategory="chair"
    )

    assert isinstance(result, ComposedSearch)
    assert result.candidate.request.seating_capacity is None
    assert result.candidate.semantics.seating_min is None
    assert result.candidate.semantics.seating_max is None
    assert result.resolved.request.seating_capacity is None


def test_a_seat_count_on_a_one_seat_type_is_never_filtered(
    composer: SearchRefinementComposer,
) -> None:
    result = composer.refine(_state(subcategory="single-seater-sofa"), _seats(1, 1))

    assert isinstance(result, ComposedSearch)
    assert result.candidate.request.seating_capacity is None


def test_a_new_one_seat_search_carries_no_seat_filter(
    composer: SearchRefinementComposer,
) -> None:
    resolved = ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="single-seater-sofa",
            seating_capacity=ONE,
        ),
        semantics=ConstraintSemantics(subcategory=LOCKED, seating_min=LOCKED, seating_max=LOCKED),
    )

    result = composer.seed_new_task(resolved)

    assert result.resolved.request.seating_capacity is None
    assert result.resolved.semantics.seating_min is None


def test_an_unconfirmed_type_keeps_its_seat_count(composer: SearchRefinementComposer) -> None:
    result = composer.refine(_state(subcategory="recliner"), _seats(1, 1))

    assert isinstance(result, ComposedSearch)
    assert result.candidate.request.seating_capacity == ONE


# ── one seat of a multi-seat type is a misreading ───────────────────────────


def test_one_seat_on_a_sofa_is_refused(composer: SearchRefinementComposer) -> None:
    result = composer.refine(_state(), _seats(1, 1))

    assert isinstance(result, CompositionFailed)
    assert result.defect is CompositionDefect.ONE_SEAT_ON_MULTI_SEAT_TYPE


def test_a_type_change_into_a_sofa_type_carrying_one_seat_is_refused(
    composer: SearchRefinementComposer,
) -> None:
    result = composer.refine_taxonomy(
        _state(capacity=ONE), commerce_category="seating", commerce_subcategory="sofa-set"
    )

    assert isinstance(result, CompositionFailed)


def test_at_least_one_seat_is_not_a_misreading(composer: SearchRefinementComposer) -> None:
    result = composer.refine(_state(), _seats(1, None))

    assert isinstance(result, ComposedSearch)


def test_a_saved_one_seat_sofa_search_stays_refinable(
    composer: SearchRefinementComposer,
) -> None:
    """An older session or a misread new search must not lock the customer out."""
    result = composer.refine(_state(capacity=ONE), CHEAPER)

    assert isinstance(result, ComposedSearch)
    assert result.candidate.request.price is not None


def test_naming_the_same_type_again_is_not_a_type_change(
    composer: SearchRefinementComposer,
) -> None:
    result = composer.refine_taxonomy(
        _state(capacity=ONE),
        commerce_category="seating",
        commerce_subcategory="sofa",
        delta=CHEAPER,
    )

    assert isinstance(result, ComposedSearch)


def test_without_the_registry_nothing_changes() -> None:
    taxonomy = load_taxonomy()
    plain = SearchRefinementComposer(
        load_catalog_attributes(), load_dimension_semantics(taxonomy=taxonomy)
    )

    one_seat_sofa = plain.refine(_state(), _seats(1, 1))
    chair = plain.refine_taxonomy(
        _state(capacity=FOUR), commerce_category="seating", commerce_subcategory="chair"
    )

    assert isinstance(one_seat_sofa, ComposedSearch)
    assert isinstance(chair, ComposedSearch)
    assert chair.candidate.request.seating_capacity == FOUR


# ── the correction, through the turn engine ─────────────────────────────────


RETYPE = CustomerAgentDecision(action=AgentAction.REFINE_SEARCH, taxonomy_change_requested=True)
