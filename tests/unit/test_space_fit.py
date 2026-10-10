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
from app.core.exceptions import LLMResponseInvalidError
from app.schemas.catalog_overview import CatalogOverview, SubcategoryShelf
from app.schemas.composition import ComposedSearch
from app.schemas.design import (
    DesignTask,
    InteriorDesignRequest,
    InteriorDesignResult,
    SpaceFitAdvice,
    SpaceFitRequest,
)
from app.schemas.discovery import DimensionConstraintKind
from app.schemas.product_brief import BriefSpaceOption
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
from app.services.product_brief import _with_space
from app.taxonomy.dimensions import DimensionRole

from tests.unit.test_query_understanding import _service
from tests.unit.test_turn_coordinator import (
    CONTEXT,
    SOFAS,
    FakeCapabilities,
    FakeDesign,
    _coordinator,
)

FIT = InteriorDesignResult(space_fit=SpaceFitAdvice(ratio=0.7, reason="room for a side table"))


class ShapedShelves(FakeCapabilities):
    """Sofas pass the shape rule: their longer side is their width."""

    def __init__(self, long_and_shallow: bool = True) -> None:
        super().__init__()
        self.long_and_shallow = long_and_shallow

    async def overview(self, context: Any) -> CatalogOverview:
        return CatalogOverview(
            store_id=50,
            shelves=(
                SubcategoryShelf(
                    commerce_category="seating",
                    commerce_subcategory="sofa",
                    active_count=40,
                    price_minimum=Decimal("900"),
                    price_maximum=Decimal("9000"),
                    long_and_shallow=self.long_and_shallow,
                ),
            ),
        )


def _engine(design: Any = None, *, on: bool = True, shaped: bool = True) -> Any:
    from app.schemas.agent_decision import AgentAction, CustomerAgentDecision

    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        design=design if design is not None else FakeDesign(FIT),
        capabilities=ShapedShelves(long_and_shallow=shaped),
        designer_space_fit=on,
    )
    return coordinator, parts


def _composed(lean: RankingLean | None) -> ComposedSearch:
    from app.schemas.agent_state import ActiveSearchState

    return ComposedSearch(
        candidate=ActiveSearchState(request=SOFAS, revision=1, lean=lean),
        resolved=ResolvedSearch(request=SOFAS, lean=lean),
    )


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


def test_a_tapped_space_is_the_space_too() -> None:
    tapped = _with_space(
        ResolvedSearch(request=SOFAS), BriefSpaceOption(key="space-2", max_cm=Decimal("240"))
    )

    assert tapped.request.dimensions == ()
    assert tapped.lean is not None and tapped.lean.space_cm == Decimal("240")
    assert not tapped.lean.space_fitted


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


async def test_the_designer_decides_the_proportion_and_code_the_width() -> None:
    coordinator, parts = _engine()

    composed, view = await coordinator._with_space_fit(
        _composed(RankingLean(space_cm=Decimal("400"))), _state(), CONTEXT
    )

    request: InteriorDesignRequest = parts["design"].requests[0]
    assert request.task is DesignTask.SPACE_FIT
    assert request.space_fit == SpaceFitRequest(
        commerce_subcategory="sofa", space_width_cm=Decimal("400")
    )
    lean = composed.resolved.lean
    assert lean is not None and lean.space_fitted and lean.size_target_cm == Decimal("280")
    assert composed.candidate.lean == lean
    assert view is not None and (view.space_cm, view.ideal_cm) == (Decimal("400"), Decimal("280"))


async def test_the_designer_is_asked_once_per_space() -> None:
    coordinator, parts = _engine()
    asked = RankingLean(space_cm=Decimal("400"), space_fitted=True, size_target_cm=Decimal("280"))

    composed, view = await coordinator._with_space_fit(_composed(asked), _state(), CONTEXT)

    assert parts["design"].requests == []
    assert composed.resolved.lean == asked and view is None


@pytest.mark.parametrize(
    ("design", "shaped"),
    [
        (FakeDesign(error=LLMResponseInvalidError(reason="no proportion")), True),
        (FakeDesign(FIT), False),
    ],
    ids=["designer_cannot_answer", "kind_not_long_and_shallow"],
)
async def test_without_an_answer_the_space_only_limits(design: Any, shaped: bool) -> None:
    coordinator, _ = _engine(design, shaped=shaped)

    composed, view = await coordinator._with_space_fit(
        _composed(RankingLean(space_cm=Decimal("400"))), _state(), CONTEXT
    )

    lean = composed.resolved.lean
    assert lean is not None and lean.space_fitted and lean.size_target_cm is None
    assert view is None


async def test_switched_off_nothing_is_asked() -> None:
    coordinator, parts = _engine(on=False)
    spaced = _composed(RankingLean(space_cm=Decimal("400")))

    composed, view = await coordinator._with_space_fit(spaced, _state(), CONTEXT)

    assert parts["design"].requests == []
    assert composed == spaced and view is None


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


def test_wider_than_the_space_comes_last_and_is_never_hidden() -> None:
    from tests.unit.test_designer_direction import _candidate, _order

    resolved = ResolvedSearch(
        request=SOFAS,
        lean=RankingLean(space_cm=Decimal("300"), size_target_cm=Decimal("210")),
    )
    cards = [
        _candidate(1, "Grey", long_cm="320"),
        _candidate(2, "Grey"),
        _candidate(3, "Grey", long_cm="290"),
        _candidate(4, "Grey", long_cm="215"),
    ]

    assert _order(resolved, cards) == [4, 3, 2, 1]


def test_a_space_chip_shows_and_its_cross_takes_the_space_away() -> None:
    from app.schemas.agent_state import ActiveSearchState
    from app.schemas.language import ReplyLanguage
    from app.services.product_brief import without_facet

    from tests.unit.test_product_brief import _builder

    spaced = ActiveSearchState(
        request=SOFAS,
        revision=1,
        lean=RankingLean(space_cm=Decimal("400"), space_fitted=True, size_target_cm=Decimal("280")),
    )
    builder, _ = _builder()

    chips = builder.chips(spaced, ReplyLanguage.EN)
    dropped = without_facet(spaced, "space")

    assert [c.label for c in chips if c.facet == "space"] == ["For a 400 cm space"]
    assert dropped is not None and dropped.lean is None


def test_tapping_a_kind_keeps_the_space_and_works_out_what_suits_the_new_kind() -> None:
    from app.schemas.product_brief import BriefKindOption
    from app.services.product_brief import _with_kind

    fitted = RankingLean(space_cm=Decimal("400"), space_fitted=True, size_target_cm=Decimal("280"))
    base = ResolvedSearch(request=SOFAS, lean=fitted)

    same = _with_kind(
        base,
        BriefKindOption(
            key="sofa:3", commerce_category="seating", commerce_subcategory="sofa", seats=3
        ),
    )
    other = _with_kind(
        base,
        BriefKindOption(
            key="sofa-set", commerce_category="seating", commerce_subcategory="sofa-set"
        ),
    )

    assert same.lean == fitted
    assert other.lean is not None and other.lean.space_cm == Decimal("400")
    assert not other.lean.space_fitted and other.lean.size_target_cm is None


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
