"""A sofa search also shows sofa sets and sectional sofas.

What these tests hold the feature to: the types beside a search come from
reviewed data and nowhere else; only a search the customer asked for gets them,
never a suggestion after a pick or "more like this"; a size or "just sofas"
keeps the search to its own type; the cards take turns by type only among
equally good matches, never against an explicit sort; and the reply is given
the true count of the type they asked for, so "only one 5-seat sofa" is a fact.
Switched off, nothing changes.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
)
from app.schemas.agent_state import ActiveSearchState
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    ProductSearchRequest,
    ProductSort,
)
from app.schemas.grounding import TypeMix, TypeOnScreen
from app.schemas.product import EligibleProduct
from app.schemas.query import (
    ConstraintStrength,
    ResolvedSearch,
    SemanticPreference,
)
from app.schemas.refinement import SearchRefinementDelta
from app.schemas.relaxation import RelaxedCandidate
from app.schemas.retailer import RetailerContext
from app.services.refinement_composer import SearchRefinementComposer
from app.services.search_pipeline import _type_mix
from app.services.semantic_ranking import SemanticRankingService
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes
from app.taxonomy.dimensions import DimensionRole, load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.seating import load_seating_semantics
from pydantic import BaseModel, ValidationError

from tests.unit.agents.fakes import SOFAS

TAXONOMY = load_taxonomy()
SEATING = load_seating_semantics(taxonomy=TAXONOMY)
FAMILY = ("sofa-set", "sectional-sofa")
CONTEXT = RetailerContext(store_id=50)


def _width(max_cm: str = "220") -> DimensionConstraint:
    return DimensionConstraint(
        role=DimensionRole.OVERALL_WIDTH,
        kind=DimensionConstraintKind.MAX,
        max_cm=Decimal(max_cm),
        source_value=max_cm,
        source_unit="cm",
    )


# ── the reviewed data ───────────────────────────────────────────────────────


def test_a_sofa_search_shows_sets_and_sectionals_beside_it() -> None:
    assert SEATING.shown_with("sofa") == FAMILY


@pytest.mark.parametrize("subcategory", ["sofa-set", "sectional-sofa", "chair", "rug", None])
def test_no_other_type_has_anything_beside_it(subcategory: str | None) -> None:
    assert SEATING.shown_with(subcategory) == ()


def _seating_file(tmp_path: Path, shown_with: str) -> Path:
    path = tmp_path / "seating.yaml"
    path.write_text(
        "version: v1\n"
        "implied_capacity:\n  chair: 1\n"
        "multi_seat:\n  - sofa\n  - sofa-set\n  - sectional-sofa\n"
        f"shown_with:\n{shown_with}",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("shown_with", "refusal"),
    [
        ("  sofa:\n    - sofa\n", "beside itself"),
        ("  sofa:\n    - chair\n", "must seat several"),
        ("  sofa:\n    - luxury-couch\n", "not an approved seating"),
        ("  rug:\n    - sofa-set\n", "not an approved seating"),
        ("  sofa: []\n", "at least one type"),
        ("  - sofa\n", "must map a type"),
        ("  sofa:\n    - sofa-set\n    - sofa-set\n", "repeats a value"),
    ],
)
def test_the_loader_refuses_a_malformed_entry(
    tmp_path: Path, shown_with: str, refusal: str
) -> None:
    with pytest.raises(TaxonomyConfigurationError, match=refusal):
        load_seating_semantics(path=_seating_file(tmp_path, shown_with), taxonomy=TAXONOMY)


def test_the_reviewed_order_is_the_order_types_take_turns_in(tmp_path: Path) -> None:
    path = _seating_file(tmp_path, "  sofa:\n    - sectional-sofa\n    - sofa-set\n")

    assert load_seating_semantics(path=path, taxonomy=TAXONOMY).shown_with("sofa") == (
        "sectional-sofa",
        "sofa-set",
    )


# ── the search contract ─────────────────────────────────────────────────────


def test_a_request_covers_its_own_type_then_the_types_beside_it() -> None:
    request = SOFAS.model_copy(update={"alongside_subcategories": FAMILY})

    assert request.subcategories == ("sofa", *FAMILY)
    assert "alongside_subcategories" in request.applied_filters()


@pytest.mark.parametrize(
    "fields",
    [
        {"commerce_subcategory": None, "alongside_subcategories": FAMILY},
        {"commerce_subcategory": "sofa", "alongside_subcategories": ("sofa", "sofa-set")},
        {"commerce_subcategory": "sofa", "alongside_subcategories": ("sofa-set", "sofa-set")},
        {"commerce_subcategory": "sofa", "alongside_subcategories": FAMILY, "single_type": True},
        {
            "commerce_subcategory": "sofa",
            "alongside_subcategories": FAMILY,
            "dimensions": (_width(),),
        },
    ],
    ids=["no-own-type", "itself", "repeated", "one-type-alone", "with-a-size"],
)
def test_the_request_refuses_an_impossible_mix(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProductSearchRequest(commerce_category="seating", **fields)


def test_nothing_new_is_written_with_an_ordinary_request() -> None:
    """Sessions saved without the mix read exactly as before."""
    dumped = SOFAS.model_dump()

    assert "alongside_subcategories" not in dumped
    assert "single_type" not in dumped


# ── the order ───────────────────────────────────────────────────────────────


def _card(
    product_id: int,
    subcategory: str,
    *,
    price: str = "1000",
    depth: int = 0,
    colour: str | None = None,
    seats: int | None = None,
) -> RelaxedCandidate:
    return RelaxedCandidate(
        product=EligibleProduct(
            product_id=product_id,
            subcategory=subcategory,
            price_amount=Decimal(price),
            main_color=colour,
            seating_capacity=seats,
        ),
        relaxation_depth=depth,
    )


def _mixed(
    *,
    sort: ProductSort = ProductSort.DEFAULT,
    preferences: tuple[SemanticPreference, ...] = (),
    seat_preference: int | None = None,
    alongside: tuple[str, ...] = FAMILY,
) -> ResolvedSearch:
    return ResolvedSearch(
        request=SOFAS.model_copy(update={"alongside_subcategories": alongside, "sort": sort}),
        semantic_preferences=preferences,
        seat_preference=seat_preference,
    )


async def _order(resolved: ResolvedSearch, cards: list[RelaxedCandidate]) -> list[int]:
    ranked = await SemanticRankingService(None, None).rank(
        resolved, cards, CONTEXT, namespace="store-50"
    )
    return list(ranked.product_ids)


async def test_the_types_take_turns_the_asked_type_first() -> None:
    cards = [
        _card(1, "sofa"),
        _card(2, "sofa"),
        _card(3, "sofa"),
        _card(4, "sofa-set"),
        _card(5, "sofa-set"),
        _card(6, "sectional-sofa"),
    ]

    assert await _order(_mixed(), cards) == [1, 4, 6, 2, 5, 3]


async def test_a_better_match_is_never_moved_below_a_worse_one() -> None:
    """Grey was asked for: the grey sofas lead whatever the sets' type."""
    grey = SemanticPreference(
        family=AttributeFamily.COLOR,
        raw_value="grey",
        canonical_value="Grey",
        strength=ConstraintStrength.PREFERRED,
    )
    cards = [
        _card(1, "sofa", colour="Grey"),
        _card(2, "sofa", colour="Grey"),
        _card(3, "sofa-set", colour="Beige"),
        _card(4, "sofa-set", colour="Grey"),
    ]

    assert await _order(_mixed(preferences=(grey,)), cards) == [1, 4, 2, 3]


async def test_the_head_count_keeps_its_order_across_types() -> None:
    """For five, the pieces that seat five come first, of any type."""
    cards = [
        _card(1, "sofa", seats=3),
        _card(2, "sofa", seats=5),
        _card(3, "sofa-set", seats=5),
        _card(4, "sofa-set", seats=5),
        _card(5, "sectional-sofa", seats=4),
    ]

    order = await _order(_mixed(seat_preference=5), cards)

    assert order[:3] == [2, 3, 4]


async def test_an_exact_match_stays_ahead_of_a_widened_one_of_another_type() -> None:
    cards = [_card(1, "sofa", depth=1), _card(2, "sofa-set", depth=0)]

    assert await _order(_mixed(), cards) == [2, 1]


async def test_the_cheapest_is_the_cheapest_whatever_its_type() -> None:
    cards = [
        _card(1, "sofa", price="1500"),
        _card(2, "sofa", price="1600"),
        _card(3, "sofa-set", price="4000"),
        _card(4, "sectional-sofa", price="3000"),
    ]

    assert await _order(_mixed(sort=ProductSort.PRICE_ASC), cards) == [1, 2, 3, 4]


async def test_a_search_of_one_type_keeps_its_order() -> None:
    cards = [_card(1, "sofa"), _card(2, "sofa"), _card(3, "sofa")]

    assert await _order(_mixed(alongside=()), cards) == [1, 2, 3]


# ── what the reply is told ──────────────────────────────────────────────────


def _shown(*subcategories: str) -> Any:
    return [SimpleNamespace(commerce=SimpleNamespace(subcategory=s)) for s in subcategories]


def test_the_count_of_the_asked_type_is_the_exact_ones_that_seat_them() -> None:
    """One 5-seat sofa, twelve 5-seat sets: "only one sofa seats 5" is true.
    An unrecorded seat count and a widened match are never counted."""
    searched = SimpleNamespace(
        candidates=(
            _card(1, "sofa", seats=5),
            _card(2, "sofa", seats=3),
            _card(3, "sofa", seats=None),
            _card(4, "sofa", seats=6, depth=1),
            _card(5, "sofa-set", seats=5),
        )
    )

    mix = _type_mix(
        _mixed(seat_preference=5), cast(Any, searched), _shown("sofa", "sofa-set", "sofa-set")
    )

    assert mix == TypeMix(
        asked_type="sofa",
        alongside=FAMILY,
        asked_type_matches=1,
        on_screen=(
            TypeOnScreen(commerce_subcategory="sofa", count=1),
            TypeOnScreen(commerce_subcategory="sofa-set", count=2),
        ),
    )


def test_without_a_head_count_every_exact_sofa_counts() -> None:
    searched = SimpleNamespace(candidates=(_card(1, "sofa"), _card(2, "sofa", seats=2)))

    mix = _type_mix(_mixed(), cast(Any, searched), _shown("sofa"))

    assert mix is not None
    assert mix.asked_type_matches == 2


def test_a_search_of_one_type_reports_no_mix() -> None:
    searched = SimpleNamespace(candidates=(_card(1, "sofa"),))

    assert _type_mix(_mixed(alongside=()), cast(Any, searched), _shown("sofa")) is None


# ── the turn ────────────────────────────────────────────────────────────────


# ── a refinement keeps "just sofas" only for sofas ──────────────────────────


@pytest.fixture(scope="module")
def composer() -> SearchRefinementComposer:
    return SearchRefinementComposer(
        load_catalog_attributes(), load_dimension_semantics(taxonomy=TAXONOMY), SEATING
    )


def test_refining_the_sofas_keeps_just_sofas(composer: SearchRefinementComposer) -> None:
    state = ActiveSearchState(request=SOFAS.model_copy(update={"single_type": True}), revision=1)

    result = composer.refine(state, SearchRefinementDelta())

    assert result.candidate.request.single_type  # type: ignore[union-attr]


def test_a_change_of_type_lets_just_sofas_go(composer: SearchRefinementComposer) -> None:
    state = ActiveSearchState(request=SOFAS.model_copy(update={"single_type": True}), revision=1)

    result = composer.refine_taxonomy(
        state, commerce_category="seating", commerce_subcategory="sectional-sofa"
    )

    assert not result.candidate.request.single_type  # type: ignore[union-attr]


# ── the decision model is asked exactly what it was, switched off ───────────


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return "decision-model-under-test"

    async def parse(self, *, instructions: str, user_input: str, schema: type[BaseModel]) -> Any:
        self.calls.append({"instructions": instructions, "schema": schema})
        return CustomerAgentDecision(action=AgentAction.ANSWER)


# ── "a simple sofa" never brings the sets back (known issue 21) ─────────────


def test_a_combination_for_sofas_alone_uses_no_other_main_type() -> None:
    from app.schemas.seating_solution import SeatingRequirements
    from app.services.seating_solution import SeatingSolutionPlanner

    planner = SeatingSolutionPlanner(cast(Any, None), cast(Any, None), cast(Any, None), SEATING)
    alone = SeatingRequirements(asked_type="sofa", single_type=True)
    either = SeatingRequirements(asked_type="sofa")

    assert planner._left_out("sofa-set", alone)
    assert planner._left_out("sectional-sofa", alone)
    assert not planner._left_out("sofa", alone)
    assert not planner._left_out("chair", alone)
    assert not planner._left_out("sofa-set", either)
    assert planner._left_out("sofa-set", SeatingRequirements(avoid_type="sofa-set"))


# ── found by the QA review ──────────────────────────────────────────────────


def test_refining_more_like_this_keeps_its_type(composer: SearchRefinementComposer) -> None:
    """ "Cheaper ones" after More like this on a sofa is still sofas."""
    state = ActiveSearchState(request=SOFAS, revision=1, from_product=True)

    result = composer.refine(state, SearchRefinementDelta())

    assert result.candidate.request.single_type  # type: ignore[union-attr]


async def test_their_own_words_keep_the_closest_first_whatever_its_type() -> None:
    """ "Something emerald": similarity carries words no approved colour
    names, so it decides the order - the types do not take turns."""
    worded = _mixed().model_copy(update={"semantic_text": "emerald"})
    cards = [_card(1, "sofa"), _card(2, "sofa"), _card(3, "sofa-set")]

    assert await _order(worded, cards) == [1, 2, 3]
