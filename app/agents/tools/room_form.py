"""Nora's room form: the questions every room shares, asked together.

When she starts a room, Nora shows one small form - the budget, the room's
size, its colours and style - instead of asking them one reply at a time. She
decides when; code decides what is on it, from fixed settings and what this
store stocks, and leaves off what is already known (docs/room-form-plan.md).
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import Field, create_model

from app.agents.context import ToolContext
from app.agents.payloads import RoomFormShown
from app.agents.tools.base import ToolArgs, ToolOutcome, ZoryTool, one_of
from app.agents.tools.schemas import ToolSchemas
from app.agents.work import ToolStatus, TurnWork
from app.core.exceptions import IntegrationUnavailableError
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import RoomProjectState
from app.schemas.agent_updates import AgentStateUpdate, RoomProjectUpdate
from app.schemas.language import ReplyLanguage
from app.services.agent_state import apply_update, record_room_form
from app.services.card_labels import card_labels
from app.services.fit import knows_room_size
from app.services.product_display import ProductDisplayWords
from app.services.room_form import FormKnown, build_room_form

NORA = frozenset({AgentPersona.NORA})

FORM_ANSWERED = "room_form_answers"
"""The button the form's Design it sends (`TapEvent.button`)."""


def _args(schemas: ToolSchemas) -> type[ToolArgs]:
    rooms = schemas.room_pieces
    kinds = tuple(rooms.kinds) if rooms is not None else ()
    fields: dict[str, Any] = {
        "room_type": (
            str,
            Field(min_length=1, max_length=60, description="The room, in their words."),
        ),
        "room_kind": (one_of(kinds) | None, None) if kinds else (str | None, None),
    }
    return cast(type[ToolArgs], create_model("AskRoomFormArgs", __base__=ToolArgs, **fields))


def ask_room_form_tool(schemas: ToolSchemas) -> ZoryTool[Any]:
    async def ask(args: Any, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        try:
            overview = await services.capabilities.overview(context.retailer)
        except IntegrationUnavailableError:
            return ToolOutcome(status=ToolStatus.REFUSED, code="catalog_unavailable")
        state = work.staged
        current = state.room_project
        another_in_progress = (
            current is not None
            and bool(current.in_cart or current.bundle_items)
            and _another_room(current, args)
        )
        if not another_in_progress:
            # The room the form is for, noted as note_room would.
            same = (
                current is not None
                and current.room_kind is not None
                and args.room_kind == current.room_kind
            )
            state = apply_update(
                state,
                AgentStateUpdate(
                    room_project=RoomProjectUpdate(
                        room_type=None if same else args.room_type, room_kind=args.room_kind
                    )
                ),
            )
        room = None if another_in_progress else state.room_project
        previous = state.room_form
        known = FormKnown(
            budget=room is not None
            and room.budget is not None
            and room.budget.max_amount is not None,
            size=room is not None and (knows_room_size(room.geometry) or bool(room.size_declined)),
            look=previous is not None and previous.looks_answered and not another_in_progress,
        )
        language = context.language or ReplyLanguage.EN
        rooms = services.room_pieces
        built = build_room_form(
            number=(previous.number + 1) if previous is not None else 1,
            settings=services.room_form,
            labels=card_labels(language),
            words=services.display or ProductDisplayWords(services.taxonomy, services.attributes),
            language=language,
            overview=overview,
            main_kinds=rooms.main_kinds(args.room_kind or args.room_type) if rooms else (),
            known=known,
            budget_missing=context.tapped == FORM_ANSWERED,
        )
        if built is None:
            return ToolOutcome(
                status=ToolStatus.REFUSED,
                code="nothing_to_ask",
                facts={
                    "next": "the budget, size and look are known: ask the room's follow-up "
                    "if it has one left, else build it"
                },
                state=state,
            )
        form, record = built
        return ToolOutcome(
            facts={
                "form_shows": [q.kind for q in form.questions],
                "next": "reply with one short line and no question of your own; the form "
                "asks them. Wait for their answers.",
            },
            state=record_room_form(state, record),
            payload=RoomFormShown(form=form),
        )

    return ZoryTool(
        name="ask_room_form",
        description=(
            "Show the room form when you start designing a room: its budget, size, colours "
            "and style together, each with choices and a box to type. Only what is not "
            "known yet is on it. Call it once the room is clear, before any question of "
            "your own."
        ),
        args=_args(schemas),
        owners=NORA,
        handler=ask,
    )


def _another_room(current: RoomProjectState, args: Any) -> bool:
    if args.room_kind is not None and current.room_kind is not None:
        return bool(args.room_kind != current.room_kind)
    if current.room_type is not None:
        return bool(args.room_type.casefold().strip() != current.room_type.casefold().strip())
    return False
