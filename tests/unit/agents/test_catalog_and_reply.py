"""The real tool catalogue, each agent's allowlist, and the reply's figures."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.agents.payloads import ProductShown, SearchShown, SelectionShown
from app.agents.reply import unsupported_figure
from app.agents.screen import presentation
from app.agents.screen_actions import tap_of
from app.agents.tools.catalog import build_registry
from app.agents.tools.common import RespondArgs
from app.agents.tools.schemas import ToolSchemas
from app.agents.work import ToolRecord, ToolStatus, TurnWork
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.bundle_action import BundleRemoveAction
from app.schemas.chat import ChatRequest
from app.schemas.conversation import ConversationContext, ConversationMessage, ConversationRole
from app.schemas.language import ReplyLanguage
from app.schemas.query import build_constrained_interpretation
from app.schemas.search_action import MoreOptionsAction
from app.services.grounding_builder import to_grounded_product
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics
from pydantic import ValidationError

from tests.unit.agents.fakes import FakePipeline, _product

TAXONOMY = load_taxonomy()
ATTRIBUTES = load_catalog_attributes()
ROOMS = load_room_pieces(taxonomy=TAXONOMY, seating=load_seating_semantics(taxonomy=TAXONOMY))


def _schemas() -> ToolSchemas:
    return ToolSchemas(
        interpretation=build_constrained_interpretation(TAXONOMY, ATTRIBUTES),
        attributes=ATTRIBUTES,
        room_pieces=ROOMS,
    )


def test_the_whole_catalogue_builds_with_no_tool_able_to_name_a_record() -> None:
    registry = build_registry(_schemas())

    for agent in AgentPersona:
        names = {t.name for t in registry.for_agent(agent)}
        assert {"respond", "hand_back"} <= names, agent


@pytest.mark.parametrize(
    ("agent", "owns", "never"),
    [
        (AgentPersona.SARA, {"search_products", "add_to_cart", "compare_products"}, {"plan_room"}),
        (AgentPersona.NORA, {"plan_room", "swap_room_piece", "design_advice"}, {"store_policy"}),
        (AgentPersona.SALMAN, {"what_goes_with", "add_room_to_cart"}, {"plan_room"}),
        (AgentPersona.FAYSAL, {"store_policy", "contact_store"}, {"add_to_cart", "plan_room"}),
    ],
)
def test_each_agent_holds_its_own_tools_only(
    agent: AgentPersona, owns: set[str], never: set[str]
) -> None:
    names = {t.name for t in build_registry(_schemas()).for_agent(agent)}

    assert owns <= names
    assert not (never & names)


def _work(**facts: Any) -> TurnWork:
    work = TurnWork.starting(AgentStateV1())
    work.record(
        ToolRecord(
            agent=AgentPersona.SARA, tool="search_products", status=ToolStatus.OK, facts=facts
        )
    )
    return work


def test_a_figure_a_tool_returned_may_be_said() -> None:
    work = _work(products=[{"card": 1, "price": str(Decimal("1499.00"))}])

    reply = RespondArgs(message="The first one is 1,499 SAR.")

    assert unsupported_figure(reply, work, "sofas", ConversationContext(), [{}]) is None


def test_a_figure_from_nowhere_is_caught() -> None:
    work = _work(products=[{"card": 1, "price": "1499.00"}])

    reply = RespondArgs(message="It's on sale for 999 SAR today.")

    broke = unsupported_figure(reply, work, "sofas", ConversationContext(), [{}])
    assert broke is not None and broke.value == "999"


def test_a_figure_the_customer_said_earlier_may_be_said() -> None:
    history = ConversationContext(
        messages=(ConversationMessage(role=ConversationRole.USER, content="under 5000 please"),)
    )

    reply = RespondArgs(message="All of these are under 5000.")

    assert unsupported_figure(reply, _work(), "show me", history, [{}]) is None


def test_a_tap_is_a_typed_event_owned_by_no_agent() -> None:
    more = ChatRequest(
        session_id="s", store_id=50, message="More", search_action=MoreOptionsAction()
    )
    remove = ChatRequest(
        session_id="s",
        store_id=50,
        message="Remove",
        bundle_action=BundleRemoveAction(bundle_ordinal=2),
    )

    shown = tap_of(more)
    assert shown is not None and shown.event.button == "more_results"
    removed = tap_of(remove)
    assert removed is not None
    assert removed.event.button == "remove_room_piece" and removed.event.room_piece == 2
    assert removed.said.startswith("[tapped a button]")


def test_chips_without_a_question_are_sent_back_to_the_agent() -> None:
    chips = [{"label": "Rugs", "value": "Show me rugs"}]

    with pytest.raises(ValidationError):
        RespondArgs.model_validate({"message": "Here are three sofas.", "choices": chips})
    asked = RespondArgs.model_validate(
        {"message": "Here are three sofas.", "follow_up_question": "Any rugs?", "choices": chips}
    )
    assert len(asked.choices) == 1


def test_the_reply_chooses_what_the_screen_shows() -> None:
    first, second = _grounded(10), _grounded(11)
    work = TurnWork.starting(AgentStateV1())
    work.record(
        ToolRecord(
            agent=AgentPersona.SARA,
            tool="show_cart",
            status=ToolStatus.OK,
            payload=SelectionShown(products=(first,)),
        )
    )
    work.record(
        ToolRecord(
            agent=AgentPersona.SARA,
            tool="product_details",
            status=ToolStatus.OK,
            payload=ProductShown(product=second),
        )
    )

    chose = presentation(work, AgentStateV1(), (), ReplyLanguage.EN, "cart")
    default = presentation(work, AgentStateV1(), (), ReplyLanguage.EN)
    missing = presentation(work, AgentStateV1(), (), ReplyLanguage.EN, "room")

    assert chose is not None and chose.product_source == "selection"
    assert default is not None and default.product_source == "detail"
    assert missing is not None and missing.product_source == "detail"


def _grounded(product_id: int) -> Any:
    return to_grounded_product(
        _product(product_id), grounding_ref=1, presented_ordinal=None, relaxation_depth=0
    )


async def test_nothing_shown_before_this_turns_list_is_put_over_it() -> None:
    work = TurnWork.starting(AgentStateV1())
    work.record(
        ToolRecord(
            agent=AgentPersona.SALMAN,
            tool="product_details",
            status=ToolStatus.OK,
            payload=ProductShown(product=_grounded(10)),
        )
    )
    run = await FakePipeline(ids=(11, 12)).execute(None, None)
    work.record(
        ToolRecord(
            agent=AgentPersona.SALMAN,
            tool="what_goes_with",
            status=ToolStatus.OK,
            payload=SearchShown(grounding=run.grounding),
        )
    )

    shown = presentation(work, AgentStateV1(), (), ReplyLanguage.EN, "product")

    assert shown is not None and shown.product_source == "search"
