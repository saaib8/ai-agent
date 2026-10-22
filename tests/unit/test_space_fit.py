"""The space a piece must fit, and the width that suits it.

"My wall is 400 cm" orders, never filters: a room being designed may use a
piece differently. Pieces that fit come first and wider ones last; within
them, the designer gives a proportion, code turns it into a width, and the
cards are ordered by closeness to it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.composition import ComposedSearch
from app.schemas.design import (
    DesignTask,
    InteriorDesignRequest,
    InteriorDesignResult,
    SpaceFitAdvice,
    SpaceFitRequest,
)
from app.schemas.discovery import DimensionConstraintKind
from app.schemas.query import (
    CommerceInterpretation,
    DimensionInterpretation,
    RankingLean,
    ResolvedSearch,
    SpaceInterpretation,
)
from app.schemas.refinement import (
    DimensionRefinement,
    RefinementOp,
    SearchRefinementDelta,
    SpaceRefinement,
)
from app.taxonomy.dimensions import DimensionRole

from tests.unit.agents.fakes import SOFAS
from tests.unit.test_query_understanding import _service

FIT = InteriorDesignResult(space_fit=SpaceFitAdvice(ratio=0.7, reason="room for a side table"))


def _width(search: Any) -> Any:
    return next(c for c in search.request.dimensions if c.role is DimensionRole.OVERALL_WIDTH)


# ── where the space comes from ══════════════════════════════════════════════


async def test_a_wall_said_in_words_is_the_space_and_filters_nothing() -> None:
    service, _ = _service(
        CommerceInterpretation(
            commerce_category="seating",
            commerce_subcategory="sofa",
            space_width=SpaceInterpretation(value="4", unit="m"),
        )
    )

    outcome = await service.interpret("a sofa for my 4 m wall")

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.dimensions == ()
    assert outcome.lean is not None and outcome.lean.space_cm == Decimal("400")


async def test_their_own_width_for_the_piece_decides_and_the_space_adds_nothing() -> None:
    service, _ = _service(
        CommerceInterpretation(
            commerce_category="seating",
            commerce_subcategory="sofa",
            dimensions=[
                DimensionInterpretation(
                    role=DimensionRole.OVERALL_WIDTH,
                    kind=DimensionConstraintKind.MAX,
                    max_value="220",
                    unit="cm",
                )
            ],
            space_width=SpaceInterpretation(value="400", unit="cm"),
        )
    )

    outcome = await service.interpret("a sofa under 220 cm for my 400 cm wall")

    assert isinstance(outcome, ResolvedSearch)
    assert _width(outcome).max_cm == Decimal("220")
    assert outcome.lean is None


def _refine(state: Any, delta: SearchRefinementDelta) -> Any:
    from app.schemas.composition import ComposedSearch as Composed
    from app.services.refinement_composer import SearchRefinementComposer
    from app.taxonomy.attributes import load_catalog_attributes
    from app.taxonomy.dimensions import load_dimension_semantics
    from app.taxonomy.registry import load_taxonomy

    composer = SearchRefinementComposer(
        load_catalog_attributes(), load_dimension_semantics(taxonomy=load_taxonomy())
    )
    outcome = composer.refine(state, delta)
    assert isinstance(outcome, Composed)
    return outcome


def test_a_space_given_with_results_on_screen_is_asked_again() -> None:
    from app.schemas.agent_state import ActiveSearchState

    asked_before = RankingLean(
        space_cm=Decimal("300"), space_fitted=True, size_target_cm=Decimal("210")
    )
    state = ActiveSearchState(request=SOFAS, revision=1, lean=asked_before)

    composed = _refine(
        state, SearchRefinementDelta(space_width=SpaceRefinement(value="400", unit="cm"))
    )

    assert composed.resolved.request.dimensions == ()
    lean = composed.candidate.lean
    assert lean is not None and lean.space_cm == Decimal("400")
    assert not lean.space_fitted and lean.size_target_cm is None


def test_a_width_of_their_own_ends_the_space() -> None:
    from app.schemas.agent_state import ActiveSearchState

    spaced = _refine(
        ActiveSearchState(request=SOFAS, revision=1),
        SearchRefinementDelta(space_width=SpaceRefinement(value="400", unit="cm")),
    ).candidate

    composed = _refine(
        spaced,
        SearchRefinementDelta(
            dimensions=(
                DimensionRefinement(
                    op=RefinementOp.SET,
                    role=DimensionRole.OVERALL_WIDTH,
                    kind=DimensionConstraintKind.MAX,
                    max_value="220",
                    unit="cm",
                ),
            )
        ),
    )

    assert _width(composed.resolved).max_cm == Decimal("220")
    assert composed.candidate.lean is None


# ── what suits it ═══════════════════════════════════════════════════════════


def test_the_ideal_width_never_exceeds_the_space() -> None:
    with pytest.raises(ValueError):
        SpaceFitAdvice(ratio=1.2, reason="bigger than the wall")


def test_a_space_fit_carries_the_space_and_consults_no_catalog() -> None:
    with pytest.raises(ValueError, match="space"):
        InteriorDesignRequest(task=DesignTask.SPACE_FIT)
    with pytest.raises(ValueError, match="space"):
        InteriorDesignRequest(
            task=DesignTask.GENERAL_ADVICE,
            question="how big a sofa?",
            space_fit=SpaceFitRequest(commerce_subcategory="sofa", space_width_cm=Decimal("400")),
        )


def _state() -> Any:
    from app.schemas.agent_state import AgentStateV1

    return AgentStateV1()


def test_a_type_change_keeps_the_space_and_works_out_what_suits_it_again() -> None:
    from app.schemas.agent_state import ActiveSearchState
    from app.services.refinement_composer import SearchRefinementComposer
    from app.taxonomy.attributes import load_catalog_attributes
    from app.taxonomy.dimensions import load_dimension_semantics
    from app.taxonomy.registry import load_taxonomy

    fitted = RankingLean(space_cm=Decimal("400"), space_fitted=True, size_target_cm=Decimal("280"))
    composer = SearchRefinementComposer(
        load_catalog_attributes(), load_dimension_semantics(taxonomy=load_taxonomy())
    )

    outcome = composer.refine_taxonomy(
        ActiveSearchState(request=SOFAS, revision=1, lean=fitted),
        commerce_category="seating",
        commerce_subcategory="sectional-sofa",
    )

    assert isinstance(outcome, ComposedSearch)
    lean = outcome.candidate.lean
    assert lean is not None and lean.space_cm == Decimal("400")
    assert not lean.space_fitted and lean.size_target_cm is None
