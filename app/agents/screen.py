"""The customer's screen after a turn, built from what the tools did.

Never from the reply's words. The replying agent names what to show - a list,
one product, a comparison, a room, a seating offer, the cart - among what this
turn's tools produced; when it names nothing, the last of them is shown. The
store's support channels are drawn from its contacts.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.agents.operations.rooms import open_room_options
from app.agents.payloads import (
    ComparisonShown,
    KnowledgeShown,
    OptionsShown,
    ProductShown,
    RoomShown,
    SearchShown,
    SeatingShown,
    SelectionShown,
    SupportShown,
)
from app.agents.work import ToolStatus, TurnWork
from app.schemas.agent_state import AgentStateV1
from app.schemas.chat import ChatPresentation
from app.schemas.language import ReplyLanguage
from app.schemas.reply_choice import ReplyChoice
from app.schemas.text_choice import TextReplyChoice
from app.services.room_cart import room_in_cart
from app.services.seating_presentation import present_seating_solution
from app.services.support_channels import channels_for

_REVIEWED_BEFORE_SHOWN = frozenset({"design_room"})
"""Tools whose result is checked by the agent before the customer sees it."""

_SHOWN_AS: dict[str, tuple[type, ...]] = {
    "list": (SearchShown, OptionsShown),
    "product": (ProductShown,),
    "comparison": (ComparisonShown,),
    "room": (RoomShown,),
    "seating": (SeatingShown,),
    "cart": (SelectionShown,),
    "liked": (SelectionShown,),
    "unchanged": (),
}


def presentation(
    work: TurnWork,
    state: AgentStateV1,
    choices: Sequence[TextReplyChoice],
    language: ReplyLanguage,
    show: str | None = None,
) -> ChatPresentation | None:
    """`choices` are the reply's chips as the customer receives them - only
    those that answer a question the reply asks. `show` is what the replying
    agent chose to put on screen, from what this turn's tools produced; the
    last thing a tool showed when it chose nothing this turn produced."""
    shown = [r.payload for r in work.records if r.status is ToolStatus.OK and r.payload is not None]
    screens = [p for p in shown if not isinstance(p, SupportShown | KnowledgeShown)]
    # A room just designed is a draft until Nora has read it: it is drawn only
    # when she says so (show: "room"), never as the default.
    drafts = [
        r.payload
        for r in work.records
        if r.tool in _REVIEWED_BEFORE_SHOWN and r.status is ToolStatus.OK and r.payload is not None
    ]
    # A list the turn showed is the list "the second one" means from now on:
    # nothing shown before it may be chosen over it.
    last_list = max(
        (n for n, p in enumerate(screens) if isinstance(p, SearchShown | OptionsShown)), default=0
    )
    chosen = [
        p
        for p in screens[last_list:]
        if show is not None
        and isinstance(p, _SHOWN_AS[show])
        and (not isinstance(p, SelectionShown) or p.liked == (show == "liked"))
    ]
    candidates = chosen or [p for p in screens if not any(p is d for d in drafts)]
    # "unchanged": the agent judged this turn's results wrong to show - they
    # were drafts, and the screen stays as the customer last saw it.
    screen = candidates[-1] if candidates and show != "unchanged" else None
    support = next((p for p in reversed(shown) if isinstance(p, SupportShown)), None)
    fields: dict[str, object] = {}
    match screen:
        case SearchShown(grounding=grounding) if grounding.products:
            fields.update(
                products=grounding.products,
                product_source="search",
                list_revision=state.product_interaction.presented_search_revision,
            )
        case OptionsShown(products=products) if products:
            piece = open_room_options(state)
            fields.update(
                products=products,
                product_source="search" if piece is None else "room_options",
                options_for_piece=piece,
                list_revision=state.product_interaction.presented_search_revision,
            )
        case SelectionShown(products=products, liked=liked):
            fields.update(products=products, product_source="liked" if liked else "selection")
        case ProductShown(product=product):
            fields.update(products=(product,), product_source="detail")
        case ComparisonShown(comparison=comparison):
            fields.update(comparison=comparison)
        case RoomShown(room=room):
            fields.update(room=room, room_in_cart=room_in_cart(state) is not None)
        case SeatingShown(solution=solution):
            fields.update(seating_bundles=present_seating_solution(solution))
        case _:
            pass
    if support is not None:
        offered = support.support
        fields["support"] = channels_for(offered.contacts, language, handover=offered.handover)
    fields["choices"] = tuple(ReplyChoice(label=c.label, value=c.value) for c in choices)
    built = ChatPresentation.model_validate(fields)
    return None if built.is_empty() else built
