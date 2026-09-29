"""The add-ons offered after a room is built (CLAUDE.md 10.2, 27).

A salesperson with a room on the table suggests what would finish it - never a
second bed, never a swap of something just chosen - and one more if the
customer takes it. The pure half (which pieces, which product, which reasons)
is tested directly; the coordinator half through its own methods with small
fakes: a catalog that returns products by type, and an optimiser that prices
the room it is handed, so the figures are real sums.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import AgentAction
from app.schemas.agent_state import (
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    RoomDesignNeedState,
    RoomProjectState,
    RoomUpgradeOfferState,
    UpgradeReason,
)
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.bundle import BundleLine, BundleStatus, RoomBundle
from app.schemas.bundle_action import (
    BundleActionRequest,
    UpgradeAcceptAction,
    UpgradeDeclineAction,
)
from app.schemas.design import DesignPriority
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import PriceConstraint, ProductSearchRequest
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    ResolvedSearch,
    SemanticPreference,
)
from app.schemas.relaxation import StopReason
from app.schemas.resolution import CandidatePoolResult, RankedProductCandidate
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerCatalogCapability
from app.services.room_presentation import UPGRADE_CHOICES
from app.services.room_upgrade import (
    RoomUpgradePolicy,
    add_on_pieces,
    add_on_reasons,
    choose_add_on,
)
from app.services.turn_coordinator import _bundle_action_decision
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics
from pydantic import TypeAdapter

from tests.unit.test_turn_coordinator import (
    CONTEXT,
    FakeCapabilities,
    FakeDesignDiscovery,
    _coordinator,
)

TAXONOMY = load_taxonomy()
SEATING = load_seating_semantics(taxonomy=TAXONOMY)
ROOMS = load_room_pieces(taxonomy=TAXONOMY, seating=SEATING)
BEDROOM = ROOMS.template("bedroom")
assert BEDROOM is not None
POLICY = RoomUpgradePolicy(max_over_budget=Decimal("0.15"))
SAR = "SAR"

BED, NIGHTSTAND, MIRROR, GOLD_MIRROR, FLOOR_LAMP, DEAR_MIRROR = 101, 102, 301, 302, 303, 304

GOLD = SemanticPreference(
    family=AttributeFamily.COLOR,
    raw_value="gold",
    canonical_value="Gold",
    strength=ConstraintStrength.PREFERRED,
)


def _product(
    product_id: int,
    *,
    subcategory: str,
    category: str,
    price: str,
    colour: str | None = "White",
) -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Piece {product_id}",
        name_arabic="قطعة",
        price_amount=Decimal(price),
        price_unit=SAR,
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/p/{product_id}",
        commerce=CommerceClassification(category=category, subcategory=subcategory),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color=colour,
        styles=(),
    )


CATALOG = {
    BED: _product(BED, subcategory="bed", category="bedroom", price="1000"),
    NIGHTSTAND: _product(NIGHTSTAND, subcategory="nightstand", category="tables", price="900"),
    MIRROR: _product(MIRROR, subcategory="mirror", category="decor", price="450"),
    GOLD_MIRROR: _product(
        GOLD_MIRROR, subcategory="mirror", category="decor", price="500", colour="Gold"
    ),
    FLOOR_LAMP: _product(FLOOR_LAMP, subcategory="floor-lamp", category="lighting", price="380"),
    DEAR_MIRROR: _product(DEAR_MIRROR, subcategory="mirror", category="decor", price="3000"),
}

STOCK = (
    ("bedroom", "bed"),
    ("tables", "nightstand"),
    ("decor", "mirror"),
    ("lighting", "floor-lamp"),
)


def _capabilities(*pairs: tuple[str, str]) -> RetailerCatalogCapabilities:
    return RetailerCatalogCapabilities(
        capabilities=tuple(
            RetailerCatalogCapability(
                commerce_category=category,
                commerce_subcategory=subcategory,
                active_product_count=3,
            )
            for category, subcategory in pairs
        )
    )


# ── which pieces ────────────────────────────────────────────────────────────


def test_the_suggestions_are_the_rooms_add_ons_it_does_not_hold() -> None:
    pieces = add_on_pieces(BEDROOM, {"bed", "nightstand", "mirror"}, _capabilities(*STOCK))
    # The mirror is already in the room; the bed is never an add-on.
    assert [p.key for p in pieces] == ["floor-lamp"]


def test_a_piece_the_store_does_not_sell_is_never_suggested() -> None:
    pieces = add_on_pieces(BEDROOM, set(), _capabilities(("decor", "mirror")))
    assert [p.key for p in pieces] == ["mirror"]


def test_the_seating_is_never_an_add_on() -> None:
    living = ROOMS.template("living_room")
    assert living is not None and "sofa" not in living.add_ons


# ── which product ───────────────────────────────────────────────────────────


def test_the_best_ranked_product_within_the_budget_is_chosen() -> None:
    ranked = [CATALOG[DEAR_MIRROR], CATALOG[GOLD_MIRROR], CATALOG[MIRROR]]
    assert choose_add_on(ranked, 1, Decimal("600"), Decimal("900")) == CATALOG[GOLD_MIRROR]


def test_past_the_budget_only_within_the_stretch() -> None:
    ranked = [CATALOG[DEAR_MIRROR], CATALOG[GOLD_MIRROR]]
    assert choose_add_on(ranked, 1, Decimal("100"), Decimal("600")) == CATALOG[GOLD_MIRROR]
    assert choose_add_on(ranked, 1, Decimal("100"), Decimal("200")) is None


def test_a_pair_costs_twice() -> None:
    assert choose_add_on([CATALOG[FLOOR_LAMP]], 2, Decimal("700"), Decimal("700")) is None


def test_their_colour_is_the_reason() -> None:
    assert add_on_reasons(CATALOG[GOLD_MIRROR], (GOLD,)) == (UpgradeReason.THEIR_COLOUR,)
    assert add_on_reasons(CATALOG[MIRROR], (GOLD,)) == ()


# ── the chips and the stand-ins ─────────────────────────────────────────────


def test_the_add_on_is_answered_by_two_explicit_chips() -> None:
    assert [c.label for c in UPGRADE_CHOICES] == ["Yes, add it", "No, I'm good"]
    assert isinstance(UPGRADE_CHOICES[0].bundle_action, UpgradeAcceptAction)
    assert isinstance(UPGRADE_CHOICES[1].bundle_action, UpgradeDeclineAction)


@pytest.mark.parametrize("kind", ["upgrade_accept", "upgrade_decline"])
def test_the_action_union_reads_the_answers(kind: str) -> None:
    action = TypeAdapter(BundleActionRequest).validate_python({"kind": kind})
    assert _bundle_action_decision(action).action is AgentAction.ANSWER


# ── the coordinator ─────────────────────────────────────────────────────────


class NeedDiscovery(FakeDesignDiscovery):
    def resolve_need(self, need: Any, request: Any) -> ResolvedSearch:
        return ResolvedSearch(
            request=ProductSearchRequest(
                commerce_category=need.commerce_category,
                commerce_subcategory=need.commerce_subcategory,
            ),
            semantics=ConstraintSemantics(),
        )


def _pool(*products: ProductCandidate) -> CandidatePoolResult:
    return CandidatePoolResult(
        candidates=tuple(
            RankedProductCandidate(product=product, relaxation_depth=0) for product in products
        ),
        eligible_count=len(products),
        was_relaxed=False,
        stop_reason=StopReason.EXACT_SUFFICIENT,
        semantic_used=False,
    )


class Shelf:
    """Products by type, honouring the price ceiling; a forced pool of one."""

    def __init__(self, by_type: dict[str, tuple[ProductCandidate, ...]]) -> None:
        self.by_type = by_type
        self.searched: list[ResolvedSearch] = []

    async def execute_candidate_pool(self, resolved: Any, context: Any) -> CandidatePoolResult:
        self.searched.append(resolved)
        ceiling = resolved.request.price.max_amount if resolved.request.price else None
        found = [
            p
            for p in self.by_type.get(resolved.request.commerce_subcategory, ())
            if ceiling is None or p.price_amount <= ceiling
        ]
        return _pool(*found)

    async def execute_forced_pool(self, product_id: int, context: Any) -> CandidatePoolResult:
        return _pool(CATALOG[product_id])


class CatalogHydration:
    """Reads products back from this test's own catalog."""

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[ProductCandidate, ...]:
        return tuple(CATALOG[p] for p in product_ids if p in CATALOG)


class PricingOptimizer:
    """The room it is handed: each role its forced product, plus every lock."""

    def optimize(self, request: Any) -> RoomBundle:
        lines = [
            BundleLine(
                product=entry.pool.candidates[0].product,
                quantity=entry.need.quantity,
                locked=False,
                acquisition=BundleAcquisition.TO_BUY,
                relaxation_depth=0,
            )
            for entry in request.discovery.needs
            if entry.pool is not None and entry.pool.candidates
        ]
        lines += [
            BundleLine(
                product=lock.product,
                quantity=lock.quantity,
                locked=True,
                acquisition=lock.acquisition,
            )
            for lock in request.locked
        ]
        return _room_bundle(tuple(lines))


def _room_bundle(lines: tuple[BundleLine, ...]) -> RoomBundle:
    return RoomBundle(
        lines=lines,
        status=BundleStatus.COMPLETE,
        new_spend_total=sum(
            (line.product.price_amount * line.quantity for line in lines), Decimal(0)
        ),
        currency=SAR,
    )


def _built(budget: str | None = "3000") -> tuple[AgentStateV1, RoomBundle]:
    needs = (
        RoomDesignNeedState(
            need_id=1,
            commerce_category="bedroom",
            commerce_subcategory="bed",
            priority=DesignPriority.REQUIRED,
            quantity=1,
        ),
        RoomDesignNeedState(
            need_id=2,
            commerce_category="tables",
            commerce_subcategory="nightstand",
            priority=DesignPriority.REQUIRED,
            quantity=1,
        ),
    )
    items = tuple(
        BundleItemState(
            line_id=n,
            product_id=pid,
            quantity=1,
            acquisition=BundleAcquisition.TO_BUY,
            status=BundleItemStatus.SUGGESTED,
            need_id=n,
        )
        for n, pid in ((1, BED), (2, NIGHTSTAND))
    )
    state = AgentStateV1(
        room_project=RoomProjectState(
            room_kind="bedroom",
            budget=PriceConstraint.at_most(Decimal(budget), SAR) if budget else None,
            design_preferences=(GOLD,),
            design_needs=needs,
            next_design_need_id=3,
            bundle_items=items,
            next_bundle_line_id=3,
            bundle_revision=1,
        )
    )
    lines = tuple(
        BundleLine(
            product=CATALOG[pid],
            quantity=1,
            locked=False,
            acquisition=BundleAcquisition.TO_BUY,
            relaxation_depth=0,
        )
        for pid in (BED, NIGHTSTAND)
    )
    return state, _room_bundle(lines)


def _seller(shelf: dict[str, tuple[ProductCandidate, ...]]) -> tuple[Any, Shelf]:
    from app.schemas.agent_decision import CustomerAgentDecision

    catalog = Shelf(shelf)
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        pipeline=catalog,  # type: ignore[arg-type]
        capabilities=FakeCapabilities(pairs=STOCK),
        design_discovery=NeedDiscovery(),
        optimizer=PricingOptimizer(),
        hydration=CatalogHydration(),  # type: ignore[arg-type]
        rooms=ROOMS,
        seating=SEATING,
    )
    coordinator._upgrade_policy = POLICY
    return coordinator, catalog


def _turn(state: AgentStateV1, **action: Any) -> CustomerTurnInput:
    return CustomerTurnInput(message="design it", state=state, context=CONTEXT, **action)


def _holding(state: AgentStateV1, product_id: int, *, revision: int = 1, round_: int = 1) -> Any:
    return state.model_copy(
        update={
            "room_upgrade_offer": RoomUpgradeOfferState(
                product_id=product_id, bundle_revision=revision, round=round_
            )
        }
    )


SHELF = {"mirror": (CATALOG[GOLD_MIRROR], CATALOG[MIRROR]), "floor-lamp": (CATALOG[FLOOR_LAMP],)}


async def test_a_built_room_suggests_the_first_add_on_with_real_figures() -> None:
    coordinator, catalog = _seller(SHELF)
    state, room = _built()

    after, offer = await coordinator._upgrade(state, room, _turn(state))

    assert offer is not None
    assert (offer.piece, offer.round) == ("mirror", 1)
    assert offer.reasons == (UpgradeReason.THEIR_COLOUR,)
    # 1,900 of a 3,000 budget: the gold mirror at 500 fits and ranks first.
    assert (offer.extra_cost, offer.new_total) == (Decimal("500"), Decimal("2400"))
    assert offer.over_budget_by is None
    assert offer.product is not None and offer.product.name_english == f"Piece {GOLD_MIRROR}"
    assert after.room_upgrade_offer == RoomUpgradeOfferState(
        product_id=GOLD_MIRROR, quantity=1, bundle_revision=1, round=1
    )
    # Capped at what the budget can take, with the stretch, and locked.
    (searched, *_) = catalog.searched
    assert searched.request.price is not None
    assert searched.request.price.max_amount == Decimal("1550")
    assert searched.semantics.price_max is ConstraintStrength.LOCKED


async def test_a_piece_the_room_already_holds_is_never_suggested_again() -> None:
    coordinator, _ = _seller(SHELF)
    state, room = _built()
    mirror = BundleLine(
        product=CATALOG[MIRROR],
        quantity=1,
        locked=True,
        acquisition=BundleAcquisition.TO_BUY,
    )
    with_mirror = _room_bundle((*room.lines, mirror))

    _, offer = await coordinator._upgrade(state, with_mirror, _turn(state))

    assert offer is not None and offer.piece == "floor lamp"


async def test_past_the_budget_the_offer_says_by_how_much() -> None:
    coordinator, _ = _seller({"mirror": (CATALOG[GOLD_MIRROR],)})
    state, room = _built(budget="2100")

    _, offer = await coordinator._upgrade(state, room, _turn(state))

    # 2,400 against a 2,100 budget: inside the 15% stretch (2,415), and said.
    assert offer is not None
    assert (offer.over_budget_by, offer.budget) == (Decimal("300"), Decimal("2100"))


async def test_a_room_still_missing_a_piece_gets_no_add_on() -> None:
    coordinator, catalog = _seller(SHELF)
    state, room = _built()
    partial = room.model_copy(update={"status": BundleStatus.PARTIAL})

    _, offer = await coordinator._upgrade(state, partial, _turn(state))

    assert offer is None and catalog.searched == []


async def test_no_third_add_on() -> None:
    coordinator, catalog = _seller(SHELF)
    state, room = _built()

    _, offer = await coordinator._upgrade(state, room, _turn(state), round_=3)

    assert offer is None and catalog.searched == []


async def test_an_unreachable_catalog_is_no_offer_and_no_failure() -> None:
    from app.core.exceptions import IntegrationUnavailableError

    coordinator, catalog = _seller({})

    async def down(resolved: Any, context: Any) -> CandidatePoolResult:
        raise IntegrationUnavailableError(detail="down")

    catalog.execute_candidate_pool = down  # type: ignore[method-assign]
    state, room = _built()

    after, offer = await coordinator._upgrade(state, room, _turn(state))

    assert offer is None and after is state


async def test_yes_adds_it_to_the_room_and_suggests_one_more() -> None:
    coordinator, _ = _seller(SHELF)
    state, _ = _built()

    result = await coordinator.run(
        _turn(_holding(state, GOLD_MIRROR), bundle_action=UpgradeAcceptAction())
    )

    room = result.state.room_project
    assert room is not None
    added = next(i for i in room.bundle_items if i.product_id == GOLD_MIRROR)
    assert added.status is BundleItemStatus.LOCKED
    assert result.add_on_added == "mirror"
    # The second, and last: what the room still lacks.
    assert result.room_upgrade is not None
    assert (result.room_upgrade.piece, result.room_upgrade.round) == ("floor lamp", 2)
    assert result.state.room_upgrade_offer is not None
    assert result.state.room_upgrade_offer.round == 2


async def test_yes_to_the_second_ends_the_offers() -> None:
    coordinator, _ = _seller(SHELF)
    state, _ = _built()

    result = await coordinator.run(
        _turn(_holding(state, FLOOR_LAMP, round_=2), bundle_action=UpgradeAcceptAction())
    )

    assert result.add_on_added == "floor lamp"
    assert result.room_upgrade is None
    assert result.state.room_upgrade_offer is None


async def test_yes_past_the_budget_raises_it_to_fit() -> None:
    coordinator, _ = _seller({})
    state, _ = _built(budget="2100")

    result = await coordinator.run(
        _turn(_holding(state, GOLD_MIRROR, round_=2), bundle_action=UpgradeAcceptAction())
    )

    room = result.state.room_project
    assert room is not None and room.budget is not None
    assert room.budget.max_amount == Decimal("2400")


async def test_no_to_the_first_keeps_the_room_and_suggests_a_different_piece() -> None:
    coordinator, _ = _seller(SHELF)
    state, _ = _built()

    result = await coordinator.run(
        _turn(_holding(state, GOLD_MIRROR), bundle_action=UpgradeDeclineAction())
    )

    assert result.upgrade_declined
    room = result.state.room_project
    assert room is not None and GOLD_MIRROR not in [i.product_id for i in room.bundle_items]
    # Never the mirror again: the next piece on the list.
    assert result.room_upgrade is not None
    assert (result.room_upgrade.piece, result.room_upgrade.round) == ("floor lamp", 2)
    assert result.state.room_upgrade_offer is not None
    assert result.state.room_upgrade_offer.round == 2


async def test_no_to_the_second_ends_it_and_keeps_the_room() -> None:
    coordinator, _ = _seller(SHELF)
    state, _ = _built()

    result = await coordinator.run(
        _turn(_holding(state, FLOOR_LAMP, round_=2), bundle_action=UpgradeDeclineAction())
    )

    assert result.upgrade_declined
    assert result.room_upgrade is None
    assert result.state.room_upgrade_offer is None
    assert result.state.room_project == state.room_project


async def test_a_yes_to_an_offer_the_room_has_moved_past_changes_nothing() -> None:
    coordinator, _ = _seller(SHELF)
    state, _ = _built()

    result = await coordinator.run(
        _turn(_holding(state, GOLD_MIRROR, revision=0), bundle_action=UpgradeAcceptAction())
    )

    assert result.state.room_upgrade_offer is None
    assert result.state.room_project == state.room_project
    assert result.bundle_outcome is None
