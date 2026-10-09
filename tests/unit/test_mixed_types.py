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
from app.prompts.customer_commerce.v1 import (
    MIXED_TYPES_SUFFIX,
    VERSION,
    build_instructions,
)
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    with_mixed_types,
)
from app.schemas.agent_state import ActiveSearchState
from app.schemas.agent_turn import DecisionInput
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    ProductSearchRequest,
    ProductSort,
)
from app.schemas.grounding import TypeMix, TypeOnScreen
from app.schemas.product import EligibleProduct
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    ResolvedSearch,
    SemanticPreference,
)
from app.schemas.refinement import SearchRefinementDelta
from app.schemas.relaxation import RelaxedCandidate
from app.schemas.response import TypeMixView
from app.schemas.retailer import RetailerContext
from app.services.customer_decision import CustomerAgentDecisionService
from app.services.refinement_composer import SearchRefinementComposer
from app.services.response_generator import _type_mix_counts
from app.services.response_view import _type_mix_view
from app.services.search_pipeline import _type_mix
from app.services.semantic_ranking import SemanticRankingService
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes
from app.taxonomy.dimensions import DimensionRole, load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.seating import load_seating_semantics
from pydantic import BaseModel, ValidationError

from tests.unit.test_turn_coordinator import (
    SOFAS,
    FakePipeline,
    _coordinator,
    _resolved,
    _state,
    _turn,
)

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


def _sofa_with_width() -> ResolvedSearch:
    return ResolvedSearch(
        request=SOFAS.model_copy(update={"dimensions": (_width(),)}),
        semantics=ConstraintSemantics(
            dimensions=(
                DimensionConstraintSemantics(
                    role=DimensionRole.OVERALL_WIDTH, strength=ConstraintStrength.LOCKED
                ),
            )
        ),
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


def test_the_reply_reads_kinds_in_words_without_counting_the_page() -> None:
    view = _type_mix_view(
        TypeMix(
            asked_type="sofa",
            alongside=FAMILY,
            asked_type_matches=1,
            on_screen=(
                TypeOnScreen(commerce_subcategory="sofa", count=1),
                TypeOnScreen(commerce_subcategory="sofa-set", count=4),
            ),
        )
    )

    assert view == TypeMixView(
        asked_kind="sofa",
        shown_beside=("sofa set", "sectional sofa"),
        asked_kind_matches=1,
        on_screen=("sofa", "sofa set"),
    )


@pytest.mark.parametrize(
    ("matches", "presented", "sayable"),
    [(1, 5, (1,)), (0, 5, (0,)), (173, 5, ()), (5, 5, ()), (None, 5, ())],
)
def test_only_a_shortfall_of_the_asked_kind_may_be_counted(
    matches: int | None, presented: int, sayable: tuple[int, ...]
) -> None:
    """ "I have only one sofa that seats 5" - never "2 sofas here", which
    reads as the shop's stock when about 170 match."""
    view = TypeMixView(asked_kind="sofa", asked_kind_matches=matches)

    assert _type_mix_counts(view, presented) == sayable


# ── the turn ────────────────────────────────────────────────────────────────


async def test_a_new_sofa_search_also_searches_sets_and_sectionals() -> None:
    pipeline = FakePipeline(ids=(20, 21))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(),
        pipeline=pipeline,
        seating=SEATING,
        mixed_types=True,
    )

    result = await coordinator.run(_turn(_state(revision=1)))

    assert pipeline.calls[0].request.alongside_subcategories == FAMILY
    # Worked out on every run, never kept with the search.
    assert result.state.active_search is not None
    assert result.state.active_search.request.alongside_subcategories == ()


async def test_switched_off_a_sofa_search_is_sofas_only() -> None:
    pipeline = FakePipeline(ids=(20,))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(),
        pipeline=pipeline,
        seating=SEATING,
    )

    await coordinator.run(_turn(_state(revision=1)))

    assert pipeline.calls[0].request.alongside_subcategories == ()


async def test_a_size_keeps_the_search_to_sofas_and_says_why() -> None:
    pipeline = FakePipeline(ids=(20,))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_sofa_with_width(),
        pipeline=pipeline,
        seating=SEATING,
        mixed_types=True,
    )

    result = await coordinator.run(_turn(_state(revision=1), "a sofa under 220 cm wide"))

    assert pipeline.calls[0].request.alongside_subcategories == ()
    assert result.grounding.search is not None
    assert result.grounding.search.type_mix == TypeMix(
        asked_type="sofa", left_out_for_size=FAMILY
    )


async def test_just_sofas_keeps_to_sofas_and_stays_with_the_search() -> None:
    pipeline = FakePipeline(ids=(20, 21))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, only_asked_type=True),
        interpretation=_resolved(),
        pipeline=pipeline,
        seating=SEATING,
        mixed_types=True,
    )

    result = await coordinator.run(_turn(_state(revision=1), "just sofas"))

    assert pipeline.calls[0].request.alongside_subcategories == ()
    assert result.state.active_search is not None
    assert result.state.active_search.request.single_type


async def test_showing_more_keeps_to_sofas_after_just_sofas() -> None:
    kept = SOFAS.model_copy(update={"single_type": True})
    pipeline = FakePipeline(ids=(22,))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, show_more=True),
        pipeline=pipeline,
        seating=SEATING,
        mixed_types=True,
    )

    await coordinator.run(_turn(_state(request=kept), "show me more"))

    assert pipeline.calls[0].request.alongside_subcategories == ()


async def test_showing_more_of_a_mixed_search_stays_mixed() -> None:
    pipeline = FakePipeline(ids=(22,))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, show_more=True),
        pipeline=pipeline,
        seating=SEATING,
        mixed_types=True,
    )

    await coordinator.run(_turn(_state(), "show me more"))

    assert pipeline.calls[0].request.alongside_subcategories == FAMILY


@pytest.mark.parametrize("origin", ["ordered_by_pick", "from_product"])
async def test_what_goes_with_a_pick_and_more_like_this_keep_their_type(origin: str) -> None:
    """Ours to order, not the customer's search: their paging stays one type."""
    state = _state()
    assert state.active_search is not None
    state = state.model_copy(
        update={"active_search": state.active_search.model_copy(update={origin: True})}
    )
    pipeline = FakePipeline(ids=(22,))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, show_more=True),
        pipeline=pipeline,
        seating=SEATING,
        mixed_types=True,
    )

    await coordinator.run(_turn(state, "show me more"))

    assert pipeline.calls[0].request.alongside_subcategories == ()


async def test_a_refinement_saying_only_sofas_drops_the_others() -> None:
    pipeline = FakePipeline(ids=(20,))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            refinement=SearchRefinementDelta(),
            only_asked_type=True,
        ),
        pipeline=pipeline,
        seating=SEATING,
        mixed_types=True,
    )

    result = await coordinator.run(_turn(_state(), "only regular sofas please"))

    assert pipeline.calls[0].request.alongside_subcategories == ()
    assert result.state.active_search is not None
    assert result.state.active_search.request.single_type


async def test_another_type_has_nothing_beside_it() -> None:
    rugs = ProductSearchRequest(commerce_category="decor", commerce_subcategory="rug")
    pipeline = FakePipeline(ids=(20,))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(rugs),
        pipeline=pipeline,
        seating=SEATING,
        mixed_types=True,
    )

    await coordinator.run(_turn(_state(revision=1), "show me rugs"))

    assert pipeline.calls[0].request.alongside_subcategories == ()


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


@pytest.mark.parametrize("on", [True, False])
async def test_the_decision_sees_only_asked_type_only_when_it_is_on(on: bool) -> None:
    client = _Client()

    await CustomerAgentDecisionService(client, mixed_types=on).decide(
        DecisionInput(message="just sofas")
    )

    call = client.calls[0]
    properties = call["schema"].model_json_schema()["properties"]
    assert ("only_asked_type" in properties) is on
    assert ("SOFAS, SETS AND SECTIONALS" in call["instructions"]) is on


def test_off_the_instructions_are_exactly_as_before() -> None:
    assert build_instructions() == build_instructions(mixed_types=False)
    assert "only_asked_type" not in build_instructions()
    assert "only_asked_type" in build_instructions(mixed_types=True)


def test_the_plain_contract_hides_the_field_and_keeps_its_value() -> None:
    assert "only_asked_type" not in CustomerAgentDecision.model_json_schema()["properties"]
    shown = with_mixed_types(CustomerAgentDecision)
    assert "only_asked_type" in shown.model_json_schema()["properties"]
    assert VERSION + MIXED_TYPES_SUFFIX != VERSION


# ── "a simple sofa" never brings the sets back (known issue 21) ─────────────


class _ByAnyType(FakePipeline):
    """Finds products for whichever type a search covers, recording each."""

    def __init__(self, found: dict[str, tuple[int, ...]]) -> None:
        super().__init__()
        self.found = found

    async def execute(self, resolved: Any, context: Any, **kwargs: Any) -> Any:
        self.ids = tuple(
            pid for kind in resolved.request.subcategories for pid in self.found.get(kind, ())
        )
        return await super().execute(resolved, context, **kwargs)


def _sofas_for(seats: int, **fields: Any) -> ProductSearchRequest:
    from app.schemas.discovery import SeatingCapacityConstraint

    return SOFAS.model_copy(
        update={"seating_capacity": SeatingCapacityConstraint(min_capacity=seats), **fields}
    )


async def test_a_sofa_for_six_finds_the_sets_in_the_same_search() -> None:
    from tests.unit.test_seating_instead import Catalog

    pipeline = _ByAnyType({"sofa-set": (31, 32)})
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(_sofas_for(6)),
        pipeline=pipeline,
        capabilities=Catalog(),
        seating=SEATING,
        mixed_types=True,
    )

    result = await coordinator.run(_turn(_state(request=None), "a sofa for 6"))

    assert len(pipeline.calls) == 1
    assert result.state.product_interaction.presented_product_ids == (31, 32)


async def test_just_sofas_for_six_is_never_switched_to_the_sets() -> None:
    from tests.unit.test_seating_instead import Catalog
    from tests.unit.test_turn_coordinator import FakeSeatingPlanner

    pipeline = _ByAnyType({"sofa-set": (31, 32)})
    planner = FakeSeatingPlanner()
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, only_asked_type=True),
        interpretation=_resolved(_sofas_for(6)),
        pipeline=pipeline,
        capabilities=Catalog(),
        seating=SEATING,
        seating_planner=planner,
        mixed_types=True,
    )

    await coordinator.run(_turn(_state(request=None), "a simple sofa for 6, not a set"))

    assert [call.request.subcategories for call in pipeline.calls] == [("sofa",)]
    assert planner.calls[0]["requirements"].single_type


async def test_rather_a_simple_sofa_while_sets_show_keeps_to_sofas() -> None:
    """The sets for seven were on screen beside the sofas; naming sofas again
    is turning the sets down."""
    from tests.unit.test_seating_instead import Catalog
    from tests.unit.test_turn_coordinator import FakeSeatingPlanner

    pipeline = _ByAnyType({"sofa-set": (31, 32)})
    planner = FakeSeatingPlanner()
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH, taxonomy_change_requested=True
        ),
        interpretation=_resolved(_sofas_for(7)),
        pipeline=pipeline,
        capabilities=Catalog(),
        seating=SEATING,
        seating_planner=planner,
        mixed_types=True,
    )

    result = await coordinator.run(
        _turn(_state(request=_sofas_for(7)), "I dont want l shape sofa rather a simple sofa")
    )

    assert [call.request.subcategories for call in pipeline.calls] == [("sofa",)]
    assert planner.calls[0]["requirements"].single_type
    assert result.state.active_search is not None
    assert result.state.active_search.request.single_type


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


def test_a_tapped_kind_is_that_type_alone() -> None:
    """Tapping "3-seater" means a 3-seat sofa: the kind is locked (10.4)."""
    from app.schemas.product_brief import BriefKindOption
    from app.services.product_brief import _with_kind

    three = BriefKindOption(
        key="kind-3", commerce_category="seating", commerce_subcategory="sofa", seats=3
    )

    tapped = _with_kind(_resolved(), three)

    assert tapped.request.single_type
    assert tapped.request.alongside_subcategories == ()


async def test_show_more_but_only_regular_sofas_keeps_to_sofas() -> None:
    pipeline = FakePipeline(ids=(22,))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH, show_more=True, only_asked_type=True),
        pipeline=pipeline,
        seating=SEATING,
        mixed_types=True,
    )

    result = await coordinator.run(_turn(_state(), "more, but only regular sofas"))

    assert pipeline.calls[0].request.alongside_subcategories == ()
    assert result.state.active_search is not None
    assert result.state.active_search.request.single_type


async def test_only_regular_sofas_holds_across_a_change_of_family() -> None:
    tables = ProductSearchRequest(commerce_category="tables", commerce_subcategory="center-table")
    pipeline = FakePipeline(ids=(20,))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(
            action=AgentAction.REFINE_SEARCH,
            taxonomy_change_requested=True,
            only_asked_type=True,
        ),
        interpretation=_resolved(),
        pipeline=pipeline,
        seating=SEATING,
        mixed_types=True,
    )

    result = await coordinator.run(_turn(_state(request=tables), "now just regular sofas"))

    assert pipeline.calls[-1].request.commerce_subcategory == "sofa"
    assert pipeline.calls[-1].request.alongside_subcategories == ()
    assert result.state.active_search is not None
    assert result.state.active_search.request.single_type


class _Cards:
    """The cards on screen are of the given types."""

    def __init__(self, kinds: dict[int, str]) -> None:
        self.kinds = kinds
        self.calls: list[list[int]] = []

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[Any, ...]:
        from tests.unit.test_turn_coordinator import _product

        self.calls.append(list(product_ids))
        return tuple(
            _product(p, subcategory=self.kinds[p]) for p in product_ids if p in self.kinds
        )


@pytest.mark.parametrize(
    ("on_screen", "kept_to_sofas"),
    [
        ({10: "sofa-set", 11: "sofa-set"}, True),
        ({10: "sofa", 11: "sofa-set"}, False),
    ],
    ids=["only-sets-on-screen", "a-sofa-on-screen"],
)
async def test_sofas_asked_for_while_only_sets_show_means_sofas_alone(
    on_screen: dict[int, str], kept_to_sofas: bool
) -> None:
    """Known issue 21, whichever way the model reads "show me sofas instead"."""
    pipeline = FakePipeline(ids=(20,))
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(_sofas_for(7)),
        pipeline=pipeline,
        hydration=cast(Any, _Cards(on_screen)),
        seating=SEATING,
        mixed_types=True,
    )

    await coordinator.run(_turn(_state(request=_sofas_for(7)), "show me sofas instead"))

    assert pipeline.calls[0].request.single_type is kept_to_sofas


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


async def test_types_already_covered_are_not_searched_again() -> None:
    """A mixed search for six in purple found nothing: the sets were in it,
    so they are not searched a second time as "another type"."""
    from tests.unit.test_seating_instead import Catalog

    pipeline = _ByAnyType({})
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(_sofas_for(6, colors_any_of=("Purple",))),
        pipeline=pipeline,
        capabilities=Catalog(),
        seating=SEATING,
        mixed_types=True,
    )

    await coordinator.run(_turn(_state(request=None), "a purple sofa for 6, only purple"))

    assert len(pipeline.calls) == 1


@pytest.mark.parametrize(("alone", "combining"), [(True, True), (False, False)])
async def test_just_sofas_for_seven_needs_combining(alone: bool, combining: bool) -> None:
    """Sofas seat four at most here and sets seven: for sofas alone, seven is
    the seating-shape question, asked before anything else."""
    from tests.unit.test_seating_instead import Catalog

    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        capabilities=Catalog(),
        seating=SEATING,
        mixed_types=True,
    )
    request = _sofas_for(7, single_type=alone)

    assert await coordinator._needs_combining(_resolved(request), _turn(_state())) is combining


async def test_switched_off_a_stored_sofas_alone_changes_nothing() -> None:
    """A session that said "just sofas" while the mix was on: switched off,
    the search for six still finds the sofa set, exactly as before."""
    from tests.unit.test_seating_instead import ByType, Catalog

    pipeline = ByType({"sofa-set": (31, 32)})
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH),
        interpretation=_resolved(_sofas_for(6, single_type=True)),
        pipeline=pipeline,
        capabilities=Catalog(),
        seating=SEATING,
    )

    await coordinator.run(_turn(_state(request=None), "a sofa for 6"))

    assert [call.request.commerce_subcategory for call in pipeline.calls] == ["sofa", "sofa-set"]


def test_an_empty_refinement_is_allowed_only_for_sofas_alone() -> None:
    with pytest.raises(ValidationError):
        CustomerAgentDecision(action=AgentAction.REFINE_SEARCH, refinement=SearchRefinementDelta())
    CustomerAgentDecision(
        action=AgentAction.REFINE_SEARCH, refinement=SearchRefinementDelta(), only_asked_type=True
    )
    assert "only_asked_type and no delta" in build_instructions(mixed_types=True)
