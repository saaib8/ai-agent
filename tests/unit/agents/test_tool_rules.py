"""The rules the tools keep whatever an agent decides: lists, offers, limits."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any, cast

import pytest
from app.agents.facts import cart_facts, product_facts
from app.agents.loop import TurnBudget, json_facts
from app.agents.operations.rooms import RoomEngine, RoomOutcome
from app.agents.payloads import RenderShown
from app.agents.reply import unsupported_figure
from app.agents.services import AgentServices
from app.agents.tools.common import RespondArgs
from app.agents.tools.shopping import _executable
from app.agents.trace import TurnTrace
from app.agents.work import ToolRecord, ToolStatus, TurnWork
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import (
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    OfferedCombination,
    OfferedCombinationLine,
    ProductInteractionState,
    RoomProjectState,
    SeatingOfferState,
)
from app.schemas.card_comparison import CardRef
from app.schemas.conversation import ConversationContext
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.grounding import SearchExecutionGrounding, SearchOutcome
from app.schemas.query import ClarificationReason, ClarificationRequired
from app.schemas.relaxation import StopReason
from app.schemas.resolution import (
    ComparisonFailureReason,
    ComparisonUnavailable,
    ProductSearchExecutionResult,
    ResolvedProductReference,
)
from app.schemas.seating_solution import SeatingShape
from app.schemas.support import SupportContacts
from app.schemas.visualization import RenderSource, RoomRenderPresentation
from app.services.cross_sell import CompanionSearchBuilder
from app.services.grounding_builder import to_grounded_product
from app.services.support import SupportService
from langchain_core.messages import HumanMessage

from tests.unit.agents.fake_chat_model import ScriptedChatModel, call, step
from tests.unit.agents.fakes import (
    ATTRIBUTES,
    OFF_SCREEN,
    TAXONOMY,
    FakeComparison,
    FakeHydration,
    FakePipeline,
    FakeReferences,
    _product,
    _state,
    agent_services,
    real_loop,
    tool_context,
)

LOOP = real_loop()


def _tool(name: str) -> Any:
    tool = LOOP._registry.get(name)
    assert tool is not None and tool.handler is not None
    return tool


async def _call(name: str, args: dict[str, Any], state: AgentStateV1, **context: Any) -> Any:
    tool = _tool(name)
    work = TurnWork.starting(state)
    services = context.pop("services", None) or agent_services()
    outcome = await tool.handler(
        tool.args.model_validate(args), tool_context(services, **context), work
    )
    return outcome


# ── the number check ────────────────────────────────────────────────────────


def _work(facts: dict[str, Any], tool: str = "x") -> TurnWork:
    work = TurnWork.starting(AgentStateV1())
    work.record(
        ToolRecord(
            agent=AgentPersona.SARA, tool=tool, status=ToolStatus.OK, facts=json_facts(facts)
        )
    )
    return work


@pytest.mark.parametrize(
    ("facts", "said"),
    [
        ({"product": {"name": "Oslo Floor Lamp 2.25m"}}, "The 2.25m Oslo lamp suits it."),
        ({"passages": [{"text": "Delivery takes 4 to 7 working days."}]}, "It takes 4 to 7 days."),
    ],
)
def test_figures_inside_words_a_tool_returned_may_be_said(facts: dict[str, Any], said: str) -> None:
    reply = RespondArgs(message=said)

    assert unsupported_figure(reply, _work(facts), "hi", ConversationContext(), [{}]) is None


def test_a_cart_total_summed_in_code_may_be_said() -> None:
    a, b = _product(1, price="3000"), _product(2, price="2300")
    state = AgentStateV1(product_interaction=ProductInteractionState(selected_product_ids=(1, 2)))
    facts = cart_facts((a, b), state)

    assert facts["totals"] == [{"currency": "SAR", "amount": "5300"}]
    reply = RespondArgs(message="Your cart comes to 5,300 SAR.")
    assert (
        unsupported_figure(reply, _work({"cart": facts}), "total?", ConversationContext(), [{}])
        is None
    )


def test_sizes_reach_the_agents_as_people_write_them() -> None:
    product = _product(1).model_copy(
        update={
            "dimensions": NormalisedDimensions(
                length_cm=Decimal("220.00"),
                width_cm=Decimal("100"),
                status=DimensionStatus.NORMALISED,
            )
        }
    )

    assert product_facts(product, 1)["size_cm"] == {"longer_side": "220", "shorter_side": "100"}


# ── a tapped card is read on its own list ───────────────────────────────────


class ListReferences:
    def __init__(self) -> None:
        self.on_list: list[tuple[int, int | None]] = []

    async def resolve(self, selector: Any, state: Any, context: Any) -> Any:
        return ResolvedProductReference(product_id=99)

    async def resolve_on_list(
        self, ordinal: int, revision: int | None, state: Any, context: Any
    ) -> Any:
        self.on_list.append((ordinal, revision))
        return ResolvedProductReference(product_id=7)


async def test_a_tapped_card_is_read_on_the_list_it_was_tapped_on() -> None:
    references = ListReferences()
    services = agent_services(
        references=cast(Any, references), hydration=FakeHydration(available=(7,))
    )

    outcome = await _call(
        "add_to_cart",
        {"items": [{"which": {"kind": "presented_ordinal", "position": 3}}]},
        AgentStateV1(),
        services=services,
        tap_cards=(CardRef(list_revision=2, ordinal=3),),
    )

    assert references.on_list == [(3, 2)]
    assert outcome.state.product_interaction.selected_product_ids == (7,)


# ── seating ─────────────────────────────────────────────────────────────────


# ── going over budget needs their yes ───────────────────────────────────────


def _room_state(revision: int = 3) -> AgentStateV1:
    return _state(presented=(30, 31), selected=()).model_copy(
        update={"room_project": RoomProjectState(room_type="living room", bundle_revision=revision)}
    )


class Rooms:
    def __init__(self) -> None:
        self.swaps: list[tuple[int, int, bool]] = []

    async def swap(
        self, state: Any, context: Any, piece: int, option: int, *, allow_over_budget: bool
    ) -> RoomOutcome:
        self.swaps.append((piece, option, allow_over_budget))
        if not allow_over_budget:
            return RoomOutcome(
                state=state,
                bundle=None,
                code="over_budget",
                committed=False,
                facts={"overage": "400"},
            )
        return RoomOutcome(state=state, bundle=None, code="ok", committed=True)


def _with_rooms(rooms: Rooms) -> AgentServices:
    return dataclasses.replace(agent_services(), rooms=cast(RoomEngine, rooms))


async def test_an_over_budget_swap_changes_nothing_and_holds_the_offer() -> None:
    rooms = Rooms()

    outcome = await _call(
        "swap_room_piece", {"piece": 2, "option": 2}, _room_state(), services=_with_rooms(rooms)
    )

    assert rooms.swaps == [(2, 2, False)]
    offer = outcome.state.swap_budget_offer
    assert offer.bundle_ordinal == 2 and offer.chosen_product_id == 31
    assert outcome.state.room_project == _room_state().room_project


async def test_their_yes_replays_the_held_offer_and_nothing_else() -> None:
    rooms = Rooms()
    held = await _call(
        "swap_room_piece", {"piece": 2, "option": 2}, _room_state(), services=_with_rooms(rooms)
    )

    accepted = await _call("accept_over_budget", {}, held.state, services=_with_rooms(rooms))

    assert rooms.swaps[-1] == (2, 2, True)
    assert accepted.state.swap_budget_offer is None


async def test_a_yes_with_no_offer_held_does_nothing() -> None:
    rooms = Rooms()

    outcome = await _call("accept_over_budget", {}, _room_state(), services=_with_rooms(rooms))

    assert outcome.status is ToolStatus.REFUSED and rooms.swaps == []


async def test_a_yes_to_an_offer_for_a_room_that_moved_on_does_nothing() -> None:
    rooms = Rooms()
    held = await _call(
        "swap_room_piece", {"piece": 2, "option": 2}, _room_state(), services=_with_rooms(rooms)
    )
    moved = held.state.model_copy(update={"room_project": _room_state(revision=4).room_project})

    outcome = await _call("accept_over_budget", {}, moved, services=_with_rooms(rooms))

    assert outcome.code == "no_offer_to_accept"


# ── a room into the cart keeps the cart's limit ─────────────────────────────


async def test_a_room_goes_into_the_cart_whole_even_past_the_limit() -> None:
    lines = tuple(
        BundleItemState(
            line_id=n,
            product_id=100 + n,
            quantity=1,
            acquisition=BundleAcquisition.TO_BUY,
            status=BundleItemStatus.SUGGESTED,
        )
        for n in range(1, 5)
    )
    state = AgentStateV1(
        room_project=RoomProjectState(
            room_type="living room", bundle_items=lines, next_bundle_line_id=5
        )
    )
    services = agent_services(max_picks=2, hydration=FakeHydration(available=(101, 102, 103, 104)))

    outcome = await _call("add_room_to_cart", {}, state, services=services)

    # Half a room is not a choice they made (CLAUDE.md 36).
    assert len(outcome.state.product_interaction.selected_product_ids) == 4
    assert outcome.code is None


# ── the turn's last call is the reply's ─────────────────────────────────────


async def test_the_last_model_call_offers_respond_alone() -> None:
    model = ScriptedChatModel(script=[step(call("respond", "r", message="Here they are."))])
    budget = TurnBudget(max_model_calls=2, model_calls=1)

    await LOOP.run(
        AgentPersona.SARA,
        model,
        [HumanMessage("hi")],
        TurnWork.starting(AgentStateV1()),
        tool_context(),
        budget,
        TurnTrace(store_id=50, path="typed"),
    )

    assert model.offered[-1] == ["respond"]


# ── the second review's fixes ───────────────────────────────────────────────


async def test_an_over_budget_proposal_is_never_drawn_as_the_room() -> None:
    rooms = Rooms()

    outcome = await _call(
        "swap_room_piece", {"piece": 2, "option": 2}, _room_state(), services=_with_rooms(rooms)
    )

    assert outcome.payload is None


def _offer(*combinations: OfferedCombination) -> AgentStateV1:
    return AgentStateV1(
        seating_offer=SeatingOfferState(
            target_seats=8, offered_shapes=(SeatingShape.SEPARATE_SOFAS,), shown=combinations
        )
    )


def _combination(product_id: int) -> OfferedCombination:
    return OfferedCombination(
        shape=SeatingShape.SEPARATE_SOFAS,
        lines=(OfferedCombinationLine(product_id=product_id, quantity=2),),
    )


class TwoLists:
    async def resolve(self, selector: Any, state: Any, context: Any) -> Any:
        raise AssertionError("checked cards are read on their own lists")

    async def resolve_on_list(
        self, ordinal: int, revision: int | None, state: Any, context: Any
    ) -> Any:
        return ResolvedProductReference(product_id=100 + (revision or 0))


async def test_two_checked_cards_at_one_position_on_two_lists_are_two_products() -> None:
    comparison = FakeComparison(
        ComparisonUnavailable(
            reason=ComparisonFailureReason.TOO_FEW_PRODUCTS, requested_count=2, allowed_maximum=10
        )
    )
    services = dataclasses.replace(
        agent_services(references=cast(Any, TwoLists())), comparison=cast(Any, comparison)
    )
    which = {"kind": "presented_ordinal", "position": 2}

    await _call(
        "compare_products",
        {"products": [which, which]},
        AgentStateV1(),
        services=services,
        tap_cards=(CardRef(list_revision=1, ordinal=2), CardRef(list_revision=3, ordinal=2)),
    )

    assert comparison.calls == [[101, 103]]


def test_a_figure_in_the_words_a_tap_stands_for_may_be_said() -> None:
    reply = RespondArgs(message="Done - combination 2 is in your cart.")

    assert (
        unsupported_figure(
            reply,
            _work({}),
            "Choose",
            ConversationContext(),
            [{}],
            also_said=("I'll take seating combination 2.",),
        )
        is None
    )


def test_a_search_that_cannot_run_says_what_would_settle_it() -> None:
    resolved, facts = _executable(
        ClarificationRequired(reason=ClarificationReason.MULTIPLE_PRODUCT_TYPES)
    )

    assert resolved is None
    assert facts["missing"] == "multiple_product_types"
    assert "search each kind" in facts["what_would_settle_it"]


async def test_what_goes_with_lists_kinds_until_one_is_chosen() -> None:
    from app.taxonomy.complements import load_complements

    from tests.unit.agents.fakes import TAXONOMY, FakeCapabilities

    pipeline = FakePipeline(ids=(10, 11))
    services = agent_services(
        pipeline=pipeline,
        hydration=FakeHydration(available=(OFF_SCREEN, 10, 11)),
        capabilities=FakeCapabilities(pairs=(("seating", "sofa"), ("decor", "carpet"))),
        companion_search=CompanionSearchBuilder(ATTRIBUTES),
        complements=load_complements(taxonomy=TAXONOMY),
    )
    which = {"which": {"kind": "picked_ordinal", "position": 1}}

    listed = await _call("what_goes_with", which, _state(), services=services)
    shown = await _call(
        "what_goes_with", {**which, "show_kind": "carpet"}, _state(), services=services
    )
    same = await _call(
        "what_goes_with", {**which, "show_kind": "sofa"}, _state(), services=services
    )
    refused = await _call(
        "what_goes_with", {**which, "show_kind": "chandelier"}, _state(), services=services
    )

    # Without a kind: a grid of one product per complementary kind (just carpet
    # here, the only reviewed pairing in stock) with the kinds as chips; the
    # kind drill-down still gives that kind's full page.
    assert listed.payload is not None, "the no-kind call now shows a one-per-kind grid"
    assert [k["kind"] for k in listed.facts["kinds"]] == ["carpet"]
    assert [s["kind"] for s in listed.facts["suggestions"]] == ["carpet"]
    assert "store_shelf" in listed.facts["other_kinds"]
    assert shown.payload is not None and shown.facts["shown_kind"] == "carpet"
    assert same.code == "same_kind_as_the_piece"
    assert same.facts["use_instead"] == "more_like_this"
    assert refused.code == "kind_not_stocked"


class _PerKindPipeline:
    """A distinct product per complementary subcategory, so the cross-sell grid
    gets one card of each; a subcategory mapped to None is out of stock."""

    def __init__(self, by_subcat: dict[str, int | None]) -> None:
        self.by_subcat = by_subcat
        self.calls: list[Any] = []

    async def execute(
        self,
        resolved: Any,
        context: Any,
        *,
        dropped_constraints: Any = (),
        earlier_sizes_applied: bool = False,
        presentation_limit: int | None = None,
        closest_sizes: bool = False,
    ) -> ProductSearchExecutionResult:
        self.calls.append(resolved)
        product_id = self.by_subcat.get(resolved.request.commerce_subcategory)
        ids = (product_id,) if product_id is not None else ()
        return ProductSearchExecutionResult(
            presented_product_ids=ids,
            grounding=SearchExecutionGrounding(
                outcome=SearchOutcome.RESULTS if ids else SearchOutcome.ZERO_RESULTS,
                products=tuple(
                    to_grounded_product(
                        _product(pid), grounding_ref=n, presented_ordinal=n, relaxation_depth=0
                    )
                    for n, pid in enumerate(ids, start=1)
                ),
                eligible_count=len(ids),
                ranked_count=len(ids),
                selected_count=len(ids),
                presented_count=len(ids),
                exact_candidate_count=len(ids),
                stop_reason=StopReason.EXACT_SUFFICIENT,
            ),
        )


async def test_cross_sell_shows_one_product_per_complementary_kind() -> None:
    """Without a kind, the grid samples one product from each stocked
    complementary kind - a nightstand, a lamp, a rug - so the cards match the
    chips' variety, with out-of-stock kinds skipped."""
    from app.taxonomy.complements import load_complements

    from tests.unit.agents.fakes import FakeCapabilities

    # A sofa's reviewed complements: a distinct product for a table and a rug,
    # nothing for a floor lamp (out of stock), so the grid skips it.
    pipeline = _PerKindPipeline({"center-table": 20, "carpet": 21, "floor-lamp": None})
    services = agent_services(
        pipeline=pipeline,
        hydration=FakeHydration(available=(OFF_SCREEN, 20, 21)),
        capabilities=FakeCapabilities(
            pairs=(
                ("seating", "sofa"),
                ("tables", "center-table"),
                ("decor", "carpet"),
                ("lighting", "floor-lamp"),
            )
        ),
        companion_search=CompanionSearchBuilder(ATTRIBUTES),
        complements=load_complements(taxonomy=TAXONOMY),
    )
    which = {"which": {"kind": "picked_ordinal", "position": 1}}

    listed = await _call("what_goes_with", which, _state(), services=services)

    assert listed.payload is not None, "the no-kind call shows a one-per-kind grid"
    grounding = listed.payload.grounding
    # One card per in-stock complementary kind, the out-of-stock one left out.
    assert [s["kind"] for s in listed.facts["suggestions"]] == ["center-table", "carpet"]
    assert len(grounding.products) == 2
    assert [p.grounding_ref for p in grounding.products] == [1, 2]
    # A curated set, never a search with a story: nothing to claim as widened,
    # set aside, or truncated, so no reply can over-promise.
    assert grounding.was_relaxed is False and grounding.relaxations == ()
    assert grounding.set_aside == () and grounding.truncated_for_presentation is False
    assert all(product.relaxation_depth == 0 for product in grounding.products)
    # The grid is the presented list, so each card is selectable / visualisable.
    assert listed.state is not None
    assert listed.state.product_interaction.presented_product_ids == (20, 21)


def _plan_services(pipeline: Any) -> Any:
    from app.taxonomy.complements import load_complements

    from tests.unit.agents.fakes import FakeCapabilities

    return agent_services(
        pipeline=pipeline,
        hydration=FakeHydration(available=(OFF_SCREEN, 20, 21, 22, 23, 24)),
        capabilities=FakeCapabilities(
            pairs=(
                ("seating", "sofa"),
                ("decor", "art-canvas"),
                ("tables", "center-table"),
                ("tables", "service-table"),
                ("decor", "carpet"),
                ("lighting", "floor-lamp"),
            )
        ),
        companion_search=CompanionSearchBuilder(ATTRIBUTES),
        complements=load_complements(taxonomy=TAXONOMY),
    )


def _planned(*kinds: Any, anchor: int = OFF_SCREEN) -> AgentStateV1:
    from app.schemas.agent_state import CrossSellPlan

    return _state().model_copy(
        update={"cross_sell_plan": CrossSellPlan(anchor_product_id=anchor, kinds=kinds)}
    )


_GRID_STOCK = {
    "art-canvas": 24,
    "center-table": 20,
    "service-table": 22,
    "carpet": 21,
    "floor-lamp": 23,
}


async def test_noras_unreviewed_stocked_kind_is_shown_and_reviewed_kinds_fill_the_grid() -> None:
    from app.schemas.agent_state import CrossSellKindPlan

    pipeline = _PerKindPipeline(_GRID_STOCK)
    state = _planned(CrossSellKindPlan(kind="art-canvas"), CrossSellKindPlan(kind="center-table"))
    which = {"which": {"kind": "picked_ordinal", "position": 1}}

    listed = await _call("what_goes_with", which, state, services=_plan_services(pipeline))

    assert listed.payload is not None
    assert [item["kind"] for item in listed.facts["suggestions"]] == [
        "art-canvas",
        "center-table",
        "service-table",
        "carpet",
    ]
    chips = [k["kind"] for k in listed.facts["kinds"] if k["kind"] != "sofa"]
    assert chips == ["art-canvas", "center-table", "service-table", "carpet"], (
        "the chips are the plan's kinds as the grid shows them - wall art included"
    )
    labels = {k["kind"]: k["label"] for k in listed.facts["kinds"]}
    assert labels["art-canvas"] == "wall art", "a planned kind keeps its reviewed words"


async def test_each_grid_card_leans_to_noras_plan_for_its_kind() -> None:
    from app.schemas.agent_state import CrossSellKindPlan

    pipeline = _PerKindPipeline(_GRID_STOCK)
    state = _planned(
        CrossSellKindPlan(kind="carpet", colours=("Sand",), styles=("Minimalist",)),
        CrossSellKindPlan(kind="center-table", colours=("Walnut",), avoid_colours=("Black",)),
    )
    which = {"which": {"kind": "picked_ordinal", "position": 1}}

    listed = await _call("what_goes_with", which, state, services=_plan_services(pipeline))

    leans = {c.request.commerce_subcategory: c for c in pipeline.calls}
    rug, table = leans["carpet"], leans["center-table"]
    assert (rug.lean.colours, rug.lean.styles) == (("Sand",), ("Minimalist",))
    assert rug.semantic_preferences == (), "her look replaces the pick's own styles"
    assert (table.lean.colours, table.lean.avoid_colours) == (("Walnut",), ("Black",))
    unplanned = leans["service-table"]
    assert unplanned.lean is None or unplanned.lean.colours == (), (
        "a kind she left alone leans as before"
    )
    cards = {item["kind"]: item for item in listed.facts["suggestions"]}
    assert cards["carpet"]["leaned_to"] == {"colours": ["Sand"], "styles": ["Minimalist"]}
    assert cards["carpet"]["colour"] == "Beige", "each card says its own colour beside the lean"


async def test_a_chip_page_leans_to_the_plan_for_that_kind() -> None:
    from app.schemas.agent_state import CrossSellKindPlan

    pipeline = _PerKindPipeline(_GRID_STOCK)
    state = _planned(CrossSellKindPlan(kind="carpet", colours=("Sand",)))
    args = {"which": {"kind": "picked_ordinal", "position": 1}, "show_kind": "carpet"}

    shown = await _call("what_goes_with", args, state, services=_plan_services(pipeline))

    assert pipeline.calls[-1].lean.colours == ("Sand",)
    assert shown.facts["leaned_to"] == {"colours": ["Sand"]}


async def test_a_plan_for_another_pick_is_not_used() -> None:
    from app.schemas.agent_state import CrossSellKindPlan

    pipeline = _PerKindPipeline(_GRID_STOCK)
    state = _planned(CrossSellKindPlan(kind="carpet", colours=("Sand",)), anchor=OFF_SCREEN + 1)
    which = {"which": {"kind": "picked_ordinal", "position": 1}}

    listed = await _call("what_goes_with", which, state, services=_plan_services(pipeline))

    assert "design_plan_applied" not in listed.facts
    assert all(c.lean is None or c.lean.colours == () for c in pipeline.calls)


def test_a_planned_size_is_a_share_of_the_picks_longer_side() -> None:
    from app.agents.tools.sales import _leaning
    from app.schemas.agent_state import CrossSellKindPlan
    from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
    from app.services.cross_sell import CompanionSearchBuilder as Builder
    from app.taxonomy.complements import Companion

    sofa = _product(OFF_SCREEN).model_copy(
        update={
            "dimensions": NormalisedDimensions(
                status=DimensionStatus.NORMALISED,
                length_cm=Decimal("90"),
                width_cm=Decimal("220"),
            )
        }
    )
    rug = Companion(commerce_category="decor", commerce_subcategory="carpet", label="rugs")
    base = Builder(ATTRIBUTES).build(sofa, rug)

    sized = _leaning(base, sofa, CrossSellKindPlan(kind="carpet", size_ratio=1.2))
    unsized = _leaning(base, _product(OFF_SCREEN), CrossSellKindPlan(kind="carpet", size_ratio=1.2))

    assert sized.lean.size_target_cm == Decimal("264")
    assert sized.semantic_preferences == base.semantic_preferences, (
        "no styles of hers - the pick's own styles still lead"
    )
    assert unsized is base, "no listed size on the pick - nothing to work a size from"
    assert _leaning(base, sofa, None) is base


async def test_cross_sell_falls_back_to_chips_when_nothing_is_in_stock() -> None:
    """When no complementary kind yields a product, there is no grid - just the
    chips, exactly as before - so the offer never shows an empty page."""
    from app.taxonomy.complements import load_complements

    from tests.unit.agents.fakes import FakeCapabilities

    pipeline = _PerKindPipeline({})  # every kind out of stock
    services = agent_services(
        pipeline=pipeline,
        hydration=FakeHydration(available=(OFF_SCREEN,)),
        capabilities=FakeCapabilities(pairs=(("seating", "sofa"), ("decor", "carpet"))),
        companion_search=CompanionSearchBuilder(ATTRIBUTES),
        complements=load_complements(taxonomy=TAXONOMY),
    )
    which = {"which": {"kind": "picked_ordinal", "position": 1}}

    listed = await _call("what_goes_with", which, _state(), services=services)

    assert listed.payload is None, "no product anywhere -> chips only, no empty grid"
    assert "suggestions" not in listed.facts
    assert [k["kind"] for k in listed.facts["kinds"]] == ["carpet"]


class _KindsHydration:
    """Products of the kinds named, so a cart can hold a sofa and a rug."""

    def __init__(self, kinds: dict[int, str]) -> None:
        self.kinds = kinds

    async def hydrate_ids(self, product_ids: Any, context: Any) -> tuple[Any, ...]:
        return tuple(_product(p, subcategory=self.kinds[p]) for p in product_ids if p in self.kinds)


def _cross_sell_with_a_rug_in_the_cart() -> tuple[Any, _PerKindPipeline]:
    from app.taxonomy.complements import load_complements

    from tests.unit.agents.fakes import FakeCapabilities

    pipeline = _PerKindPipeline({"center-table": 20, "carpet": 21})
    services = agent_services(
        pipeline=pipeline,
        hydration=_KindsHydration({OFF_SCREEN: "sofa", RUG_IN_CART: "carpet", 20: "center-table"}),
        capabilities=FakeCapabilities(
            pairs=(("seating", "sofa"), ("tables", "center-table"), ("decor", "carpet"))
        ),
        companion_search=CompanionSearchBuilder(ATTRIBUTES),
        complements=load_complements(taxonomy=TAXONOMY),
    )
    return services, pipeline


RUG_IN_CART = 40


async def test_cross_sell_shows_no_picture_of_a_kind_already_in_the_cart() -> None:
    """A sofa with a rug already in the cart: the grid shows a coffee table,
    never another rug - the chips still offer rugs, for a second one."""
    services, _ = _cross_sell_with_a_rug_in_the_cart()
    state = _state(selected=(OFF_SCREEN, RUG_IN_CART))
    which = {"which": {"kind": "picked_ordinal", "position": 1}}

    listed = await _call("what_goes_with", which, state, services=services)

    assert [s["kind"] for s in listed.facts["suggestions"]] == ["center-table"]
    assert "carpet" in [k["kind"] for k in listed.facts["kinds"]]


async def test_cross_sell_never_shows_back_what_is_in_the_cart() -> None:
    """Every search behind the grid, and a kind's full page, leaves the cart's
    products out - the nightstand they just added never comes back as a card."""
    services, pipeline = _cross_sell_with_a_rug_in_the_cart()
    state = _state(selected=(OFF_SCREEN, RUG_IN_CART))
    which = {"which": {"kind": "picked_ordinal", "position": 1}}

    await _call("what_goes_with", which, state, services=services)
    await _call("what_goes_with", {**which, "show_kind": "carpet"}, state, services=services)

    assert pipeline.calls, "the grid and the kind's page searched"
    for resolved in pipeline.calls:
        assert {OFF_SCREEN, RUG_IN_CART} <= set(resolved.request.exclude_product_ids)


async def test_what_goes_with_offers_another_sofa_beside_the_pairings() -> None:
    """A piece that seats several lists another of its kind with the pairings -
    a second sofa for two facing or an L - shown with more_like_this."""
    from app.taxonomy.complements import load_complements
    from app.taxonomy.seating import load_seating_semantics

    from tests.unit.agents.fakes import TAXONOMY, FakeCapabilities

    services = agent_services(
        hydration=FakeHydration(available=(OFF_SCREEN, 10, 11)),
        capabilities=FakeCapabilities(pairs=(("seating", "sofa"), ("decor", "carpet"))),
        companion_search=CompanionSearchBuilder(ATTRIBUTES),
        complements=load_complements(taxonomy=TAXONOMY),
        seating=load_seating_semantics(taxonomy=TAXONOMY),
    )
    which = {"which": {"kind": "picked_ordinal", "position": 1}}

    listed = await _call("what_goes_with", which, _state(), services=services)

    kinds = listed.facts["kinds"]
    assert [k["kind"] for k in kinds] == ["sofa", "carpet"]
    assert kinds[0]["another_of_its_kind"] and kinds[0]["show_with"] == "more_like_this"


async def test_what_goes_with_offers_stocked_kinds_when_none_is_reviewed() -> None:
    """A piece with no reviewed pairing still gets kinds to choose from - the
    store's stocked kinds, never its own - so the offer never ends empty."""
    from tests.unit.agents.fakes import FakeCapabilities

    services = agent_services(
        hydration=FakeHydration(available=(OFF_SCREEN, 10, 11)),
        capabilities=FakeCapabilities(pairs=(("seating", "sofa"), ("decor", "carpet"))),
        companion_search=CompanionSearchBuilder(ATTRIBUTES),
        complements=None,
    )
    which = {"which": {"kind": "picked_ordinal", "position": 1}}

    listed = await _call("what_goes_with", which, _state(), services=services)

    assert listed.code is None
    assert [k["kind"] for k in listed.facts["kinds"]] == ["carpet"]
    assert "none_reviewed" in listed.facts


class _Contacts:
    async def for_store(self, context: Any) -> SupportContacts:
        return SupportContacts(email="help@example.test")


async def test_passing_a_request_on_is_offered_only_when_they_asked_for_a_person() -> None:
    services = agent_services(support=SupportService(cast(Any, _Contacts()), handover=True))

    browsing = await _call("contact_store", {}, AgentStateV1(), services=services)
    asked = await _call("contact_store", {"hand_over": True}, AgentStateV1(), services=services)
    tapped = await _call(
        "contact_store", {}, AgentStateV1(), services=services, tapped="contact_store"
    )

    assert browsing.payload.support.handover is False
    assert asked.payload.support.handover is True
    assert tapped.payload.support.handover is True


def test_a_figure_the_shop_wrote_in_words_may_be_said_in_digits() -> None:
    facts = {"passages": [{"text": "Returns are accepted within fourteen days, or 10k."}]}
    work = _work(facts, tool="store_policy")
    reply = RespondArgs(message="You can return it within 14 days.")

    assert unsupported_figure(reply, work, "returns?", ConversationContext(), [{}]) is None
    made_up = RespondArgs(message="You can return it within 30 days.")
    assert unsupported_figure(made_up, work, "returns?", ConversationContext(), [{}])


async def test_ask_about_gives_what_the_store_carries_and_shows_nothing() -> None:
    # A bare need is met with what the store carries of the kind, so the agent
    # asks about that in its own words - nothing searched, no products shown,
    # and no fixed questions to recite (CLAUDE.md 10.2).
    tool = _tool("ask_about")
    assert tool.owners == frozenset({AgentPersona.SARA})

    outcome = await tool.handler(
        tool.args.model_validate(
            {"search": {"commerce_category": "seating", "commerce_subcategory": "recliner"}}
        ),
        tool_context(agent_services()),
        TurnWork.starting(AgentStateV1()),
    )

    # Remembered, so the answers offered about it later are checked against
    # what the store carries of it.
    assert outcome.state is not None and outcome.state.kind_asked is not None
    assert outcome.payload is None


def test_a_figure_in_words_in_a_product_name_is_not_a_figure() -> None:
    facts = {"product": {"name": "Two Seater Sofa"}}
    reply = RespondArgs(message="It seats 2.")

    assert unsupported_figure(reply, _work(facts), "seats?", ConversationContext(), [{}])


# ── visualize_product (see it in a room) ────────────────────────────────────


class _FakeVisualizer:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def render_single(self, product_id: int, context: Any) -> RoomRenderPresentation:
        self.calls.append(product_id)
        return RoomRenderPresentation(
            image_url="data:image/jpeg;base64,eA==",
            width=1536,
            height=1024,
            view=None,
            view_label="In a room",
            items=(),
            source=RenderSource.PRODUCT,
        )


async def test_visualize_product_stages_the_card_and_focuses_it() -> None:
    """The tapped/typed card renders to a RenderShown, and the piece is put in
    focus so the "Add to cart" chip adds that one."""
    viz = _FakeVisualizer()
    services = agent_services(
        references=FakeReferences(default=ResolvedProductReference(product_id=10)),
        hydration=FakeHydration(available=(10,)),
        visualizer=cast(Any, viz),
    )
    outcome = await _call(
        "visualize_product",
        {"which": {"kind": "presented_ordinal", "position": 1}},
        _state(presented=(10, 11)),
        services=services,
    )

    assert outcome.status is ToolStatus.OK
    assert isinstance(outcome.payload, RenderShown)
    assert outcome.payload.render.source is RenderSource.PRODUCT
    assert viz.calls == [10]
    assert outcome.state is not None
    assert outcome.state.product_interaction.focused_product_id == 10
    assert "staged" in (outcome.facts or {})


async def test_visualize_product_refused_when_visualisation_is_unconfigured() -> None:
    """A deployment without visualisation configured has no visualizer; the
    tool refuses rather than crashing, and nothing is rendered."""
    outcome = await _call(
        "visualize_product",
        {"which": {"kind": "presented_ordinal", "position": 1}},
        _state(presented=(10, 11)),
        services=agent_services(visualizer=None),
    )

    assert outcome.status is ToolStatus.REFUSED
    assert outcome.code == "visualisation_unavailable"
