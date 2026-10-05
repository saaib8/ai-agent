"""The agent loop after a weak search (CLAUDE.md 14.8).

Nothing of the asked type met the customer's limits; the model looks at the
stocked siblings, may try one with every other limit kept, and chooses what to
present. Every rule is code: only stocked siblings can be tried, a try changes
the type and nothing else, nothing is committed until the caller presents the
choice, and anything unusable keeps the original reply.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

import pytest
from app.core.exceptions import IntegrationUnavailableError
from app.schemas.agent_loop import Finish, LoopMove, TryType, build_move_schema
from app.schemas.agent_state import AgentStateV1
from app.schemas.catalog_overview import CatalogOverview, SubcategoryShelf
from app.schemas.composition import ComposedSearch
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PriceConstraint,
    ProductSearchRequest,
    SeatingCapacityConstraint,
)
from app.schemas.grounding import SearchExecutionGrounding, SearchOutcome
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    ResolvedSearch,
)
from app.schemas.relaxation import StopReason
from app.schemas.resolution import ProductSearchExecutionResult
from app.schemas.response import ResponseGroundingView, ResponseOutcomeKind
from app.schemas.retailer import RetailerContext
from app.services.agent_loop import WeakSearchLoop
from app.services.grounding_builder import to_grounded_product
from app.services.response_view import route_response
from app.services.retype import composed_as_type
from app.services.stock_fit import StockFit
from app.taxonomy.compare_groups import load_compare_groups
from app.taxonomy.dimensions import DimensionRole
from app.taxonomy.seating import load_seating_semantics

from tests.unit import test_product_brief as brief_tests
from tests.unit.test_closest_type import TAXONOMY
from tests.unit.test_stock_fit import FakeStockFit, _shelf

CONTEXT = RetailerContext(store_id=50)
SEATING = load_seating_semantics(taxonomy=TAXONOMY)
GROUPS = load_compare_groups(taxonomy=TAXONOMY)


def _seats(shelf: SubcategoryShelf, seats: int) -> SubcategoryShelf:
    return shelf.model_copy(update={"implied_seats": seats})


STORE = CatalogOverview(
    store_id=50,
    currency="SAR",
    shelves=(
        _seats(_shelf("seating", "sectional-sofa"), 6),
        _seats(_shelf("seating", "sofa"), 4),
        _seats(_shelf("seating", "sofa-set"), 7),
        _seats(_shelf("seating", "lounge-chair"), 1),
        _shelf("tables", "center-table"),
    ),
)


class ScriptedModel:
    """Answers with the given moves in order, recording each call. Ignores the
    schema so a test can hand back a move the constrained schema forbids."""

    def __init__(self, *moves: TryType | Finish, error: Exception | None = None) -> None:
        self._moves = list(moves)
        self._error = error
        self.calls: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return "fake-model"

    async def parse(self, *, instructions: str, user_input: str, schema: type) -> Any:
        self.calls.append({"input": json.loads(user_input), "schema": schema})
        if self._error is not None:
            raise self._error
        return LoopMove(move=self._moves.pop(0))


def _found(count: int) -> ProductSearchExecutionResult:
    ids = tuple(range(41, 41 + count))
    return ProductSearchExecutionResult(
        presented_product_ids=ids,
        grounding=SearchExecutionGrounding(
            outcome=SearchOutcome.RESULTS if count else SearchOutcome.ZERO_RESULTS,
            products=tuple(
                to_grounded_product(
                    brief_tests._sofa(pid), grounding_ref=n, presented_ordinal=n, relaxation_depth=0
                )
                for n, pid in enumerate(ids, start=1)
            ),
            eligible_count=count,
            ranked_count=count,
            selected_count=count,
            presented_count=count,
            exact_candidate_count=count,
            stop_reason=(
                StopReason.EXACT_SUFFICIENT if count else StopReason.NO_RELAXABLE_CONSTRAINTS
            ),
        ),
    )


@dataclass
class ShelfPipeline:
    """How many products each type has within the limits; records each run."""

    found: dict[str, int]
    ran: list[Any] = field(default_factory=list)

    async def execute(self, resolved: Any, context: Any, **kwargs: Any) -> Any:
        self.ran.append((resolved, kwargs))
        return _found(self.found.get(resolved.request.commerce_subcategory, 0))


def _composed(*, seats: int | None = None, width: int | None = None) -> ComposedSearch:
    from app.services.refinement_composer import SearchRefinementComposer

    dimensions = (
        (
            DimensionConstraint(
                role=DimensionRole.OVERALL_WIDTH,
                kind=DimensionConstraintKind.MAX,
                max_cm=Decimal(width),
            ),
        )
        if width
        else ()
    )
    need = ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sectional-sofa",
            price=PriceConstraint(max_amount=Decimal("3000"), currency="SAR"),
            colors_any_of=("Grey",),
            seating_capacity=SeatingCapacityConstraint(min_capacity=seats) if seats else None,
            dimensions=dimensions,
        ),
        semantics=ConstraintSemantics(
            subcategory=ConstraintStrength.LOCKED,
            price_max=ConstraintStrength.LOCKED,
            seating_min=ConstraintStrength.LOCKED if seats else None,
            dimensions=tuple(
                DimensionConstraintSemantics(role=d.role, strength=ConstraintStrength.LOCKED)
                for d in dimensions
            ),
        ),
    )
    composer = SearchRefinementComposer(brief_tests.ATTRIBUTES, brief_tests.DIMENSIONS, SEATING)
    return composer.seed_new_task(need)


def _loop(model: ScriptedModel, pipeline: ShelfPipeline, *, max_tries: int = 2) -> WeakSearchLoop:
    return WeakSearchLoop(
        cast(Any, model), cast(Any, pipeline), SEATING, GROUPS, max_tries=max_tries
    )


EMPTY = _found(0).grounding


# ── what the loop decides, and what it may not ─────────────────────────────


async def test_a_related_type_that_meets_every_limit_is_chosen() -> None:
    model = ScriptedModel(TryType(commerce_subcategory="sofa"), Finish(present="sofa"))
    pipeline = ShelfPipeline({"sofa": 4})

    outcome = await _loop(model, pipeline).explore(_composed(), EMPTY, STORE, CONTEXT)

    assert outcome.chosen is not None and outcome.chosen.commerce_subcategory == "sofa"
    request = outcome.chosen.composed.resolved.request
    # The type changed and nothing else did.
    assert request.price is not None and request.price.max_amount == Decimal("3000")
    assert request.colors_any_of == ("Grey",)
    assert outcome.steps == 2 and len(pipeline.ran) == 1


async def test_finishing_with_the_original_tries_nothing() -> None:
    """The gap is the budget: another kind would not fix it."""
    model = ScriptedModel(Finish(present="original"))
    pipeline = ShelfPipeline({})

    outcome = await _loop(model, pipeline).explore(_composed(), EMPTY, STORE, CONTEXT)

    assert outcome.chosen is None and pipeline.ran == [] and outcome.steps == 1


async def test_a_try_that_found_nothing_is_never_presented() -> None:
    model = ScriptedModel(TryType(commerce_subcategory="sofa"), Finish(present="sofa"))

    outcome = await _loop(model, ShelfPipeline({})).explore(_composed(), EMPTY, STORE, CONTEXT)

    assert outcome.chosen is None


async def test_presenting_a_type_never_tried_keeps_the_original() -> None:
    model = ScriptedModel(Finish(present="sofa-set"))

    outcome = await _loop(model, ShelfPipeline({"sofa-set": 5})).explore(
        _composed(), EMPTY, STORE, CONTEXT
    )

    assert outcome.chosen is None


@pytest.mark.parametrize("off_list", ["center-table", "sectional-sofa", "chandelier"])
async def test_a_type_outside_the_stocked_siblings_is_never_tried(off_list: str) -> None:
    """Another category, the asked type itself, or one not stocked."""
    pipeline = ShelfPipeline({off_list: 9})
    model = ScriptedModel(TryType(commerce_subcategory=off_list))

    outcome = await _loop(model, pipeline).explore(_composed(), EMPTY, STORE, CONTEXT)

    assert outcome.chosen is None and pipeline.ran == []


async def test_the_tries_are_capped_and_the_last_step_can_only_finish() -> None:
    model = ScriptedModel(
        TryType(commerce_subcategory="sofa"),
        TryType(commerce_subcategory="sofa-set"),
        Finish(present="sofa-set"),
    )
    pipeline = ShelfPipeline({"sofa": 0, "sofa-set": 2})

    outcome = await _loop(model, pipeline, max_tries=2).explore(_composed(), EMPTY, STORE, CONTEXT)

    assert outcome.chosen is not None and outcome.chosen.commerce_subcategory == "sofa-set"
    assert len(pipeline.ran) == 2 and len(model.calls) == 3
    final = model.calls[-1]["schema"].model_json_schema()
    assert "try_type" not in json.dumps(final)


async def test_with_nothing_found_and_nothing_left_to_try_it_stops_without_asking() -> None:
    model = ScriptedModel(TryType(commerce_subcategory="sofa"))

    outcome = await _loop(model, ShelfPipeline({}), max_tries=1).explore(
        _composed(), EMPTY, STORE, CONTEXT
    )

    assert outcome.chosen is None and len(model.calls) == 1


async def test_several_seats_are_never_offered_a_one_seat_type() -> None:
    model = ScriptedModel(Finish(present="original"))

    await _loop(model, ShelfPipeline({})).explore(_composed(seats=4), EMPTY, STORE, CONTEXT)

    assert "lounge-chair" not in model.calls[0]["input"]["you_may_still_try"]


async def test_a_one_seat_type_may_be_tried_without_a_seat_count() -> None:
    """A chaise lounge asked for, no seat count: a lounge chair does that job."""
    model = ScriptedModel(Finish(present="original"))
    chaise = composed_as_type(_composed(), "chaise-lounge")

    await _loop(model, ShelfPipeline({})).explore(chaise, EMPTY, STORE, CONTEXT)

    assert model.calls[0]["input"]["you_may_still_try"] == ["lounge-chair"]


async def test_a_provider_failure_keeps_the_original_reply() -> None:
    model = ScriptedModel(error=IntegrationUnavailableError())

    outcome = await _loop(model, ShelfPipeline({})).explore(_composed(), EMPTY, STORE, CONTEXT)

    assert outcome.chosen is None


async def test_a_category_level_search_is_left_alone() -> None:
    composed = _composed()
    broad = composed.model_copy(
        update={
            "resolved": composed.resolved.model_copy(
                update={
                    "request": composed.resolved.request.model_copy(
                        update={"commerce_subcategory": None}
                    )
                }
            )
        }
    )
    model = ScriptedModel()

    outcome = await _loop(model, ShelfPipeline({})).explore(broad, EMPTY, STORE, CONTEXT)

    assert outcome.chosen is None and model.calls == []


# ── what a try carries ──────────────────────────────────────────────────────


def test_a_try_drops_and_reports_the_size_given_for_the_asked_type() -> None:
    variant = composed_as_type(_composed(width=250), "sofa")

    assert variant.resolved.request.dimensions == ()
    assert variant.candidate.request.dimensions == ()
    assert [d.role for d in variant.dropped_constraints] == [DimensionRole.OVERALL_WIDTH]
    assert variant.earlier_sizes_applied is False


def test_a_try_keeps_the_seat_count() -> None:
    variant = composed_as_type(_composed(seats=4), "sofa-set")

    capacity = variant.resolved.request.seating_capacity
    assert capacity is not None and capacity.min_capacity == 4


def test_the_observation_carries_facts_not_ids() -> None:
    """What the model sees: the limits in words, the shelf in summary."""
    from app.services.agent_loop import _observation

    seen = json.loads(_observation(_composed(), EMPTY, STORE, [], ("sofa",)))

    assert seen["asked_kind"] == "sectional-sofa"
    assert "at most 3000 SAR" in seen["their_limits"]
    assert "only these colours: Grey" in seen["their_limits"]
    assert seen["you_may_still_try"] == ["sofa"]
    assert "41" not in json.dumps(seen)


def test_the_move_schema_has_only_this_steps_choices() -> None:
    schema = json.dumps(build_move_schema(("sofa",), ("sofa-set",)).model_json_schema())

    assert '"sofa"' in schema and '"sofa-set"' in schema and '"original"' in schema
    assert "oneOf" not in schema


# ── in the turn ─────────────────────────────────────────────────────────────


class Shelf:
    """Capabilities with an overview of the store."""

    async def overview(self, context: Any) -> CatalogOverview:
        return STORE


class EmptyFor(brief_tests.Pipeline):
    """The harness's pipeline, with nothing for the given types."""

    def __init__(self, *empty: str) -> None:
        super().__init__()
        self.empty = set(empty)

    async def execute(self, resolved: Any, context: Any, **kwargs: Any) -> Any:
        if resolved.request.commerce_subcategory in self.empty:
            self.requests.append(resolved)
            return _found(0)
        return await super().execute(resolved, context, **kwargs)


def _turn_coordinator(model: ScriptedModel, pipeline: EmptyFor, *, stock_fit: Any = None) -> Any:
    coordinator, _ = brief_tests._coordinator(
        brief_tests._search(skip_questions=True),
        brief_tests._need("sectional-sofa"),
        capabilities=Shelf(),
        pipeline=pipeline,
        stock_fit=stock_fit,
    )
    coordinator._agent_loop = WeakSearchLoop(
        cast(Any, model), cast(Any, pipeline), SEATING, GROUPS, max_tries=2
    )
    return coordinator


async def test_a_weak_search_presents_the_related_type_and_says_so() -> None:
    model = ScriptedModel(TryType(commerce_subcategory="sofa"), Finish(present="sofa"))
    pipeline = EmptyFor("sectional-sofa")
    coordinator = _turn_coordinator(model, pipeline)

    result = await coordinator.run(brief_tests._typed(AgentStateV1(), "just show me sectionals"))

    assert [r.request.commerce_subcategory for r in pipeline.requests] == [
        "sectional-sofa",
        "sofa",
    ], "asked, then tried - and the try is committed as it ran, never run twice"
    assert result.alternative_to == "sectional-sofa"
    assert result.state.active_search is not None
    assert result.state.active_search.request.commerce_subcategory == "sofa"
    route = route_response(result)
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.alternative_to == "sectional sofa"
    # Another kind is its own next step: no card folded beside it.
    assert result.product_brief is None


async def test_keeping_the_original_leaves_the_turn_exactly_as_it_was() -> None:
    model = ScriptedModel(Finish(present="original"))
    pipeline = EmptyFor("sectional-sofa")
    coordinator = _turn_coordinator(model, pipeline)

    result = await coordinator.run(brief_tests._typed(AgentStateV1(), "just show me sectionals"))

    assert len(pipeline.requests) == 1
    assert result.alternative_to is None
    assert result.state.active_search is not None
    assert result.state.active_search.request.commerce_subcategory == "sectional-sofa"


async def test_results_for_the_asked_type_never_reach_the_loop() -> None:
    model = ScriptedModel()
    coordinator = _turn_coordinator(model, EmptyFor())

    await coordinator.run(brief_tests._typed(AgentStateV1(), "just show me sectionals"))

    assert model.calls == []


async def test_a_type_the_stock_check_already_judged_is_not_looked_at_again() -> None:
    """A substitute or a type not carried: phase 1 already chose."""
    model = ScriptedModel()
    stock = FakeStockFit(StockFit.NOT_CARRIED, asked="sectional-sofa")
    coordinator = _turn_coordinator(model, EmptyFor("sectional-sofa"), stock_fit=stock)

    await coordinator.run(brief_tests._typed(AgentStateV1(), "just show me sectionals"))

    assert model.calls == []


def test_one_turn_gives_one_substitution_reason() -> None:
    with pytest.raises(ValueError, match="one substitution reason"):
        ResponseGroundingView(
            kind=ResponseOutcomeKind.ANSWER,
            alternative_to="sectional sofa",
            kind_not_found="bunk bed",
        )


async def test_a_sibling_whose_cheapest_piece_is_over_budget_is_never_offered() -> None:
    """Sectionals start at 3,450 in a store, the budget is 3,000: not a try."""
    pricey = CatalogOverview(
        store_id=50,
        currency="SAR",
        shelves=(
            _shelf("seating", "sofa-set"),
            SubcategoryShelf(
                commerce_category="seating",
                commerce_subcategory="sectional-sofa",
                active_count=5,
                price_minimum=Decimal("3450"),
                price_maximum=Decimal("9950"),
            ),
            _shelf("seating", "sofa"),
        ),
    )
    composed = composed_as_type(_composed(), "sofa-set")
    model = ScriptedModel(Finish(present="original"))

    await _loop(model, ShelfPipeline({})).explore(composed, EMPTY, pricey, CONTEXT)

    assert model.calls[0]["input"]["you_may_still_try"] == ["sofa"]


async def test_a_sibling_that_cannot_seat_that_many_is_never_offered() -> None:
    """Sofas seat four at most; a party of six is offered only sets."""
    model = ScriptedModel(Finish(present="original"))

    await _loop(model, ShelfPipeline({})).explore(_composed(seats=6), EMPTY, STORE, CONTEXT)

    assert model.calls[0]["input"]["you_may_still_try"] == ["sofa-set"]


async def test_the_budget_filter_is_skipped_when_the_currency_differs() -> None:
    """A currency we cannot compare can only add candidates, never remove one."""
    usd = STORE.model_copy(update={"currency": "USD"})
    model = ScriptedModel(Finish(present="original"))

    await _loop(model, ShelfPipeline({})).explore(_composed(), EMPTY, usd, CONTEXT)

    assert "sofa-set" in model.calls[0]["input"]["you_may_still_try"]


async def test_a_try_found_only_by_widening_is_never_presented() -> None:
    """Products that met every limit exactly, or none: a widened or colour-lifted
    try would make "nothing was loosened" untrue."""
    widened = _found(3).grounding.model_copy(update={"exact_candidate_count": 0})

    @dataclass
    class WidenedPipeline:
        async def execute(self, resolved: Any, context: Any, **_: Any) -> Any:
            return _found(3).model_copy(update={"grounding": widened})

    model = ScriptedModel(TryType(commerce_subcategory="sofa"), Finish(present="sofa"))

    outcome = await WeakSearchLoop(
        cast(Any, model), cast(Any, WidenedPipeline()), SEATING, GROUPS, max_tries=2
    ).explore(_composed(), EMPTY, STORE, CONTEXT)

    assert outcome.chosen is None


async def test_a_search_with_a_size_never_loops_and_keeps_the_size() -> None:
    """QA bug 1: "around 220 cm wide" may be the whole gap - another kind would
    only drop it. The honest offer to set the width aside stands (13.5)."""
    model = ScriptedModel()
    pipeline = EmptyFor("sectional-sofa")
    coordinator = _turn_coordinator(model, pipeline)
    width = DimensionConstraint(
        role=DimensionRole.OVERALL_WIDTH, kind=DimensionConstraintKind.MAX, max_cm=Decimal(250)
    )
    coordinator._query_understanding.outcome = ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sectional-sofa",
            dimensions=(width,),
        ),
        semantics=ConstraintSemantics(
            subcategory=ConstraintStrength.LOCKED,
            dimensions=(
                DimensionConstraintSemantics(
                    role=DimensionRole.OVERALL_WIDTH, strength=ConstraintStrength.LOCKED
                ),
            ),
        ),
    )

    result = await coordinator.run(brief_tests._typed(AgentStateV1(), "just show me sectionals"))

    assert model.calls == [] and result.alternative_to is None
    assert result.state.customer_preferences.measurements_for("sectional-sofa") is not None


async def test_a_type_the_store_does_not_stock_never_reaches_the_loop() -> None:
    """With the stock check off, an absent type's empty search is still not
    "nothing met your limits" - the loop leaves it alone."""

    class NoSectionals:
        async def overview(self, context: Any) -> CatalogOverview:
            return STORE.model_copy(
                update={
                    "shelves": tuple(
                        s for s in STORE.shelves if s.commerce_subcategory != "sectional-sofa"
                    )
                }
            )

    model = ScriptedModel()
    pipeline = EmptyFor("sectional-sofa")
    coordinator, _ = brief_tests._coordinator(
        brief_tests._search(skip_questions=True),
        brief_tests._need("sectional-sofa"),
        capabilities=NoSectionals(),
        pipeline=pipeline,
    )
    coordinator._agent_loop = WeakSearchLoop(
        cast(Any, model), cast(Any, pipeline), SEATING, GROUPS, max_tries=2
    )

    result = await coordinator.run(brief_tests._typed(AgentStateV1(), "just show me sectionals"))

    assert model.calls == [] and result.alternative_to is None


async def test_answering_a_substitutes_card_stays_with_it_and_says_so() -> None:
    """The recliner card was for lounge chairs: its answers search lounge
    chairs, say recliners are not carried, and never loop past them."""
    from app.schemas.agent_turn import CustomerTurnInput
    from app.schemas.search_action import BriefAnswerAction

    stock = FakeStockFit(StockFit.SUBSTITUTED, swap_to=("seating", "sofa"), asked="recliner")
    model = ScriptedModel()
    pipeline = EmptyFor("sofa")
    coordinator, _ = brief_tests._coordinator(
        brief_tests._search(),
        brief_tests._need("recliner"),
        capabilities=Shelf(),
        pipeline=pipeline,
        stock_fit=stock,
    )
    coordinator._agent_loop = WeakSearchLoop(
        cast(Any, model), cast(Any, pipeline), SEATING, GROUPS, max_tries=2
    )
    carded = await coordinator.run(brief_tests._typed(AgentStateV1(), "I need a recliner"))
    pending = carded.state.product_brief.pending
    assert pending is not None and pending.substituted_for == "recliner"

    result = await coordinator.run(
        CustomerTurnInput(
            message="Show me sofas",
            state=carded.state,
            context=CONTEXT,
            search_action=BriefAnswerAction(card=pending.card),
        )
    )

    assert model.calls == []
    assert result.unstocked_type == "recliner"
    assert result.alternative_to is None


async def test_a_kind_they_insisted_on_is_never_swapped() -> None:
    """QA bug 2: "it has to be L-shaped, nothing else" keeps the honest reply."""
    composed = _composed()
    insisted = composed.model_copy(
        update={"resolved": composed.resolved.model_copy(update={"kind_required": True})}
    )
    model = ScriptedModel()

    outcome = await _loop(model, ShelfPipeline({"sofa": 5})).explore(
        insisted, EMPTY, STORE, CONTEXT
    )

    assert outcome.chosen is None and model.calls == []


@pytest.mark.parametrize(
    ("asked", "category", "offered"),
    [
        ("center-table", "tables", ("service-table",)),
        ("dining-chair", "seating", ("chair",)),
        ("tv-table", "tables", ("console",)),
    ],
)
async def test_only_types_that_do_the_same_job_are_offered(
    asked: str, category: str, offered: tuple[str, ...]
) -> None:
    """QA bug 3: a side table is not a coffee table, an armchair not a dining
    chair, a console not a TV unit - by the reviewed groups, not by a guess.
    With no stand-in, no model call is spent at all (QA bug 4)."""
    shelves = tuple(_shelf(category, s) for s in (asked, *offered))
    store = CatalogOverview(store_id=50, currency="SAR", shelves=shelves)
    composed = composed_as_type(_composed(), "sofa")
    retyped = composed.model_copy(
        update={
            "resolved": composed.resolved.model_copy(
                update={
                    "request": composed.resolved.request.model_copy(
                        update={
                            "commerce_category": category,
                            "commerce_subcategory": asked,
                            "colors_any_of": (),
                        }
                    )
                }
            )
        }
    )
    model = ScriptedModel()

    outcome = await _loop(model, ShelfPipeline(dict.fromkeys(offered, 5))).explore(
        retyped, EMPTY, store, CONTEXT
    )

    assert outcome.chosen is None and model.calls == []


def test_nothing_else_survives_seeding_the_search() -> None:
    """The flag must reach the loop: the composer rebuilds the search it runs."""
    from app.services.refinement_composer import SearchRefinementComposer

    need = brief_tests._need("sectional-sofa").model_copy(update={"kind_required": True})
    composer = SearchRefinementComposer(brief_tests.ATTRIBUTES, brief_tests.DIMENSIONS, SEATING)

    assert composer.seed_new_task(need).resolved.kind_required is True
