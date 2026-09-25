"""How many people a product type seats, as reviewed domain data.

Chairs, stools and single-seater sofas seat one, and the catalog records no
capacity for any of them - so a seat filter could only ever hide them all. A
sofa for one is a misreading, corrected into a change of product type.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    CustomerStateProposal,
    PreferenceProposal,
    PreferenceProposalOp,
)
from app.schemas.composition import ComposedSearch, CompositionDefect, CompositionFailed
from app.schemas.discovery import ProductSearchRequest, SeatingCapacityConstraint
from app.schemas.grounding import TurnFailureCode
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
from tests.unit.test_turn_coordinator import _coordinator, _preference, _turn
from tests.unit.test_turn_coordinator import _state as _turn_state

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


class _Decisions:
    """Each call returns the next decision, and records what it was told."""

    def __init__(self, *decisions: CustomerAgentDecision) -> None:
        self.decisions = decisions
        self.problems: list[tuple[str, ...]] = []

    async def decide(self, _input: Any, *, problems: Any = ()) -> CustomerAgentDecision:
        self.problems.append(tuple(problems))
        return self.decisions[min(len(self.problems), len(self.decisions)) - 1]


ONE_SEAT_SOFA = CustomerAgentDecision(
    action=AgentAction.REFINE_SEARCH,
    refinement=_seats(1, 1),
    state_proposal=CustomerStateProposal(
        customer_preferences=PreferenceProposal(
            op=PreferenceProposalOp.ADD, preferences=(_preference("Japandi"),)
        )
    ),
)
RETYPE = CustomerAgentDecision(action=AgentAction.REFINE_SEARCH, taxonomy_change_requested=True)
SINGLE_SEATERS = ResolvedSearch(
    request=ProductSearchRequest(
        commerce_category="seating", commerce_subcategory="single-seater-sofa"
    ),
    semantics=ConstraintSemantics(),
)


async def test_one_seat_sofa_is_corrected_into_a_change_of_type(seating: SeatingSemantics) -> None:
    decisions = _Decisions(ONE_SEAT_SOFA, RETYPE)
    coordinator, parts = _coordinator(
        RETYPE, decisions=decisions, seating=seating, interpretation=SINGLE_SEATERS
    )
    before = _turn_state(revision=1)

    result = await coordinator.run(_turn(before, "make them single seaters"))

    assert decisions.problems[0] == ()
    assert any("taxonomy_change_requested" in p for p in decisions.problems[1])
    # Only the corrected decision ran a search.
    assert len(parts["pipeline"].calls) == 1
    executed = parts["pipeline"].calls[0].request
    assert executed.commerce_subcategory == "single-seater-sofa"
    assert executed.seating_capacity is None
    assert result.grounding.failure is None
    # Nothing from the refused attempt was kept.
    assert result.state.customer_preferences.semantic_preferences == ()


async def test_a_second_misreading_ends_as_a_friendly_reply(seating: SeatingSemantics) -> None:
    decisions = _Decisions(ONE_SEAT_SOFA, ONE_SEAT_SOFA)
    coordinator, parts = _coordinator(
        RETYPE, decisions=decisions, seating=seating, interpretation=SINGLE_SEATERS
    )
    before = _turn_state(revision=1)

    result = await coordinator.run(_turn(before, "make them single seaters"))

    assert len(decisions.problems) == 2
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.REQUEST_NOT_UNDERSTOOD
    assert result.state == before
    assert parts["pipeline"].calls == []
