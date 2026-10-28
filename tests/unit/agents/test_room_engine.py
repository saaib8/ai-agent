"""The room engine: design, swap, remove, lock and add, against hand fakes.

The optimiser, the discovery bridge and the registries are the real ones; only
the catalog and the search pipeline are faked, so what is asserted is the
engine's own behaviour - what it builds from Nora's design, what it commits,
what it pins and what it refuses.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from app.agents.operations.rooms import RoomDesignInputs, RoomEngine
from app.core.exceptions import CatalogUnavailableError
from app.schemas.agent_state import AgentStateV1, BundleItemStatus, SwapBudgetOfferState
from app.schemas.agent_updates import AgentStateUpdate, ProductInteractionUpdate, ReplaceItems
from app.schemas.design import (
    DesignCategoryNeed,
    DesignPriority,
    InteriorDesignResult,
    PieceDesign,
)
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import PriceConstraint, SeatingCapacityConstraint
from app.schemas.geometry import RoomGeometry, RoomMeasurement, RoomMeasurementRole
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
    40: ("seating", "chair", "700"),
    41: ("seating", "lounge-chair", "900"),
    42: ("seating", "lounge-chair", "1400"),
}
COLOURS = {40: "Olive", 41: "Beige", 42: "Beige"}
LONGER_CM = {1: 260, 2: 200}


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
        dimensions=(
            NormalisedDimensions(
                status=DimensionStatus.NORMALISED,
                length_cm=Decimal(LONGER_CM[product_id]),
                width_cm=Decimal(95),
            )
            if product_id in LONGER_CM
            else NormalisedDimensions(status=DimensionStatus.ABSENT)
        ),
        main_color=COLOURS.get(product_id, "Beige"),
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
        self.similarity: dict[int, float] = {}
        """Look scores Pinecone would give, by product; none by default."""
        self.searched: list[Any] = []
        self.forced_reads: list[tuple[int, ...]] = []

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
        return _pool(self._matching(resolved), self.similarity)

    async def execute_forced_pool(self, product_id: int, context: Any) -> CandidatePoolResult:
        return _pool([_product(product_id)] if product_id in self.catalog.available else [])

    async def execute_forced_pools(
        self, product_ids: Sequence[int], context: Any
    ) -> dict[int, CandidatePoolResult]:
        self.forced_reads.append(tuple(product_ids))
        return {p: await self.execute_forced_pool(p, context) for p in product_ids}

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


def _pool(
    products: Sequence[ProductCandidate], similarity: dict[int, float] | None = None
) -> CandidatePoolResult:
    return CandidatePoolResult(
        candidates=tuple(
            RankedProductCandidate(
                product=p,
                relaxation_depth=0,
                similarity=(similarity or {}).get(p.product_id),
            )
            for p in products
        ),
        eligible_count=len(products),
        was_relaxed=False,
        stop_reason=StopReason.EXACT_SUFFICIENT,
        semantic_used=False,
    )


class RecordingOptimizer(BundleOptimizer):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[Any] = []

    def optimize(self, request: Any) -> Any:
        self.requests.append(request)
        return super().optimize(request)


def _engine() -> tuple[RoomEngine, FakeCatalog, FakePipeline]:
    catalog = FakeCatalog()
    pipeline = FakePipeline(catalog)
    engine = RoomEngine(
        DesignDiscoveryService(pipeline, TAXONOMY, SEATING),  # type: ignore[arg-type]
        RecordingOptimizer(),
        pipeline,
        catalog,
        catalog,
        ROOMS,
        SEATING,
    )
    return engine, catalog, pipeline


def _need(
    category: str,
    kind: str,
    role: str,
    priority: DesignPriority = DesignPriority.REQUIRED,
    **design: Any,
) -> DesignCategoryNeed:
    return DesignCategoryNeed(
        commerce_category=category,
        commerce_subcategory=kind,
        priority=priority,
        design=PieceDesign(role=role, **design),
    )


ROOM = (
    _need("seating", "sofa", "main sofa"),
    _need("tables", "center-table", "coffee table"),
    _need("decor", "carpet", "rug"),
)


def _inputs(
    budget: str | None = "10000",
    needs: tuple[DesignCategoryNeed, ...] = ROOM,
    **kwargs: Any,
) -> RoomDesignInputs:
    return RoomDesignInputs(
        room_kind="living_room",
        room_type="living room",
        same_room=kwargs.pop("same_room", False),
        budget=PriceConstraint(currency="SAR", max_amount=Decimal(budget)) if budget else None,
        plan=InteriorDesignResult(needs=needs),
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


async def test_design_commits_a_room_of_the_designed_pieces() -> None:
    engine, _, _ = _engine()
    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs())

    assert outcome.code == "ok" and outcome.committed
    assert _ids(outcome.state) == [1, 10, 20]
    room = outcome.state.room_project
    assert room is not None and room.room_kind == "living_room"
    assert room.budget is not None and room.budget.max_amount == Decimal(10000)
    assert [n.design.role for n in room.design_needs if n.design] == [
        "main sofa",
        "coffee table",
        "rug",
    ]
    pieces = outcome.facts["pieces"]
    assert isinstance(pieces, list) and len(pieces) == 3
    assert outcome.facts["total"] == Decimal(4300)
    assert outcome.facts["within_budget"] is True
    assert "product_id" not in repr(outcome.facts)
    view = outcome.presentation()
    assert view is not None and [i.grounding_ref for i in view.items] == [1, 2, 3]


async def test_the_budget_is_respected_when_the_room_is_chosen() -> None:
    engine, _, _ = _engine()
    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs("3200"))

    assert outcome.committed
    assert _ids(outcome.state) == [2, 11, 20]
    assert outcome.facts["total"] == Decimal(3100)


async def test_a_chaise_or_recliner_never_counts_as_a_seat() -> None:
    engine, _, _ = _engine()
    assert engine._room_seats(None) is None
    needs = (
        DesignCategoryNeed(
            commerce_category="seating",
            commerce_subcategory="sofa",
            priority=DesignPriority.REQUIRED,
            seating_capacity=SeatingCapacityConstraint(min_capacity=3, max_capacity=3),
        ),
    )
    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000", needs))
    # The fake sofas record no seat count: none is invented.
    assert "seats" not in outcome.facts
    assert not SEATING.counts_as_seats("chaise-lounge")


async def test_an_over_budget_swap_is_only_proposed_unless_allowed() -> None:
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("3200"))
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


async def test_a_piece_of_several_types_offers_every_type_and_swaps_across_them() -> None:
    engine, _, _ = _engine()
    needs = (_need("seating", "chair", "reading chair", alternatives=("lounge-chair",)),)
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("5000", needs))

    options = await engine.piece_options(planned.state, CONTEXT, 1)
    names = [o["name"] for o in options.facts["options"]]  # type: ignore[index, union-attr]
    taken = await engine.swap(options.state, CONTEXT, 1, 1, allow_over_budget=True)

    assert {n.split()[0] for n in names} == {"lounge-chair"}
    assert taken.committed and _ids(taken.state)[0] in (41, 42)


async def test_a_swap_needs_the_options_shown_for_that_piece() -> None:
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs())

    outcome = await engine.swap(planned.state, CONTEXT, 1, 1, allow_over_budget=True)
    assert outcome.code == "options_expired" and not outcome.committed


async def test_remove_keeps_every_other_piece_the_product_it_is() -> None:
    engine, _, pipeline = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs())
    pipeline.reverse = True  # a fresh search would now choose differently

    outcome = await engine.remove(planned.state, CONTEXT, _piece(planned.facts, "carpet"))

    assert outcome.code == "ok" and outcome.committed
    assert _ids(outcome.state) == [1, 10]
    assert outcome.facts["total"] == Decimal(3800)
    room = outcome.state.room_project
    assert room is not None and len(room.design_needs) == 2


async def test_a_locked_piece_is_kept_when_the_room_is_designed_again() -> None:
    engine, _, pipeline = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs())
    locked = await engine.set_piece_status(
        planned.state, CONTEXT, _piece(planned.facts, "sofa"), "locked"
    )
    assert locked.committed
    pipeline.reverse = True

    revised = await engine.design(locked.state, CONTEXT, _inputs("9000", same_room=True))

    assert revised.code == "ok" and revised.committed
    room = revised.state.room_project
    assert room is not None
    sofa = [line for line in room.bundle_items if line.product_id == 1]
    assert len(sofa) == 1 and sofa[0].status is BundleItemStatus.LOCKED
    # The unlocked pieces really were chosen again.
    assert _ids(revised.state) == [1, 11, 21]
    assert room.budget is not None and room.budget.max_amount == Decimal(9000)


async def test_another_room_starts_fresh() -> None:
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs())
    bedroom = RoomDesignInputs(
        room_kind="bedroom",
        room_type="bedroom",
        same_room=False,
        plan=InteriorDesignResult(needs=(_need("decor", "carpet", "rug"),)),
    )

    held = await engine.design(planned.state, CONTEXT, bedroom)
    assert held.code == "another_room_in_progress" and not held.committed
    assert held.facts["room_on_record"] == "living room"

    outcome = await engine.design(
        planned.state, CONTEXT, bedroom.model_copy(update={"replace_room": True})
    )

    room = outcome.state.room_project
    assert outcome.committed and room is not None and room.room_kind == "bedroom"
    assert room.budget is None and _ids(outcome.state) == [20]


async def test_owning_a_piece_locks_it_and_takes_it_off_the_bill() -> None:
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs())

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
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("4500"))

    pipeline.reverse = True
    held = await engine.add_product(planned.state, CONTEXT, 30, allow_over_budget=False)
    assert held.code == "over_budget" and not held.committed
    assert held.facts["overage"] == Decimal(200)

    added = await engine.add_product(planned.state, CONTEXT, 30, allow_over_budget=True)
    assert added.committed and _ids(added.state) == [1, 10, 20, 30]
    assert added.facts["added"] == "service table"


async def test_two_pieces_in_place_of_one_are_one_change_with_one_total() -> None:
    engine, _, pipeline = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("4500"))
    carpet = _piece(planned.facts, "carpet")
    pipeline.reverse = True

    held = await engine.change(
        planned.state,
        CONTEXT,
        remove_pieces=(carpet,),
        add_products=(21, 30),
        allow_over_budget=False,
    )
    assert held.code == "over_budget" and not held.committed
    assert held.state is planned.state
    assert held.facts["new_total"] == Decimal(5100) and held.facts["overage"] == Decimal(600)

    changed = await engine.change(
        planned.state,
        CONTEXT,
        remove_pieces=(carpet,),
        add_products=(21, 30),
        allow_over_budget=True,
    )
    room = changed.state.room_project
    assert changed.committed and room is not None
    assert _ids(changed.state) == [1, 10, 21, 30]
    assert room.budget is not None and room.budget.max_amount == Decimal(5100)
    before = planned.state.room_project
    assert before is not None
    old_carpet = next(n for n in before.design_needs if n.commerce_subcategory == "carpet")
    new_carpet = next(n for n in room.design_needs if n.commerce_subcategory == "carpet")
    side_table = next(n for n in room.design_needs if n.commerce_subcategory == "service-table")
    # The carpet put in takes the carpet's role; the side table is a touch of its own.
    assert new_carpet.priority is old_carpet.priority
    assert side_table.priority is DesignPriority.OPTIONAL


async def test_a_change_within_budget_is_applied_at_once() -> None:
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000"))

    changed = await engine.change(
        planned.state,
        CONTEXT,
        remove_pieces=(),
        add_products=(30, 41),
        allow_over_budget=False,
    )

    assert changed.committed and {30, 41} <= set(_ids(changed.state))


async def test_a_change_refuses_what_it_cannot_do() -> None:
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs())
    sofa = _piece(planned.facts, "sofa")

    nothing = await engine.change(
        planned.state, CONTEXT, remove_pieces=(), add_products=(), allow_over_budget=False
    )
    twice = await engine.change(
        planned.state, CONTEXT, remove_pieces=(), add_products=(1,), allow_over_budget=False
    )
    missing = await engine.change(
        planned.state, CONTEXT, remove_pieces=(99,), add_products=(30,), allow_over_budget=False
    )

    assert nothing.code == "nothing_to_change"
    assert twice.code == "already_in_room"
    assert missing.code == "piece_not_found"
    # The sofa itself may come out and go back in as part of one change.
    same = await engine.change(
        planned.state, CONTEXT, remove_pieces=(sofa,), add_products=(1,), allow_over_budget=True
    )
    assert same.committed and 1 in _ids(same.state)


def _picked(*ids: int) -> AgentStateV1:
    return apply_update(
        AgentStateV1(),
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(
                selected_product_ids=ReplaceItems(items=ids)
            )
        ),
    )


async def test_around_picks_locks_the_picks_that_are_pieces_of_the_room() -> None:
    engine, _, _ = _engine()

    outcome = await engine.design(_picked(2, 30), CONTEXT, _inputs(around_picks=True))

    room = outcome.state.room_project
    assert outcome.committed and room is not None
    picked = [line for line in room.bundle_items if line.product_id == 2]
    assert len(picked) == 1 and picked[0].status is BundleItemStatus.LOCKED
    assert 1 not in _ids(outcome.state)  # the locked sofa is the room's seating
    assert 30 not in _ids(outcome.state)  # a side table is not one of its pieces


async def test_a_piece_from_a_pick_is_that_pick() -> None:
    engine, _, _ = _engine()

    outcome = await engine.design(_picked(30, 2), CONTEXT, _inputs(pick_places={0: 2}))

    assert outcome.committed and 2 in _ids(outcome.state) and 1 not in _ids(outcome.state)
    missing = await engine.design(_picked(2), CONTEXT, _inputs(pick_places={0: 3}))
    assert missing.code == "pick_not_in_cart" and not missing.committed


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

    outcome = await engine.design(state, CONTEXT, _inputs())

    assert outcome.code == code and not outcome.committed
    assert outcome.state is state


async def test_a_vanished_locked_piece_stops_the_new_design() -> None:
    engine, catalog, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs())
    locked = await engine.set_piece_status(planned.state, CONTEXT, 1, "locked")
    catalog.available.discard(1)

    outcome = await engine.design(locked.state, CONTEXT, _inputs(same_room=True))

    assert outcome.code == "locked_unavailable" and outcome.state is locked.state


async def test_current_renders_the_committed_room_without_optimising() -> None:
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs())

    current = await engine.current(planned.state, CONTEXT)

    assert current is not None and current.code == "ok"
    assert current.facts["total"] == planned.facts["total"]
    assert await engine.current(AgentStateV1(), CONTEXT) is None


async def test_any_stocked_kind_may_finish_a_room() -> None:
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000"))

    # A lounge chair is in no living-room guide entry of its own: still a piece.
    added = await engine.add_product(planned.state, CONTEXT, 41, allow_over_budget=False)

    assert added.committed and 41 in _ids(added.state)


async def test_owning_a_piece_never_re_chooses_the_rest() -> None:
    engine, _, pipeline = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs())
    pipeline.reverse = True

    owned = await engine.set_piece_status(planned.state, CONTEXT, 1, "owned")

    assert _ids(owned.state) == _ids(planned.state)


async def _room_options() -> tuple[RoomEngine, AgentStateV1]:
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("3200"))
    options = await engine.piece_options(planned.state, CONTEXT, _piece(planned.facts, "sofa"))
    return engine, options.state


async def test_room_options_on_screen_belong_to_their_piece() -> None:
    from app.agents.operations.rooms import open_room_options
    from app.agents.payloads import OptionsShown
    from app.agents.screen import presentation
    from app.agents.work import ToolRecord, ToolStatus, TurnWork
    from app.schemas.agent_persona import AgentPersona
    from app.schemas.language import ReplyLanguage

    engine, state = await _room_options()
    piece = open_room_options(state)
    assert piece is not None
    work = TurnWork.starting(state)
    shown = (await engine.piece_options(state, CONTEXT, piece)).products
    work.record(
        ToolRecord(
            agent=AgentPersona.NORA,
            tool="room_piece_options",
            status=ToolStatus.OK,
            payload=OptionsShown(products=shown),
        )
    )

    screen = presentation(work, state, (), ReplyLanguage.EN)

    assert screen is not None and screen.product_source == "room_options"
    assert screen.options_for_piece == piece


async def test_an_agent_cannot_put_a_room_option_in_the_cart_unasked() -> None:
    from app.agents.work import TurnWork
    from app.schemas.resolution import ResolvedProductReference

    from tests.unit.agents.fakes import (
        FakeHydration,
        FakeReferences,
        agent_services,
        real_loop,
        tool_context,
    )

    _, state = await _room_options()
    option = state.product_interaction.presented_product_ids[0]
    services = agent_services(
        hydration=FakeHydration(available=(option,)),
        references=FakeReferences(default=ResolvedProductReference(product_id=option)),
    )
    tool = real_loop()._registry.get("add_to_cart")
    assert tool is not None and tool.handler is not None
    item = {"items": [{"which": {"kind": "presented_ordinal", "position": 1}}]}

    refused = await tool.handler(
        tool.args.model_validate(item), tool_context(services), TurnWork.starting(state)
    )
    asked = await tool.handler(
        tool.args.model_validate({**item, "instead_of_room": True}),
        tool_context(services),
        TurnWork.starting(state),
    )

    assert refused.code == "option_is_for_the_room"
    assert refused.facts["use_instead"] == "swap_room_piece"
    assert option in asked.state.product_interaction.selected_product_ids


async def test_a_lock_stays_its_piece_when_the_room_is_built_again() -> None:
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs())
    sofa = _piece(planned.facts, "sofa")
    locked = await engine.set_piece_status(planned.state, CONTEXT, sofa, "locked")

    added = await engine.add_product(locked.state, CONTEXT, 30, allow_over_budget=True)
    options = await engine.piece_options(added.state, CONTEXT, _piece(added.facts, "sofa"))

    assert options.code == "ok"  # still the room's sofa, so still swappable


async def test_one_short_wall_never_limits_a_piece_placed_elsewhere() -> None:
    engine, _, _ = _engine()
    room = RoomGeometry(
        measurements=(
            RoomMeasurement(role=RoomMeasurementRole.ROOM_LENGTH, centimetres=Decimal(500)),
            RoomMeasurement(role=RoomMeasurementRole.ROOM_WIDTH, centimetres=Decimal(400)),
            RoomMeasurement(
                role=RoomMeasurementRole.USABLE_WALL, centimetres=Decimal(180), label="TV wall"
            ),
        )
    )
    needs = (_need("seating", "sofa", "main sofa"),)

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000", needs, geometry=room))

    assert _ids(outcome.state) == [1]  # the 260 cm sofa fits along the 500 cm side


async def test_a_noted_room_is_the_room_being_designed() -> None:
    from app.schemas.agent_state import RoomProjectState

    engine, _, _ = _engine()
    noted = AgentStateV1(
        room_project=RoomProjectState(room_type="living room", regular_seating_count=6)
    )

    outcome = await engine.design(noted, CONTEXT, _inputs())

    room = outcome.state.room_project
    assert outcome.committed and room is not None and room.regular_seating_count == 6


async def test_a_pick_that_is_not_the_piece_is_refused() -> None:
    engine, _, _ = _engine()

    outcome = await engine.design(_picked(30), CONTEXT, _inputs(pick_places={0: 1}))

    assert outcome.code == "pick_not_this_piece" and not outcome.committed
    assert outcome.facts["pick_is"] == "service table"


async def test_a_pick_with_no_seat_count_still_fills_a_seated_piece() -> None:
    engine, _, _ = _engine()
    needs = (
        DesignCategoryNeed(
            commerce_category="seating",
            commerce_subcategory="sofa",
            priority=DesignPriority.REQUIRED,
            seating_capacity=SeatingCapacityConstraint(min_capacity=3, max_capacity=3),
            design=PieceDesign(role="main sofa"),
        ),
    )

    outcome = await engine.design(_picked(2), CONTEXT, _inputs("10000", needs, pick_places={0: 1}))

    # The pick is the sofa: never bought a second time beside it.
    assert _ids(outcome.state) == [2]


async def test_a_pieces_shorter_side_decides_its_size() -> None:
    engine, _, _ = _engine()
    needs = (_need("seating", "sofa", "main sofa", shorter_min_cm=100, shorter_max_cm=120),)

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000", needs))

    # Both sofas list a 95 cm shorter side: neither is in range, so it says so.
    piece = outcome.facts["pieces"][0]  # type: ignore[index]
    assert piece["size_in_range"] is False


async def test_a_room_says_how_much_of_its_budget_is_left() -> None:
    engine, _, _ = _engine()

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000"))

    assert outcome.facts["total"] == Decimal(4300)
    assert outcome.facts["budget_left"] == Decimal(5700)


# ── the look by meaning, fit by Nora, premium only when it looks as right ──


async def test_a_designed_pieces_look_is_search_wording_never_a_stored_name() -> None:
    engine, _, pipeline = _engine()
    needs = (
        _need(
            "seating",
            "chair",
            "reading chair",
            alternatives=("lounge-chair",),
            colours=("Olive",),
            styles=("Modern_Classic",),
        ),
    )

    await engine.design(AgentStateV1(), CONTEXT, _inputs("5000", needs))

    resolved = pipeline.searched[-1]
    # Ranked by meaning (Pinecone), so a Sage chair can serve an olive room.
    assert resolved.semantic_text == "Olive Modern Classic"
    assert resolved.semantic_preferences == ()


async def test_colour_never_decides_which_piece_is_chosen_first() -> None:
    engine, _, _ = _engine()
    needs = (
        _need(
            "seating", "chair", "reading chair", alternatives=("lounge-chair",), colours=("Beige",)
        ),
    )

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs("5000", needs))

    # No stored-name match: the search's own order decides (here, by id).
    assert _ids(outcome.state) == [40]


async def test_size_comes_before_a_share() -> None:
    engine, _, pipeline = _engine()
    pipeline.similarity = {1: 0.9, 2: 0.9}
    needs = (
        _need(
            "seating", "sofa", "main sofa", longer_min_cm=190, longer_max_cm=210, share_percent=90
        ),
    )

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs("4000", needs))

    # The 200 cm sofa is in range; a 3,600 share never buys the 260 cm one.
    assert _ids(outcome.state) == [2]


async def test_a_bigger_share_buys_a_dearer_piece_only_when_it_looks_as_right() -> None:
    needs = (_need("seating", "sofa", "main sofa", share_percent=80),)

    async def chosen(similarity: dict[int, float]) -> list[int]:
        engine, _, pipeline = _engine()
        pipeline.reverse = True  # the 2,000 sofa is the best match
        pipeline.similarity = similarity
        outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs("4000", needs))
        return _ids(outcome.state)

    # As close a look: the share (3,200) buys the dearer 3,000 sofa.
    assert await chosen({1: 0.9, 2: 0.9}) == [1]
    # A weaker look, or no score at all: the best match stays.
    assert await chosen({1: 0.8, 2: 0.9}) == [2]
    assert await chosen({}) == [2]


async def test_a_best_match_above_its_share_is_never_pulled_down() -> None:
    engine, _, pipeline = _engine()
    pipeline.similarity = {1: 0.9, 2: 0.9}
    needs = (_need("seating", "sofa", "main sofa", share_percent=10),)

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000", needs))

    # The best match costs 3,000 against a 1,000 share: it stays.
    assert _ids(outcome.state) == [1]


async def test_no_piece_is_excluded_or_flagged_for_fit() -> None:
    engine, _, _ = _engine()
    walls = RoomGeometry(
        measurements=(
            RoomMeasurement(
                role=RoomMeasurementRole.USABLE_WALL, centimetres=Decimal(240), label="TV wall"
            ),
        )
    )
    needs = (_need("seating", "sofa", "main sofa", wall="TV wall"),)

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000", needs, geometry=walls))

    # Fit is Nora's judgement: the 260 cm sofa is chosen, its size shown to her.
    assert _ids(outcome.state) == [1]
    piece = outcome.facts["pieces"][0]  # type: ignore[index]
    assert piece["size"]["longer_side"] == "260"
    assert not any("fit" in key or "floor" in key for key in outcome.facts)


async def test_each_piece_is_reported_with_its_colour_size_and_share() -> None:
    engine, _, _ = _engine()
    needs = (
        _need("seating", "chair", "reading chair", colours=("Ruby",), share_percent=20),
        _need("seating", "sofa", "main sofa", longer_min_cm=180, longer_max_cm=220),
    )

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs("5000", needs))

    pieces = {p["role"]: p for p in outcome.facts["pieces"]}  # type: ignore[union-attr]
    chair, sofa = pieces["reading chair"], pieces["main sofa"]
    assert chair["colour"] == "Olive" and "colour_as_designed" not in chair
    assert chair["share_planned_percent"] == 20 and chair["share_used_percent"] == 14
    assert sofa["size_in_range"] is True
    assert outcome.facts["colours_in_room"] == ["reading chair: Olive", "main sofa: Beige"]


async def test_revising_one_piece_finds_only_it_again() -> None:
    engine, _, pipeline = _engine()
    needs = (
        _need("seating", "chair", "reading chair", alternatives=("lounge-chair",)),
        _need("tables", "center-table", "coffee table"),
    )
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("5000", needs))
    assert _ids(planned.state) == [10, 40]
    pipeline.reverse = True  # a fresh search would choose another table

    revised = await engine.revise_piece(
        planned.state,
        CONTEXT,
        1,
        PieceDesign(role="reading chair", alternatives=("lounge-chair",), avoid_colours=("Olive",)),
    )

    assert revised.committed
    # Only the chair was found again, away from Olive; the table stays the same.
    assert _ids(revised.state) in ([10, 41], [10, 42])
    room = revised.state.room_project
    assert room is not None and room.design_needs[0].design.avoid_colours == ("Olive",)


async def test_revising_a_locked_piece_releases_its_lock() -> None:
    engine, _, _ = _engine()
    needs = (_need("seating", "chair", "reading chair", alternatives=("lounge-chair",)),)
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("5000", needs))
    locked = await engine.set_piece_status(planned.state, CONTEXT, 1, "locked")

    revised = await engine.revise_piece(
        locked.state,
        CONTEXT,
        1,
        PieceDesign(role="reading chair", alternatives=("lounge-chair",), avoid_colours=("Olive",)),
    )

    assert revised.committed and len(_ids(revised.state)) == 1
    assert _ids(revised.state)[0] in (41, 42)


async def test_a_plan_with_two_sofas_builds_both() -> None:
    engine, _, _ = _engine()
    needs = (
        _need("seating", "sofa", "sofa along the wall"),
        _need("seating", "sofa", "sofa at a right angle"),
    )

    outcome = await engine.design(
        AgentStateV1(),
        CONTEXT,
        _inputs("10000", needs, layout_idea="an L in the corner by the window"),
    )

    room = outcome.state.room_project
    assert room is not None
    sofas = [line for line in room.bundle_items if line.need_id is not None]
    # Two sofas, one per piece of her plan - several are as ordinary as one.
    assert len(sofas) == 2 and len({line.need_id for line in sofas}) == 2
    assert room is not None and room.layout_idea == "an L in the corner by the window"
    assert len(room.design_needs) == 2


def test_a_held_change_keeps_every_product_it_puts_in() -> None:
    offer = SwapBudgetOfferState(
        added_piece="art canvas",
        chosen_product_id=21,
        bundle_revision=3,
        removed_pieces=(4,),
        added_product_ids=(21, 30),
    )
    assert offer.is_change and offer.added_product_ids == (21, 30)
    with pytest.raises(ValueError, match="puts one in"):
        SwapBudgetOfferState(
            added_piece="art canvas", chosen_product_id=21, bundle_revision=3, removed_pieces=(4,)
        )


async def test_the_pinned_pieces_are_read_together_not_one_query_each() -> None:
    engine, _, pipeline = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000"))
    pipeline.forced_reads.clear()

    added = await engine.add_product(planned.state, CONTEXT, 30, allow_over_budget=True)

    assert added.committed
    assert len(pipeline.forced_reads) == 1
    assert set(pipeline.forced_reads[0]) == set(_ids(added.state))


async def test_two_revisions_are_one_change_committed_once() -> None:
    from app.agents.operations.rooms import PieceRevision

    engine, _, pipeline = _engine()
    needs = (
        _need("seating", "chair", "reading chair", alternatives=("lounge-chair",)),
        _need("tables", "center-table", "coffee table"),
        _need("decor", "carpet", "rug"),
    )
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000", needs))
    revision_before = planned.state.room_project.bundle_revision  # type: ignore[union-attr]
    pipeline.searched.clear()
    pipeline.forced_reads.clear()

    revised = await engine.revise_pieces(
        planned.state,
        CONTEXT,
        (
            PieceRevision(
                1,
                PieceDesign(
                    role="reading chair", alternatives=("lounge-chair",), avoid_colours=("Olive",)
                ),
            ),
            PieceRevision(3, PieceDesign(role="rug", colours=("Beige",))),
        ),
    )

    room = revised.state.room_project
    assert revised.committed and room is not None
    assert room.bundle_revision == revision_before + 1  # one commit, not two
    assert len(pipeline.searched) == 2  # both pieces found again
    assert len(pipeline.forced_reads) == 1  # the coffee table read once, pinned
    assert room.design_needs[0].design.avoid_colours == ("Olive",)
    assert room.design_needs[2].design.colours == ("Beige",)


async def test_one_bad_revision_changes_nothing() -> None:
    from app.agents.operations.rooms import PieceRevision

    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000"))

    refused = await engine.revise_pieces(
        planned.state,
        CONTEXT,
        (
            PieceRevision(1, PieceDesign(role="main sofa", colours=("Beige",))),
            PieceRevision(9, PieceDesign(role="nothing")),
        ),
    )
    twice = await engine.revise_pieces(
        planned.state,
        CONTEXT,
        (
            PieceRevision(1, PieceDesign(role="main sofa")),
            PieceRevision(1, PieceDesign(role="main sofa", colours=("Beige",))),
        ),
    )

    assert refused.code == "piece_not_found" and refused.state is planned.state
    assert twice.code == "piece_revised_twice" and twice.state is planned.state


async def test_two_revisions_together_choose_what_two_in_a_row_would() -> None:
    from app.agents.operations.rooms import PieceRevision

    chair = PieceDesign(
        role="reading chair", alternatives=("lounge-chair",), avoid_colours=("Olive",)
    )
    rug = PieceDesign(role="rug", colours=("Beige",))
    needs = (
        _need("seating", "chair", "reading chair", alternatives=("lounge-chair",)),
        _need("tables", "center-table", "coffee table"),
        _need("decor", "carpet", "rug"),
    )
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000", needs))

    together = await engine.revise_pieces(
        planned.state, CONTEXT, (PieceRevision(1, chair), PieceRevision(3, rug))
    )
    first = await engine.revise_piece(planned.state, CONTEXT, 1, chair)
    in_a_row = await engine.revise_piece(first.state, CONTEXT, 3, rug)

    assert _ids(together.state) == _ids(in_a_row.state)


async def test_two_revisions_never_take_the_room_over_its_budget() -> None:
    """A size range orders, never excludes: when the piece the revision prefers
    is too dear, the room keeps one it can afford rather than breaking budget."""
    from app.agents.operations.rooms import PieceRevision

    engine, _, _ = _engine()
    # 3300 buys the shorter sofa; the revision prefers the longer, dearer one.
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("3300"))
    assert 2 in _ids(planned.state)

    revised = await engine.revise_pieces(
        planned.state,
        CONTEXT,
        (
            PieceRevision(1, PieceDesign(role="main sofa", longer_min_cm=250)),
            PieceRevision(3, PieceDesign(role="rug", colours=("Beige",))),
        ),
    )

    assert revised.committed and 2 in _ids(revised.state)
    assert revised.facts["total"] <= Decimal(3300)


# ── a piece's options keep its size floor ─────────────────────────────────


class _RecordingPipeline(FakePipeline):
    def __init__(self, catalog: FakeCatalog) -> None:
        super().__init__(catalog)
        self.executed: list[Any] = []

    async def execute(
        self, resolved: Any, context: Any, *, presentation_limit: int | None = None
    ) -> ProductSearchExecutionResult:
        self.executed.append(resolved)
        return await super().execute(resolved, context, presentation_limit=presentation_limit)


def _sized_engine() -> tuple[RoomEngine, _RecordingPipeline]:
    from app.taxonomy.dimensions import load_dimension_semantics

    catalog = FakeCatalog()
    pipeline = _RecordingPipeline(catalog)
    engine = RoomEngine(
        DesignDiscoveryService(pipeline, TAXONOMY, SEATING),  # type: ignore[arg-type]
        RecordingOptimizer(),
        pipeline,
        catalog,
        catalog,
        ROOMS,
        SEATING,
        load_dimension_semantics(taxonomy=TAXONOMY, by_side=True),
    )
    return engine, pipeline


SIZED_ROOM = (
    _need("seating", "sofa", "main sofa", longer_min_cm=220),
    _need("tables", "center-table", "coffee table"),
)


async def test_a_pieces_options_keep_the_size_it_was_designed_to_be_at_least() -> None:
    engine, pipeline = _sized_engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000", SIZED_ROOM))

    found = await engine.piece_options(planned.state, CONTEXT, _piece(planned.facts, "sofa"))

    (floor,) = pipeline.executed[-1].request.dimensions
    assert floor.kind == "min" and floor.min_cm == Decimal(220)
    assert pipeline.executed[-1].semantics.dimensions[0].strength == "locked"
    assert found.facts["kept_at_least_cm"] == {"longer_side": 220}


async def test_nora_can_lower_the_floor_when_they_open_up() -> None:
    engine, pipeline = _sized_engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000", SIZED_ROOM))

    await engine.piece_options(
        planned.state, CONTEXT, _piece(planned.facts, "sofa"), longer_at_least_cm=180
    )

    (floor,) = pipeline.executed[-1].request.dimensions
    assert floor.min_cm == Decimal(180)


async def test_no_floor_where_the_piece_has_none_or_the_data_cannot_answer() -> None:
    engine, pipeline = _sized_engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs("10000", SIZED_ROOM))

    found = await engine.piece_options(planned.state, CONTEXT, 2)

    assert pipeline.executed[-1].request.dimensions == ()
    assert "kept_at_least_cm" not in found.facts
