"""The rules the tools keep whatever an agent decides: lists, offers, limits."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.agents.facts import cart_facts, product_facts
from app.agents.loop import TurnBudget, json_facts
from app.agents.operations.rooms import RoomEngine, RoomOutcome
from app.agents.reply import unsupported_figure
from app.agents.services import AgentServices
from app.agents.tools.common import RespondArgs
from app.agents.tools.shopping import _designer_questions, _executable
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
from app.schemas.discovery import DesignerQuestion
from app.schemas.query import ClarificationReason, ClarificationRequired
from app.schemas.resolution import (
    ComparisonFailureReason,
    ComparisonUnavailable,
    ResolvedProductReference,
)
from app.schemas.seating_solution import SeatingShape
from app.schemas.support import SupportContacts
from app.services.cross_sell import CompanionSearchBuilder
from app.services.support import SupportService
from app.taxonomy.subcategory_questions import load_subcategory_questions
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from tests.unit.agents.fake_chat_model import ScriptedChatModel, call, step
from tests.unit.agents.fakes import (
    ATTRIBUTES,
    OFF_SCREEN,
    TAXONOMY,
    FakeComparison,
    FakeHydration,
    FakePipeline,
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

    # The reviewed pairings the store stocks, marked; never the piece's own kind.
    assert listed.payload is None and not pipeline.calls[:-1]
    assert [k["kind"] for k in listed.facts["kinds"]] == ["carpet"]
    assert "store_shelf" in listed.facts["other_kinds"]
    assert shown.payload is not None and shown.facts["shown_kind"] == "carpet"
    assert same.code == "same_kind_as_the_piece"
    assert same.facts["use_instead"] == "more_like_this"
    assert refused.code == "kind_not_stocked"


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


def test_a_search_tells_the_agent_what_designers_ask_for_the_kind() -> None:
    services = agent_services(subcategory_questions=load_subcategory_questions(taxonomy=TAXONOMY))
    search = SimpleNamespace(commerce_category="seating", commerce_subcategory="sofa")

    asked = _designer_questions(services, search)["a_designer_might_ask"]

    # The questions as subjects, with no answer options - the agent decides the
    # options itself, from the catalog and the conversation (CLAUDE.md 10.2).
    # Each is the typed DesignerQuestion contract, not an arbitrary dict (12.1).
    assert asked
    questions = [DesignerQuestion.model_validate(topic) for topic in asked]
    assert all(q.question for q in questions)
    assert all("examples" not in topic for topic in asked)
    assert "sofa" in " ".join(q.question for q in questions).casefold()


def test_designer_question_is_a_typed_contract() -> None:
    q = DesignerQuestion(topic="budget", question="What's your approximate budget?")
    assert q.model_dump() == {"topic": "budget", "question": "What's your approximate budget?"}
    with pytest.raises(ValidationError):
        DesignerQuestion(topic="", question="x")  # a subject is required
    with pytest.raises(ValidationError):
        # No answer options on the contract - the agent decides those (10.2).
        DesignerQuestion.model_validate({"topic": "t", "question": "q", "options": []})


def test_a_kind_the_sheet_does_not_cover_yields_no_designer_topics() -> None:
    # A kind with no row, and no sheet at all: nothing is offered, and the agent
    # asks from its own judgement rather than a cheat sheet (CLAUDE.md 10.2).
    loaded = agent_services(subcategory_questions=load_subcategory_questions(taxonomy=TAXONOMY))
    uncovered = SimpleNamespace(commerce_category="not-a-real-category", commerce_subcategory=None)

    assert _designer_questions(loaded, uncovered) == {}
    assert _designer_questions(agent_services(subcategory_questions=None), uncovered) == {}


async def test_ask_about_gives_the_kinds_topics_and_shows_nothing() -> None:
    # A bare need gets the kind's designer topics before any search, so the
    # agent can ask one in its own words - nothing searched, no products shown.
    tool = _tool("ask_about")
    assert tool.owners == frozenset({AgentPersona.SARA})
    services = agent_services(subcategory_questions=load_subcategory_questions(taxonomy=TAXONOMY))

    outcome = await tool.handler(
        tool.args.model_validate(
            {"search": {"commerce_category": "seating", "commerce_subcategory": "recliner"}}
        ),
        tool_context(services),
        TurnWork.starting(AgentStateV1()),
    )

    asked = outcome.facts["a_designer_might_ask"]
    assert asked and all(q["question"] for q in asked)
    assert all("examples" not in q for q in asked)  # options are agentic
    assert outcome.payload is None


def test_a_figure_in_words_in_a_product_name_is_not_a_figure() -> None:
    facts = {"product": {"name": "Two Seater Sofa"}}
    reply = RespondArgs(message="It seats 2.")

    assert unsupported_figure(reply, _work(facts), "seats?", ConversationContext(), [{}])
