"""What every agent is told about the session: all of it, and no ids."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from app.agents.views import agent_messages, session_brief
from app.agents.work import TurnWork
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    CartQuantity,
    CustomerPreferenceState,
    OfferedCombination,
    OfferedCombinationLine,
    ProductInteractionState,
    RoomProjectState,
    RoomSwapState,
    SavedMeasurements,
    SeatingOfferState,
)
from app.schemas.conversation import ConversationContext, ConversationMessage, ConversationRole
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PriceConstraint,
    ProductSearchRequest,
    SeatingCapacityConstraint,
)
from app.schemas.language import ReplyLanguage
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    ResolvedSearch,
)
from app.schemas.seating_solution import SeatingShape
from app.services.agent_state import remember_taste
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.dimensions import DimensionRole
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from tests.unit.agents.fakes import (
    CONTEXT,
    FakeHydration,
    FakePipeline,
    _preference,
    agent_services,
    real_loop,
    tool_context,
)

SERVICES = agent_services(hydration=FakeHydration(available=(10, 11, 12, 13, 14), price="1500"))


def _brief_state() -> AgentStateV1:
    return AgentStateV1(
        customer_preferences=CustomerPreferenceState(
            semantic_preferences=(_preference("Modern"),),
            avoid_colours=("Red",),
            room="living room",
        ),
        active_search=ActiveSearchState(
            request=ProductSearchRequest(
                commerce_category="seating",
                commerce_subcategory="sofa",
                price=PriceConstraint(currency="SAR", max_amount=Decimal("5000")),
                seating_capacity=SeatingCapacityConstraint(min_capacity=3),
                colors_any_of=("Beige",),
            ),
            revision=1,
        ),
        product_interaction=ProductInteractionState(
            presented_product_ids=(10, 11),
            presented_search_revision=1,
            selected_product_ids=(12, 13),
            cart_quantities=(CartQuantity(product_id=13, quantity=2),),
            liked_product_ids=(14,),
        ),
        room_project=RoomProjectState(
            room_type="living room",
            room_kind="living_room",
            budget=PriceConstraint(currency="SAR", max_amount=Decimal("20000")),
            regular_seating_count=6,
            pieces=("sofa", "rug"),
        ),
        seating_offer=SeatingOfferState(
            target_seats=6,
            shown=(
                OfferedCombination(
                    shape=SeatingShape.SEPARATE_SOFAS,
                    lines=(OfferedCombinationLine(product_id=10, quantity=2),),
                ),
            ),
        ),
    )


async def test_the_brief_carries_cart_budgets_search_room_likes_and_taste() -> None:
    brief = (await session_brief(SERVICES, _brief_state(), tool_context(SERVICES))).context()

    assert brief["cart"]["totals"] == [{"currency": "SAR", "amount": "4500"}]
    assert [line["quantity"] for line in brief["cart"]["lines"]] == [1, 2]
    assert {b["applies_to"]: b["at_most"] for b in brief["budgets"]} == {
        "search": "5000",
        "room": "20000",
    }
    assert brief["search"]["kind"] == "sofa"
    assert brief["search"]["colours_required"] == ["Beige"]
    assert brief["search"]["seats_at_least"] == 3
    assert brief["room"]["head_count"] == 6
    assert brief["room"]["pieces_chosen"] == ["sofa", "rug"]
    assert [p["card"] for p in brief["liked"]] == [1]
    assert brief["taste"] == {
        "styles": ["Modern"],
        "avoid_colours": ["Red"],
        "shopping_for_room": "living room",
    }
    option = brief["pending"]["seating"]["options"][0]
    assert option["total"] == "3000"
    assert option["pieces"][0]["quantity"] == 2


async def test_the_brief_names_no_product_or_store() -> None:
    brief = (await session_brief(SERVICES, _brief_state(), tool_context(SERVICES))).context()
    text = json.dumps(brief)

    assert "example.test" not in text
    assert "product_id" not in text and "store" not in text.replace("store_currency", "")


async def test_an_empty_session_is_a_short_brief() -> None:
    brief = (await session_brief(SERVICES, AgentStateV1(), tool_context(SERVICES))).context()

    assert set(brief) == {"language", "cards_on_screen", "cart"}


def test_an_earlier_reply_shows_who_wrote_it_and_what_it_offered() -> None:
    conversation = ConversationContext(
        messages=(
            ConversationMessage(role=ConversationRole.USER, content="a sofa"),
            ConversationMessage(
                role=ConversationRole.ASSISTANT,
                content="Which room is it for?",
                agent=AgentPersona.SARA,
                choices=("Living room", "Bedroom"),
            ),
        )
    )
    messages = agent_messages(
        "rules", conversation, "the first", {}, TurnWork.starting(AgentStateV1()), history=4
    )
    reply = messages[2]

    assert isinstance(reply, AIMessage)
    assert reply.name == "sara"
    assert "[offered: Living room | Bedroom]" in str(reply.content)


def test_liking_and_avoiding_a_value_exclude_each_other() -> None:
    state = remember_taste(AgentStateV1(), colours=("Beige",), styles=("Modern",))
    state = remember_taste(state, avoid_colours=("Beige",))
    said = state.customer_preferences

    assert [p.canonical_value for p in said.semantic_preferences] == ["Modern"]
    assert said.avoid_colours == ("Beige",)

    state = remember_taste(state, colours=("Beige",), forget=("Modern",))
    said = state.customer_preferences
    assert [(p.family, p.canonical_value) for p in said.semantic_preferences] == [
        (AttributeFamily.COLOR, "Beige")
    ]
    assert said.avoid_colours == ()


async def test_what_they_avoid_pushes_down_the_next_page_too() -> None:
    pipeline = FakePipeline(ids=(10, 11))
    services = agent_services(pipeline=pipeline)
    state = remember_taste(_brief_state(), avoid_colours=("Grey",), avoid_styles=("Rustic",))

    await services.search.again_excluding((10,), state, CONTEXT)

    lean = pipeline.calls[-1].lean
    assert (lean.said_avoid_colours, lean.said_avoid_styles) == (("Red", "Grey"), ("Rustic",))


LONG_ROOM = "the guest majlis on the ground floor by the door"


def test_a_long_room_name_is_refused_by_the_tool_not_the_turn() -> None:
    tool = real_loop()._registry.get("remember_taste")
    assert tool is not None

    with pytest.raises(ValidationError):
        tool.args.model_validate(
            {"shopping_for_room": "the guest majlis on the ground floor by the door"}
        )


def test_forgetting_a_colour_stops_it_leaning_the_search_on_screen() -> None:
    state = _brief_state()
    search = state.active_search
    assert search is not None
    state = state.model_copy(
        update={
            "active_search": search.model_copy(
                update={"semantic_preferences": (_preference("Modern"),)}
            )
        }
    )

    after = remember_taste(state, forget=("Modern",))

    assert after.active_search is not None
    assert after.active_search.semantic_preferences == ()


async def test_options_for_a_room_piece_are_not_called_a_budget() -> None:
    state = _brief_state()
    assert state.active_search is not None
    state = state.model_copy(
        update={
            "room_swap": RoomSwapState(
                bundle_ordinal=1,
                role="sofa",
                bundle_revision=0,
                request=state.active_search.request,
            )
        }
    )
    brief = (await session_brief(SERVICES, state, tool_context(SERVICES))).context()

    assert [b["applies_to"] for b in brief["budgets"]] == ["room"]


async def test_a_cart_the_catalog_cannot_read_is_never_empty() -> None:
    services = agent_services(hydration=FakeHydration(available=(12,)))
    brief = (await session_brief(services, _brief_state(), tool_context(services))).context()

    assert brief["cart"]["items"] == 1
    assert brief["cart"]["not_priced"] == 1


async def test_prices_read_as_people_write_them() -> None:
    brief = (await session_brief(SERVICES, _brief_state(), tool_context(SERVICES))).context()

    assert brief["cards_on_screen"][0]["price"] == "1500"
    assert brief["pending"]["seating"]["options"][0]["pieces"][0]["price"] == "1500"


def _saved_sofa_width() -> SavedMeasurements:
    return SavedMeasurements(
        commerce_subcategory="sofa",
        dimensions=(
            DimensionConstraint(
                role=DimensionRole.OVERALL_WIDTH,
                kind=DimensionConstraintKind.MAX,
                max_cm=Decimal("220"),
            ),
        ),
        dimension_semantics=(
            DimensionConstraintSemantics(
                role=DimensionRole.OVERALL_WIDTH, strength=ConstraintStrength.LOCKED
            ),
        ),
    )


@pytest.mark.parametrize(
    ("earlier_sizes", "asked", "restored"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
async def test_a_kind_searched_again_brings_back_its_sizes(
    earlier_sizes: bool, asked: bool, restored: bool
) -> None:
    pipeline = FakePipeline(ids=(10,))
    services = agent_services(pipeline=pipeline)
    state = AgentStateV1(
        customer_preferences=CustomerPreferenceState(measurements_by_type=(_saved_sofa_width(),))
    )
    resolved = ResolvedSearch(
        request=ProductSearchRequest(commerce_category="seating", commerce_subcategory="sofa"),
        semantics=ConstraintSemantics(),
    )

    await services.search.new(
        resolved, state, CONTEXT, alongside=False, earlier_sizes=earlier_sizes, asked=asked
    )

    assert bool(pipeline.calls[-1].request.dimensions) is restored


async def test_a_head_count_orders_and_never_filters() -> None:
    pipeline = FakePipeline(ids=(10,))
    services = agent_services(pipeline=pipeline)
    tool = real_loop()._registry.get("search_products")
    assert tool is not None and tool.handler is not None
    args = tool.args.model_validate(
        {
            "search": {
                "commerce_category": "seating",
                "commerce_subcategory": "sofa",
            },
            "head_count": 4,
        }
    )

    await tool.handler(args, tool_context(services), TurnWork.starting(AgentStateV1()))

    executed = pipeline.calls[-1]
    assert executed.seat_preference == 4
    assert executed.request.seating_capacity is None


async def test_an_arabic_reply_is_given_each_cards_arabic_name() -> None:
    brief = (
        await session_brief(
            SERVICES, _brief_state(), tool_context(SERVICES, language=ReplyLanguage.AR)
        )
    ).context()

    assert brief["cards_on_screen"][0]["name_ar"] == "كنبة"
    assert brief["cart"]["lines"][0]["name_ar"] == "كنبة"
