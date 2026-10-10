"""The buttons on a card: Select as before, ♡ Like, and More like this.

docs/designer-led-shopping-plan.md, phase 3. A like is silent and kept apart
from the picks; selecting from the liked list is a pick like any other; More
like this is the similarity search a typed "more like the second one" runs,
from any card still on screen.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.core.config import CustomerAgentSettings
from app.core.exceptions import PickLimitError, PickUnavailableError
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.agent_state import AgentStateV1, ProductInteractionState
from app.schemas.agent_turn import CustomerTurnInput
from app.schemas.card_comparison import CardRef
from app.schemas.grounding import TurnFailureCode
from app.schemas.picks import (
    LikeCardAction,
    PicksRequest,
    SelectLikedAction,
    UnlikeAction,
)
from app.schemas.product_action import MoreLikeThisAction
from app.schemas.resolution import ResolvedProductReference
from app.schemas.session import SessionEnvelope
from app.services.picks import PicksRuntime
from app.services.response_view import route_response

from tests.unit.test_chat_api import FakeSessionStore
from tests.unit.test_picks import (
    CONTEXT,
    HISTORY,
    SESSION,
    STORE,
    _runtime,
    _state,
    _stored,
    _tick,
)
from tests.unit.test_turn_coordinator import (
    OFF_SCREEN,
    FakeHydration,
    FakeReferences,
    _coordinator,
)
from tests.unit.test_turn_coordinator import _state as _turn_state


def _request(action: Any) -> PicksRequest:
    return PicksRequest(session_id=SESSION, store_id=STORE, action=action)


def _liked(state: AgentStateV1, liked: tuple[int, ...]) -> AgentStateV1:
    return state.model_copy(
        update={
            "product_interaction": state.product_interaction.model_copy(
                update={"liked_product_ids": liked}
            )
        }
    )


def _buttons_runtime(sessions: FakeSessionStore, **settings: Any) -> PicksRuntime:
    return _runtime(sessions, **settings)


async def _saved(sessions: FakeSessionStore) -> SessionEnvelope:
    envelope = await sessions.load(STORE, SESSION)
    assert envelope is not None
    return envelope


# ── liking ══════════════════════════════════════════════════════════════════


async def test_a_like_is_kept_apart_from_the_picks_and_says_nothing() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())

    response = await _buttons_runtime(sessions).apply(
        _request(LikeCardAction(ordinal=2, list_revision=1)), CONTEXT
    )

    saved = await _saved(sessions)
    assert saved.state.product_interaction.liked_product_ids == (102,)
    assert saved.state.product_interaction.selected_product_ids == ()
    assert saved.conversation == HISTORY
    assert response.liked is not None and [item.liked for item in response.liked] == [1]
    assert response.liked[0].positions[0].ordinal == 2
    assert response.picks == () and response.goes_with is None


async def test_liking_a_liked_card_again_writes_nothing() -> None:
    sessions = FakeSessionStore()
    before = await _stored(sessions, _liked(_state(), (102,)))

    await _buttons_runtime(sessions).apply(
        _request(LikeCardAction(ordinal=2, list_revision=1)), CONTEXT
    )

    assert (await _saved(sessions)).session_revision == before.session_revision


async def test_past_the_limit_the_oldest_like_is_let_go() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _liked(_state(), (101, 102)))

    await _buttons_runtime(sessions, max_likes=2).apply(
        _request(LikeCardAction(ordinal=3, list_revision=1)), CONTEXT
    )

    assert (await _saved(sessions)).state.product_interaction.liked_product_ids == (102, 103)


async def test_an_unlike_removes_that_like_and_only_that() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _liked(_state(picks=(102,)), (101, 102)))

    await _buttons_runtime(sessions).apply(_request(UnlikeAction(liked=2)), CONTEXT)

    interaction = (await _saved(sessions)).state.product_interaction
    assert interaction.liked_product_ids == (101,)
    assert interaction.selected_product_ids == (102,)


async def test_an_unlike_that_is_not_there_is_refused() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _liked(_state(), (101,)))

    with pytest.raises(PickUnavailableError):
        await _buttons_runtime(sessions).apply(_request(UnlikeAction(liked=2)), CONTEXT)


async def test_selecting_from_the_liked_list_is_a_pick_like_a_tick() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _liked(_state(), (103,)))

    response = await _buttons_runtime(sessions).apply(_request(SelectLikedAction(liked=1)), CONTEXT)

    interaction = (await _saved(sessions)).state.product_interaction
    assert interaction.selected_product_ids == (103,)
    assert interaction.focused_product_id == 103
    assert interaction.liked_product_ids == (103,)
    assert response.goes_with == 1
    assert response.liked is not None and response.liked[0].picked


async def test_switched_off_a_like_is_refused_and_no_liked_list_reported() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())
    runtime = _buttons_runtime(sessions, designer_led_buttons=False)

    with pytest.raises(PickUnavailableError):
        await runtime.apply(_request(LikeCardAction(ordinal=1, list_revision=1)), CONTEXT)
    response = await runtime.apply(_tick(1), CONTEXT)
    assert response.liked is None


def test_a_session_saved_before_likes_still_loads_and_an_empty_list_is_not_written() -> None:
    old = ProductInteractionState(presented_product_ids=(1,), presented_search_revision=1)
    dumped = old.model_dump(mode="json")

    assert "liked_product_ids" not in dumped
    assert ProductInteractionState.model_validate(dumped).liked_product_ids == ()


def test_the_settings_default_the_buttons_on() -> None:
    settings = CustomerAgentSettings()
    assert settings.designer_led_buttons and settings.max_likes == 20


@pytest.mark.parametrize(
    "model", [LikeCardAction, UnlikeAction, SelectLikedAction, MoreLikeThisAction]
)
def test_no_button_names_a_product_id(model: Any) -> None:
    names = set(model.model_fields)
    assert not {name for name in names if name == "id" or name.endswith(("_id", "_ids"))}


# ── More like this and the liked list in words ══════════════════════════════


class ListReferences(FakeReferences):
    async def resolve_on_list(
        self, ordinal: int, list_revision: int | None, state: Any, context: Any
    ) -> Any:
        self.calls.append(((ordinal, list_revision), state))
        return self.default


def _more_like(card: int = 2, revision: int = 1) -> CustomerTurnInput:
    return CustomerTurnInput(
        message="More like the Sofa",
        state=_turn_state(),
        context=CONTEXT,
        product_action=MoreLikeThisAction(card=CardRef(list_revision=revision, ordinal=card)),
    )


async def test_more_like_this_searches_from_that_card_on_its_own_list() -> None:
    references = ListReferences(default=ResolvedProductReference(product_id=OFF_SCREEN))
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        references=references,
        hydration=FakeHydration(available=(OFF_SCREEN,)),
        designer_led_buttons=True,
    )

    result = await coordinator.run(_more_like(card=2, revision=1))

    assert references.calls[0][0] == (2, 1)
    assert parts["decisions"].inputs == [], "a tap is the decision"
    executed = parts["pipeline"].calls[0]
    assert executed.request.commerce_subcategory == "sofa"
    assert executed.request.exclude_product_ids == (OFF_SCREEN,)
    assert result.grounding.search is not None


async def test_more_like_this_switched_off_searches_nothing() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        references=ListReferences(),
        designer_led_buttons=False,
    )

    result = await coordinator.run(_more_like())

    assert parts["pipeline"].calls == []
    assert result.grounding.failure is not None


def _show_liked() -> CustomerAgentDecision:
    return CustomerAgentDecision(action=AgentAction.SHOW_SELECTION, show_liked=True)


async def test_what_have_i_liked_shows_the_liked_list_not_the_picks() -> None:
    coordinator, _ = _coordinator(
        _show_liked(), hydration=FakeHydration(available=(10, 11)), designer_led_buttons=True
    )
    state = _liked(_turn_state(selected=(11,)), (10,))

    result = await coordinator.run(
        CustomerTurnInput(message="what have I liked so far?", state=state, context=CONTEXT)
    )

    selection = result.grounding.selection
    assert selection is not None and selection.liked
    assert len(selection.products) == 1
    assert getattr(route_response(result).primary, "selection_liked", False)
    assert result.liked is not None and [item.liked for item in result.liked] == [1]


async def test_the_liked_list_is_drawn_as_cards_that_can_be_acted_on() -> None:
    from app.services.chat_runtime import ChatRuntime

    coordinator, _ = _coordinator(
        _show_liked(), hydration=FakeHydration(available=(10,)), designer_led_buttons=True
    )

    result = await coordinator.run(
        CustomerTurnInput(
            message="what have I liked so far?",
            state=_liked(_turn_state(), (10,)),
            context=CONTEXT,
        )
    )

    presentation = ChatRuntime.presentation(result)
    assert presentation is not None and presentation.product_source == "liked"


async def test_more_like_this_from_a_liked_product() -> None:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        hydration=FakeHydration(available=(OFF_SCREEN,)),
        designer_led_buttons=True,
    )

    result = await coordinator.run(
        CustomerTurnInput(
            message="More like the Sofa",
            state=_liked(_turn_state(), (OFF_SCREEN,)),
            context=CONTEXT,
            product_action=MoreLikeThisAction(liked=1),
        )
    )

    assert parts["pipeline"].calls[0].request.exclude_product_ids == (OFF_SCREEN,)
    assert result.grounding.search is not None


def test_more_like_this_names_one_card_or_one_like() -> None:
    with pytest.raises(ValueError, match="liked product"):
        MoreLikeThisAction()
    with pytest.raises(ValueError, match="liked product"):
        MoreLikeThisAction(card=CardRef(list_revision=1, ordinal=1), liked=1)


async def test_nothing_liked_yet_is_said_as_such() -> None:
    coordinator, _ = _coordinator(_show_liked(), designer_led_buttons=True)

    result = await coordinator.run(
        CustomerTurnInput(message="what have I liked?", state=_turn_state(), context=CONTEXT)
    )

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.NOTHING_LIKED


def test_the_liked_list_is_shown_only_with_show_selection() -> None:
    with pytest.raises(ValueError, match="show_selection"):
        CustomerAgentDecision(action=AgentAction.SEARCH, show_liked=True)


# ── after QA ════════════════════════════════════════════════════════════════


async def test_queued_unlikes_by_card_survive_the_renumbering() -> None:
    """Two hearts taken back one after the other remove exactly those two,
    however the liked list renumbers between them."""
    sessions = FakeSessionStore()
    await _stored(sessions, _liked(_state(), (101, 102, 103)))
    runtime = _buttons_runtime(sessions)

    for ordinal in (1, 2):
        await runtime.apply(_request(UnlikeAction(ordinal=ordinal, list_revision=1)), CONTEXT)

    assert (await _saved(sessions)).state.product_interaction.liked_product_ids == (103,)


async def test_unliking_a_card_that_is_not_liked_writes_nothing() -> None:
    sessions = FakeSessionStore()
    before = await _stored(sessions, _liked(_state(), (101,)))

    await _buttons_runtime(sessions).apply(
        _request(UnlikeAction(ordinal=3, list_revision=1)), CONTEXT
    )

    assert (await _saved(sessions)).session_revision == before.session_revision


@pytest.mark.parametrize(
    "fields",
    [{}, {"liked": 1, "ordinal": 1, "list_revision": 1}, {"ordinal": 1}, {"list_revision": 1}],
)
def test_an_unlike_names_its_like_one_way(fields: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="one of the two"):
        UnlikeAction(**fields)


async def test_selecting_a_like_into_full_picks_is_refused() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _liked(_state(picks=(101, 102)), (103,)))

    with pytest.raises(PickLimitError):
        await _buttons_runtime(sessions, max_picks=2).apply(
            _request(SelectLikedAction(liked=1)), CONTEXT
        )


def test_likes_survive_a_new_search_and_a_chosen_combination() -> None:
    from app.schemas.agent_state import (
        OfferedCombination,
        OfferedCombinationLine,
        SeatingOfferState,
    )
    from app.schemas.seating_solution import SeatingShape
    from app.services.agent_state import commit_search_results
    from app.services.turn_coordinator import _with_chosen_combination

    state = _liked(_state(), (102,))
    searched = commit_search_results(state, (201, 202))
    offer = SeatingOfferState(
        target_seats=8,
        shown=(
            OfferedCombination(
                shape=SeatingShape.SEPARATE_SOFAS,
                lines=(OfferedCombinationLine(product_id=201, quantity=2),),
            ),
        ),
    )
    chosen = _with_chosen_combination(searched, offer, 1)

    assert searched.product_interaction.liked_product_ids == (102,)
    assert chosen.product_interaction.liked_product_ids == (102,)


async def test_more_like_this_on_a_card_no_longer_on_screen_is_a_plain_failure() -> None:
    from app.schemas.resolution import ReferenceFailureReason, ReferenceUnresolved

    references = ListReferences(
        default=ReferenceUnresolved(reason=ReferenceFailureReason.NO_PRESENTED_RESULTS)
    )
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        references=references,
        designer_led_buttons=True,
    )

    result = await coordinator.run(_more_like(card=1, revision=99))

    assert parts["pipeline"].calls == []
    assert result.grounding.deterministic_clarification is None
    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.CARD_NOT_ON_SCREEN


async def test_switched_off_what_have_i_liked_shows_the_picks() -> None:
    coordinator, _ = _coordinator(
        _show_liked(), hydration=FakeHydration(available=(10, 11)), designer_led_buttons=False
    )
    state = _liked(_turn_state(selected=(11,)), (10,))

    result = await coordinator.run(
        CustomerTurnInput(message="what have I liked?", state=state, context=CONTEXT)
    )

    selection = result.grounding.selection
    assert selection is not None and not selection.liked
    assert result.liked is None


async def test_the_reply_is_told_how_many_likes_are_also_picks() -> None:
    coordinator, _ = _coordinator(
        _show_liked(), hydration=FakeHydration(available=(10, 11)), designer_led_buttons=True
    )
    state = _liked(_turn_state(selected=(11,)), (10, 11))

    result = await coordinator.run(
        CustomerTurnInput(message="what have I liked?", state=state, context=CONTEXT)
    )

    assert getattr(route_response(result).primary, "liked_also_picked", None) == 1


async def test_the_liked_list_never_ends_on_narrowing_the_cards_behind_it() -> None:
    from app.schemas.next_step import NextStepKind
    from app.services.next_step import next_step

    coordinator, _ = _coordinator(
        _show_liked(), hydration=FakeHydration(available=(10,)), designer_led_buttons=True
    )
    state = _liked(_turn_state(selected=()), (10,))

    result = await coordinator.run(
        CustomerTurnInput(message="what have I liked?", state=state, context=CONTEXT)
    )

    step = next_step(result, None)
    assert step is not None and step.kind is not NextStepKind.KEEP_BROWSING
