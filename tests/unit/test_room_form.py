"""The room form: what is on it, how its answers are read back, and that the
budget and size are saved before Nora runs (docs/room-form-plan.md)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from app.agents.screen_actions import tap_of
from app.agents.work import TurnWork
from app.core.config import RoomFormSettings
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.catalog_overview import CatalogOverview, SubcategoryShelf
from app.schemas.chat import ChatRequest
from app.schemas.geometry import RoomMeasurementRole
from app.schemas.language import ReplyLanguage
from app.schemas.room_form import YOU_DECIDE, RoomFormAction
from app.services.card_labels import card_labels
from app.services.product_display import ProductDisplayWords
from app.services.room_form import FormKnown, build_room_form, read_room_form
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics

from tests.unit.agents.fakes import (
    ATTRIBUTES,
    CONTEXT,
    TAXONOMY,
    agent_services,
    real_loop,
    tool_context,
)

LOOP = real_loop()
WORDS = ProductDisplayWords(TAXONOMY, ATTRIBUTES)
SETTINGS = RoomFormSettings()


def _shelf(kind: str, count: int, **extra: Any) -> SubcategoryShelf:
    return SubcategoryShelf(
        commerce_category="seating",
        commerce_subcategory=kind,
        active_count=count,
        price_minimum=Decimal(900),
        price_maximum=Decimal(9000),
        **extra,
    )


OVERVIEW = CatalogOverview(
    store_id=CONTEXT.store_id,
    currency="SAR",
    shelves=(
        _shelf("sofa", 40, colours=("Beige", "Grey"), styles=("Modern", "Japandi")),
        _shelf("chair", 8, colours=("Olive",), styles=("Bohemian",)),
    ),
)


NOTHING_KNOWN = FormKnown()


def _form(known: FormKnown = NOTHING_KNOWN, language: ReplyLanguage = ReplyLanguage.EN) -> Any:
    built = build_room_form(
        number=1,
        settings=SETTINGS,
        labels=card_labels(language),
        words=WORDS,
        language=language,
        overview=OVERVIEW,
        main_kinds=("sofa", "sectional-sofa", "sofa-set"),
        known=known,
    )
    assert built is not None
    return built


def test_the_form_asks_budget_size_colours_and_style() -> None:
    form, record = _form()

    assert [q.kind for q in form.questions] == ["budget", "size", "colours", "style"]
    budget = form.questions[0]
    assert budget.required
    assert [c.key for c in budget.choices] == ["5000", "10000", "15000", "20000"]
    assert budget.choices[1].label == "10,000 SAR"
    assert [c.key for c in form.questions[1].choices] == ["small", "medium", "large"]
    assert form.questions[1].choices[0].label == "Small · about 3 \u00d7 3 m"
    sides = form.questions[1].dimensions
    assert sides is not None and (sides.length_label, sides.unit) == ("Length", "m")
    assert form.questions[1].placeholder is None
    # Only what the room's main piece is stocked in - not the chair's.
    assert [c.key for c in form.questions[2].choices] == ["Beige", "Grey"]
    assert form.questions[2].placeholder is None
    assert [c.key for c in form.questions[3].choices] == ["Modern", "Japandi", YOU_DECIDE]
    assert form.submit_label == "Design it"
    assert record.budgets["10000"] == Decimal(10000)


def test_what_is_known_is_left_off_and_nothing_left_means_no_form() -> None:
    form, _ = _form(FormKnown(budget=True, colours=True, styles=True))

    assert [q.kind for q in form.questions] == ["size"]
    assert (
        build_room_form(
            number=2,
            settings=SETTINGS,
            labels=card_labels(ReplyLanguage.EN),
            words=WORDS,
            language=ReplyLanguage.EN,
            overview=OVERVIEW,
            main_kinds=("sofa",),
            known=FormKnown(budget=True, size=True, colours=True, styles=True),
        )
        is None
    )


def test_colours_said_leave_the_style_still_to_ask() -> None:
    form, record = _form(FormKnown(budget=True, colours=True))

    assert [q.kind for q in form.questions] == ["size", "style"]
    assert record.colours == () and not record.looks_answered


def test_a_style_said_leaves_the_colours_still_to_ask() -> None:
    form, record = _form(FormKnown(styles=True))

    assert [q.kind for q in form.questions] == ["budget", "size", "colours"]
    assert record.styles == ()


def test_the_form_reads_in_arabic() -> None:
    form, _ = _form(language=ReplyLanguage.AR)

    assert form.submit_label == "صمّم الغرفة"
    assert form.questions[1].choices[0].label.startswith("صغيرة")


def test_chips_are_read_back_and_unoffered_keys_ignored() -> None:
    _, record = _form()

    answers = read_room_form(
        record,
        RoomFormAction(
            form=1,
            budget="10000",
            size="medium",
            colours=("Beige", "Purple"),
            styles=("Japandi", YOU_DECIDE),
        ),
    )

    assert answers is not None
    assert answers.budget is not None and answers.budget.max_amount == Decimal(10000)
    assert answers.budget.currency == "SAR"
    assert answers.geometry is not None
    assert answers.geometry.of(RoomMeasurementRole.ROOM_LENGTH)[0].centimetres == Decimal(400)
    assert answers.colours == ("Beige",)  # Purple was never offered
    assert answers.styles == ("Japandi",) and answers.look_left_to_nora


def test_typed_answers_are_read_only_when_they_read_exactly() -> None:
    _, record = _form()

    exact = read_room_form(
        record,
        RoomFormAction(
            form=1, budget_text="8,500 SAR", length_m=Decimal("4.2"), width_m=Decimal(5)
        ),
    )
    vague = read_room_form(
        record, RoomFormAction(form=1, budget_text="around ten thousand", length_m=Decimal(4))
    )

    assert exact is not None and exact.budget is not None
    assert exact.budget.max_amount == Decimal(8500)
    assert exact.geometry is not None
    assert exact.geometry.of(RoomMeasurementRole.ROOM_LENGTH)[0].centimetres == Decimal(420)
    assert vague is not None and vague.budget is None and vague.budget_missing is False
    assert vague.budget_words == "around ten thousand"
    assert vague.geometry is None  # one side alone is not a size


def test_an_old_form_is_not_read() -> None:
    _, record = _form()

    assert read_room_form(record, RoomFormAction(form=7, budget="10000")) is None


def test_the_answers_go_to_nora_as_one_tap() -> None:
    request = ChatRequest(
        session_id="s",
        store_id=CONTEXT.store_id,
        message="Here are my answers",
        room_form_action=RoomFormAction(form=1, size="small", styles=("Modern",)),
    )

    tap = tap_of(request)

    assert tap is not None and tap.runs == "ask_room_form"
    assert tap.event.button == "room_form_answers"
    assert tap.event.answers == {"size": "small", "styles": ["Modern"], "budget": "not given"}


class _Shelves:
    async def overview(self, context: Any) -> CatalogOverview:
        return OVERVIEW


async def _ask(state: AgentStateV1, tapped: str | None = None) -> Any:
    seating = load_seating_semantics(taxonomy=TAXONOMY)
    services = agent_services(  # type: ignore[arg-type]
        capabilities=_Shelves(),
        seating=seating,
        room_pieces=load_room_pieces(taxonomy=TAXONOMY, seating=seating),
    )
    tool = LOOP._registry.get("ask_room_form")
    assert tool is not None and tool.handler is not None
    assert tool.owners == frozenset({AgentPersona.NORA})
    work = TurnWork.starting(state)
    work.working = AgentPersona.NORA
    context = replace(tool_context(services), tapped=tapped)
    args = tool.args.model_validate({"room_type": "living room", "room_kind": "living_room"})
    return await tool.handler(args, context, work)


async def _note(state: AgentStateV1, args: dict[str, Any]) -> Any:
    tool = LOOP._registry.get("note_room")
    assert tool is not None and tool.handler is not None
    work = TurnWork.starting(state)
    work.working = AgentPersona.NORA
    return await tool.handler(tool.args.model_validate(args), tool_context(agent_services()), work)


async def test_colours_said_for_the_room_are_left_off_its_form() -> None:
    noted = await _note(
        AgentStateV1(),
        {"room_type": "living room", "room_kind": "living_room", "colours": ["Beige"]},
    )

    shown = await _ask(noted.state)

    assert noted.facts["colours"] == ["Beige"]
    assert shown.facts["form_shows"] == ["budget", "size", "style"]


async def test_colours_said_again_replace_the_colours_and_keep_the_style() -> None:
    first = await _note(
        AgentStateV1(),
        {
            "room_type": "living room",
            "room_kind": "living_room",
            "colours": ["Beige"],
            "styles": ["Modern"],
        },
    )

    second = await _note(first.state, {"room_kind": "living_room", "colours": ["Grey"]})

    assert second.facts["colours"] == ["Grey"]
    assert second.facts["styles"] == ["Modern"]


async def test_another_room_does_not_take_the_last_rooms_colours() -> None:
    blue_bedroom = await _note(
        AgentStateV1(), {"room_type": "bedroom", "room_kind": "bedroom", "colours": ["Grey"]}
    )

    living = await _note(
        blue_bedroom.state, {"room_type": "living room", "room_kind": "living_room"}
    )
    shown = await _ask(living.state)

    assert "colours" not in living.facts
    assert shown.facts["form_shows"] == ["budget", "size", "colours", "style"]


async def test_nora_shows_the_form_and_the_session_remembers_it() -> None:
    outcome = await _ask(AgentStateV1())

    assert outcome.status == "ok"
    assert outcome.payload.form.number == 1
    assert outcome.facts["form_shows"] == ["budget", "size", "colours", "style"]
    assert outcome.state.room_form.number == 1
    assert outcome.state.room_project.room_kind == "living_room"


async def test_a_form_answered_without_a_budget_comes_back_marked() -> None:
    first = await _ask(AgentStateV1())
    answered = first.state.model_copy(
        update={"room_form": first.state.room_form.model_copy(update={"looks_answered": True})}
    )

    again = await _ask(answered, tapped="room_form_answers")

    assert again.payload.form.number == 2
    assert again.payload.form.budget_missing
    assert [q.kind for q in again.payload.form.questions] == ["budget", "size"]


async def test_the_budget_and_size_are_saved_before_nora_reads_them() -> None:
    from app.schemas.session import new_session
    from app.services.chat_runtime import _with_room_form
    from app.services.session_turns import LoadedSession

    shown = await _ask(AgentStateV1())
    envelope = new_session()
    loaded = LoadedSession(
        envelope=envelope.model_copy(update={"state": shown.state}),
        loaded_revision=0,
        existed=True,
    )
    request = ChatRequest(
        session_id="s",
        store_id=CONTEXT.store_id,
        message="Here are my answers",
        room_form_action=RoomFormAction(form=1, budget="15000", size="large", colours=("Grey",)),
    )

    state = _with_room_form(request, loaded).envelope.state

    room = state.room_project
    assert room is not None and room.budget is not None
    assert room.budget.max_amount == Decimal(15000)
    assert room.geometry is not None
    assert room.geometry.of(RoomMeasurementRole.ROOM_WIDTH)[0].centimetres == Decimal(500)
    assert state.room_form is not None and state.room_form.looks_answered


async def test_a_look_left_blank_is_left_to_nora_never_asked_again() -> None:
    from app.schemas.session import new_session
    from app.services.chat_runtime import _with_room_form
    from app.services.session_turns import LoadedSession

    shown = await _ask(AgentStateV1())
    loaded = LoadedSession(
        envelope=new_session().model_copy(update={"state": shown.state}),
        loaded_revision=0,
        existed=True,
    )
    request = ChatRequest(
        session_id="s",
        store_id=CONTEXT.store_id,
        message="Here are my answers",
        room_form_action=RoomFormAction(
            form=1, budget="10000", length_m=Decimal("4.2"), width_m=Decimal("5.1")
        ),
    )

    state = _with_room_form(request, loaded).envelope.state
    again = await _ask(state, tapped="room_form_answers")

    assert state.room_form is not None and state.room_form.looks_answered
    assert again.code == "nothing_to_ask"
