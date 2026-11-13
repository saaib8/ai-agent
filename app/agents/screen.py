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
    GridChip,
    KnowledgeShown,
    OptionsShown,
    ProductShown,
    RenderShown,
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
from app.schemas.product_action import CompanionAction
from app.schemas.reply_choice import ReplyChoice
from app.schemas.text_choice import TextReplyChoice
from app.services.card_labels import card_labels
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
    "render": (RenderShown,),
    "unchanged": (),
}


def _chip_key(label: str) -> str:
    return " ".join(label.casefold().split())


def _kinds_offered(work: TurnWork) -> dict[str, CompanionAction]:
    """The kinds this turn's what_goes_with offered, by their words: a chip
    the reply words exactly as one ("Rugs") becomes that kind's button, so a
    tap opens its page beside the pick (CLAUDE.md 10.3, 17.3) instead of being
    read as typed words. Any other chip stays words."""
    offered: dict[str, CompanionAction] = {}
    for record in work.records:
        if record.tool != "what_goes_with" or record.status is not ToolStatus.OK:
            continue
        entries = record.facts.get("kinds")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kind, family = entry.get("kind"), entry.get("family")
            if not isinstance(kind, str) or not isinstance(family, str):
                continue
            action = CompanionAction(category=family, subcategory=kind)
            for words in (entry.get("label"), entry.get("label_ar")):
                if isinstance(words, str) and words:
                    offered[_chip_key(words)] = action
    return offered


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
    beside_grid: tuple[GridChip, ...] = ()
    match screen:
        case SearchShown(
            grounding=grounding,
            more_options=more_options,
            kind_cards=kind_cards,
            kind_names=kind_names,
            grid_chips=grid_chips,
        ) if grounding.products:
            beside_grid = grid_chips
            fields.update(
                products=grounding.products,
                product_source="search",
                more_options=more_options,
                kind_cards=kind_cards,
                kind_names={
                    kind: (arabic if language is ReplyLanguage.AR and arabic else english)
                    for kind, (english, arabic) in kind_names.items()
                },
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
        case RenderShown(render=render):
            fields.update(render=render)
        case _:
            pass
    if support is not None:
        offered = support.support
        fields["support"] = channels_for(offered.contacts, language, handover=offered.handover)
    kinds = _kinds_offered(work)
    if fields.get("kind_cards"):
        # Beside a grid of kinds code draws the chips (CLAUDE.md 10.3): the
        # cards are the kinds, so a chip the reply wrote would only repeat one.
        fields["choices"] = _grid_choices(beside_grid, language)
        built = ChatPresentation.model_validate(fields)
        return None if built.is_empty() else built
    fields["choices"] = tuple(
        ReplyChoice(label=c.label, value=c.value, product_action=kinds.get(_chip_key(c.label)))
        for c in choices
    )
    built = ChatPresentation.model_validate(fields)
    return None if built.is_empty() else built


def _grid_choices(chips: Sequence[GridChip], language: ReplyLanguage) -> tuple[ReplyChoice, ...]:
    labels = card_labels(language)
    choices = []
    for chip in chips:
        if chip.offers == "another_of_its_kind":
            label, said = labels.another_kind, labels.say_another_kind
        elif chip.room is not None:
            label, said = labels.design_named_room, labels.say_design_named_room
        else:
            label, said = labels.design_room_around, labels.say_design_room_around
        name = chip.name_ar if language is ReplyLanguage.AR and chip.name_ar else chip.name
        room = chip.room or ""
        choices.append(
            ReplyChoice(
                label=label.replace("{name}", name).replace("{room}", room),
                value=said.replace("{name}", name).replace("{room}", room),
            )
        )
    return tuple(choices)
