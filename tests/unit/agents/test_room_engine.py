"""The room engine: plan, swap, remove, lock and add, against hand fakes.

The optimiser, the discovery bridge and the registries are the real ones; only
the catalog, the search pipeline, the specialist and the seating planner are
faked, so what is asserted is the engine's own behaviour - what it commits,
what it pins and what it refuses.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from app.agents.operations.rooms import RoomEngine, RoomPlanInputs
from app.core.exceptions import CatalogUnavailableError, LLMResponseInvalidError
from app.schemas.agent_state import AgentStateV1, BundleItemStatus
from app.schemas.agent_updates import AgentStateUpdate, ProductInteractionUpdate, ReplaceItems
from app.schemas.design import InteriorDesignRequest, InteriorDesignResult
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import PriceConstraint
from app.schemas.grounding import SearchExecutionGrounding, SearchOutcome
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.relaxation import StopReason
from app.schemas.resolution import (
    CandidatePoolResult,
    ProductSearchExecutionResult,
    RankedProductCandidate,
)
from app.schemas.retailer import (
    RetailerCatalogCapabilities,
    RetailerCatalogCapability,
    RetailerContext,
)
from app.services.agent_state import apply_update
from app.services.bundle_optimizer import BundleOptimizer
from app.services.design_discovery import DesignDiscoveryService
from app.services.grounding_builder import to_grounded_product
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics

CONTEXT = RetailerContext(store_id=50)
TAXONOMY = load_taxonomy()
SEATING = load_seating_semantics(taxonomy=TAXONOMY)
ROOMS = load_room_pieces(taxonomy=TAXONOMY, seating=SEATING)

KINDS: dict[int, tuple[str, str, str]] = {
    1: ("seating", "sofa", "3000"),
    2: ("seating", "sofa", "2000"),
    10: ("tables", "center-table", "800"),
    11: ("tables", "center-table", "600"),
    20: ("decor", "carpet", "500"),
    21: ("decor", "carpet", "900"),
    30: ("tables", "service-table", "400"),
}


def _product(product_id: int) -> ProductCandidate:
    category, subcategory, price = KINDS[product_id]
    return ProductCandidate(
        product_id=product_id,
        name_english=f"{subcategory} {product_id}",
        name_arabic="",
        price_amount=Decimal(price),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(category=category, subcategory=subcategory),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


class FakeCatalog:
    """Hydration and capabilities over the fixed catalog above."""

    def __init__(self) -> None:
        self.available = set(KINDS)
        self.error: Exception | None = None

    async def hydrate_ids(
        self, product_ids: Sequence[int], context: RetailerContext
    ) -> tuple[ProductCandidate, ...]:
        if self.error is not None:
            raise self.error
        return tuple(_product(p) for p in product_ids if p in self.available)

    async def capabilities(self, context: RetailerContext) -> RetailerCatalogCapabilities:
        if self.error is not None:
            raise self.error
        pairs = dict.fromkeys((c, s) for c, s, _ in KINDS.values())
        return RetailerCatalogCapabilities(
            capabilities=tuple(
                RetailerCatalogCapability(
                    commerce_category=c, commerce_subcategory=s, active_product_count=5
                )
                for c, s in pairs
            )
        )


class FakePipeline:
    """Every product of the requested type, in id order - or reversed."""

    def __init__(self, catalog: FakeCatalog) -> None:
        self.catalog = catalog
        self.reverse = False
        self.searched: list[Any] = []

    def _matching(self, resolved: Any) -> list[ProductCandidate]:
        request = resolved.request
        types = {request.commerce_subcategory, *request.alongside_subcategories}
        found = [
            _product(pid)
            for pid in sorted(self.catalog.available, reverse=self.reverse)
            if KINDS[pid][0] == request.commerce_category
            and (request.commerce_subcategory is None or KINDS[pid][1] in types)
            and pid not in request.exclude_product_ids
        ]
        price = request.price
        if price is not None and price.max_amount is not None:
            found = [
                p
                for p in found
                if (p.price_amount < price.max_amount)
                or (not price.max_exclusive and p.price_amount == price.max_amount)
            ]
        return found

    async def execute_candidate_pool(self, resolved: Any, context: Any) -> CandidatePoolResult:
        self.searched.append(resolved)
        return _pool(self._matching(resolved))

    async def execute_forced_pool(self, product_id: int, context: Any) -> CandidatePoolResult:
        return _pool([_product(product_id)] if product_id in self.catalog.available else [])

    async def execute(
        self, resolved: Any, context: Any, *, presentation_limit: int | None = None
    ) -> ProductSearchExecutionResult:
        found = self._matching(resolved)[: presentation_limit or 5]
        return ProductSearchExecutionResult(
            presented_product_ids=tuple(p.product_id for p in found),
            grounding=SearchExecutionGrounding(
                outcome=SearchOutcome.RESULTS if found else SearchOutcome.ZERO_RESULTS,
                products=tuple(
                    to_grounded_product(p, grounding_ref=n, presented_ordinal=n, relaxation_depth=0)
                    for n, p in enumerate(found, start=1)
                ),
                eligible_count=len(found),
                ranked_count=len(found),
                selected_count=len(found),
                presented_count=len(found),
                exact_candidate_count=len(found),
                stop_reason=StopReason.EXACT_SUFFICIENT,
            ),
        )


def _pool(products: Sequence[ProductCandidate]) -> CandidatePoolResult:
    return CandidatePoolResult(
        candidates=tuple(RankedProductCandidate(product=p, relaxation_depth=0) for p in products),
        eligible_count=len(products),
        was_relaxed=False,
        stop_reason=StopReason.EXACT_SUFFICIENT,
        semantic_used=False,
    )


class FakeDesign:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[InteriorDesignRequest] = []

    async def plan(self, request: InteriorDesignRequest) -> InteriorDesignResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return InteriorDesignResult()


class FakeSeatingPlanner:
    async def arrangements(self, **kwargs: Any) -> tuple[Any, ...]:
        return ()


class RecordingOptimizer(BundleOptimizer):
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def optimize(self, request: Any) -> Any:
        self.requests.append(request)
        return super().optimize(request)


def _engine(design: FakeDesign | None = None) -> tuple[RoomEngine, FakeCatalog, FakePipeline]:
    catalog = FakeCatalog()
    pipeline = FakePipeline(catalog)
    engine = RoomEngine(
        design if design is not None else FakeDesign(),
        DesignDiscoveryService(pipeline, TAXONOMY, SEATING),  # type: ignore[arg-type]
        RecordingOptimizer(),
        FakeSeatingPlanner(),
        pipeline,
        catalog,
        catalog,
        ROOMS,
        SEATING,
    )
    return engine, catalog, pipeline


def _inputs(budget: str | None = "10000", **kwargs: Any) -> RoomPlanInputs:
    return RoomPlanInputs(
        room_kind="living_room",
        room_type="living room",
        budget=PriceConstraint(currency="SAR", max_amount=Decimal(budget)) if budget else None,
        pieces=("sofa", "center-table", "rug"),
        **kwargs,
    )


def _ids(state: AgentStateV1) -> list[int]:
    room = state.room_project
    assert room is not None
    return sorted(line.product_id for line in room.bundle_items)


def _piece(facts: dict[str, object], kind: str) -> int:
    pieces = facts["pieces"]
    assert isinstance(pieces, list)
    return int(next(p["piece"] for p in pieces if p["kind"] == kind))


async def test_plan_commits_a_room_of_the_chosen_pieces() -> None:
    engine, _, _ = _engine()
    outcome = await engine.plan(AgentStateV1(), CONTEXT, _inputs())

    assert outcome.code == "ok" and outcome.committed
    assert _ids(outcome.state) == [1, 10, 20]
    room = outcome.state.room_project
    assert room is not None and room.room_kind == "living_room"
    assert room.budget is not None and room.budget.max_amount == Decimal(10000)
    pieces = outcome.facts["pieces"]
    assert isinstance(pieces, list) and len(pieces) == 3
    assert outcome.facts["total"] == Decimal(4300)
    assert outcome.facts["within_budget"] is True
    assert "product_id" not in repr(outcome.facts)
    view = outcome.presentation()
    assert view is not None and [i.grounding_ref for i in view.items] == [1, 2, 3]


async def test_the_budget_is_respected_when_the_room_is_chosen() -> None:
    engine, _, _ = _engine()
    outcome = await engine.plan(AgentStateV1(), CONTEXT, _inputs("3200"))

    assert outcome.committed
    assert _ids(outcome.state) == [2, 11, 20]
    assert outcome.facts["total"] == Decimal(3100)


async def test_an_over_budget_swap_is_only_proposed_unless_allowed() -> None:
    engine, _, _ = _engine()
    planned = await engine.plan(AgentStateV1(), CONTEXT, _inputs("3200"))
    sofa = _piece(planned.facts, "sofa")

    options = await engine.piece_options(planned.state, CONTEXT, sofa)
    assert options.code == "ok"
    listed = options.facts["options"]
    assert isinstance(listed, list) and len(listed) == 1
    assert listed[0]["total_if_swapped"] == Decimal(4100)
    assert listed[0]["over_budget_by"] == Decimal(900)

    held = await engine.swap(options.state, CONTEXT, sofa, 1, allow_over_budget=False)
    assert held.code == "over_budget" and not held.committed
    assert held.state is options.state
    assert held.facts["overage"] == Decimal(900)
    assert held.facts["new_total"] == Decimal(4100)
    assert _ids(held.state) == [2, 11, 20]

    taken = await engine.swap(options.state, CONTEXT, sofa, 1, allow_over_budget=True)
    assert taken.code == "ok" and taken.committed
    assert _ids(taken.state) == [1, 11, 20]
    room = taken.state.room_project
    assert room is not None and room.budget is not None
    assert room.budget.max_amount == Decimal(4100)
    assert taken.facts["budget_raised_from"] == Decimal(3200)
    assert taken.state.room_swap is None


async def test_a_swap_needs_the_options_shown_for_that_piece() -> None:
    engine, _, _ = _engine()
    planned = await engine.plan(AgentStateV1(), CONTEXT, _inputs())

    outcome = await engine.swap(planned.state, CONTEXT, 1, 1, allow_over_budget=True)
    assert outcome.code == "options_expired" and not outcome.committed


async def test_remove_keeps_every_other_piece_the_product_it_is() -> None:
    engine, _, pipeline = _engine()
    planned = await engine.plan(AgentStateV1(), CONTEXT, _inputs())
    pipeline.reverse = True  # a fresh search would now choose differently

    outcome = await engine.remove(planned.state, CONTEXT, _piece(planned.facts, "carpet"))

    assert outcome.code == "ok" and outcome.committed
    assert _ids(outcome.state) == [1, 10]
    assert outcome.facts["total"] == Decimal(3800)
    room = outcome.state.room_project
    assert room is not None and len(room.design_needs) == 2


async def test_a_locked_piece_is_kept_when_the_room_is_revised() -> None:
    engine, _, pipeline = _engine()
    planned = await engine.plan(AgentStateV1(), CONTEXT, _inputs())
    locked = await engine.set_piece_status(
        planned.state, CONTEXT, _piece(planned.facts, "sofa"), "locked"
    )
    assert locked.committed
    pipeline.reverse = True

    revised = await engine.revise(
        locked.state,
        CONTEXT,
        budget=PriceConstraint(currency="SAR", max_amount=Decimal(9000)),
    )

    assert revised.code == "ok" and revised.committed
    room = revised.state.room_project
    assert room is not None
    sofa = [line for line in room.bundle_items if line.product_id == 1]
    assert len(sofa) == 1 and sofa[0].status is BundleItemStatus.LOCKED
    # The unlocked pieces really were chosen again.
    assert _ids(revised.state) == [1, 11, 21]
    assert room.budget is not None and room.budget.max_amount == Decimal(9000)


async def test_owning_a_piece_locks_it_and_takes_it_off_the_bill() -> None:
    engine, _, _ = _engine()
    planned = await engine.plan(AgentStateV1(), CONTEXT, _inputs())

    owned = await engine.set_piece_status(
        planned.state, CONTEXT, _piece(planned.facts, "sofa"), "owned"
    )

    assert owned.committed
    pieces = owned.facts["pieces"]
    assert isinstance(pieces, list)
    sofa = next(p for p in pieces if p["kind"] == "sofa")
    assert sofa["owned"] and sofa["locked"] and sofa["line_total"] is None
    assert owned.facts["total"] == Decimal(1300)


async def test_a_finishing_touch_joins_the_room_with_every_piece_pinned() -> None:
    engine, _, pipeline = _engine()
    planned = await engine.plan(AgentStateV1(), CONTEXT, _inputs("4500"))

    could = await engine.pieces_it_could_take(planned.state, CONTEXT)
    assert "side-table" in could and "rug" not in could

    pipeline.reverse = True
    held = await engine.add_product(planned.state, CONTEXT, 30, allow_over_budget=False)
    assert held.code == "over_budget" and not held.committed
    assert held.facts["overage"] == Decimal(200)

    added = await engine.add_product(planned.state, CONTEXT, 30, allow_over_budget=True)
    assert added.committed and _ids(added.state) == [1, 10, 20, 30]
    assert added.facts["added"] == "service table"

    refused = await engine.add_product(planned.state, CONTEXT, 2, allow_over_budget=True)
    assert refused.code == "not_a_piece_it_could_take"


async def test_around_picks_locks_the_picks_that_belong_in_the_room() -> None:
    engine, _, _ = _engine()
    state = apply_update(
        AgentStateV1(),
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                selected_product_ids=ReplaceItems(items=(2,))
            )
        ),
    )

    outcome = await engine.plan(state, CONTEXT, _inputs(around_picks=True))

    room = outcome.state.room_project
    assert outcome.committed and room is not None
    picked = [line for line in room.bundle_items if line.product_id == 2]
    assert len(picked) == 1 and picked[0].status is BundleItemStatus.LOCKED
    assert 1 not in _ids(outcome.state)  # the locked sofa is the room's seating


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (CatalogUnavailableError(detail="down"), "catalog_unavailable"),
    ],
)
async def test_a_catalog_outage_is_a_code_and_changes_nothing(error: Exception, code: str) -> None:
    engine, catalog, _ = _engine()
    catalog.error = error
    state = AgentStateV1()

    outcome = await engine.plan(state, CONTEXT, _inputs())

    assert outcome.code == code and not outcome.committed
    assert outcome.state is state


async def test_a_design_failure_is_a_code_and_changes_nothing() -> None:
    engine, _, _ = _engine(FakeDesign(error=LLMResponseInvalidError(detail="bad")))
    state = AgentStateV1()

    outcome = await engine.plan(state, CONTEXT, _inputs())

    assert outcome.code == "design_unavailable" and outcome.state is state


async def test_a_vanished_locked_piece_stops_the_revision() -> None:
    engine, catalog, _ = _engine()
    planned = await engine.plan(AgentStateV1(), CONTEXT, _inputs())
    locked = await engine.set_piece_status(planned.state, CONTEXT, 1, "locked")
    catalog.available.discard(1)

    outcome = await engine.revise(locked.state, CONTEXT, seats=4)

    assert outcome.code == "locked_unavailable" and outcome.state is locked.state


async def test_current_renders_the_committed_room_without_optimising() -> None:
    engine, _, _ = _engine()
    planned = await engine.plan(AgentStateV1(), CONTEXT, _inputs())

    current = await engine.current(planned.state, CONTEXT)

    assert current is not None and current.code == "ok"
    assert current.facts["total"] == planned.facts["total"]
    assert await engine.current(AgentStateV1(), CONTEXT) is None


async def test_a_room_with_no_pieces_named_keeps_what_was_said_and_builds_nothing() -> None:
    engine, _, _ = _engine()
    inputs = _inputs().model_copy(update={"pieces": None})

    outcome = await engine.plan(AgentStateV1(), CONTEXT, inputs)

    assert outcome.code == "choose_pieces"
    room = outcome.state.room_project
    assert room is not None and not room.bundle_items
    assert room.budget is not None and room.budget.max_amount == Decimal(10000)
    could_hold = outcome.facts["room_could_hold"]
    assert isinstance(could_hold, list) and could_hold
    assert {p["tier"] for p in could_hold} <= {"essential", "recommended", "optional"}

    built = await engine.plan(outcome.state, CONTEXT, _inputs(budget=None))

    assert built.code == "ok" and _ids(built.state) == [1, 10, 20]
    assert built.state.room_project is not None
    assert built.state.room_project.budget is not None


async def test_choosing_another_rooms_pieces_never_gives_up_a_designed_room() -> None:
    engine, _, _ = _engine()
    designed = await engine.plan(AgentStateV1(), CONTEXT, _inputs())
    bedroom = RoomPlanInputs(room_kind="bedroom", room_type="bedroom")

    outcome = await engine.plan(designed.state, CONTEXT, bedroom)

    assert outcome.code == "choose_pieces" and not outcome.committed
    assert outcome.facts["inputs_kept"] is False
    assert _ids(outcome.state) == [1, 10, 20]
