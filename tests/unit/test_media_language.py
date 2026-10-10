"""Media turns use the saved chat language and preserve the language feature gate."""

import pytest
from app.schemas.language import ReplyLanguage
from app.schemas.visualization import RenderView, RoomType, VisualizeRequest
from app.services.media_wording import ARABIC_ROOMS, VIEW_PHRASES

from tests.unit import test_catalog as catalog
from tests.unit import test_furniture_finder as finder
from tests.unit import test_room_visualization as rooms
from tests.unit.test_chat_api import FakeSessionStore


async def arabic_session() -> FakeSessionStore:
    sessions = FakeSessionStore()
    state = rooms.a_room_state().model_copy(update={"reply_language": ReplyLanguage.AR})
    await rooms.a_stored_session(sessions, state)
    return sessions


@pytest.mark.parametrize("count", [0, 1, 3])
async def test_photo_counts_and_followup_are_arabic_and_saved(count: int) -> None:
    sessions = await arabic_session()
    service, _ = finder.a_finding_service([finder.a_row(31 + i) for i in range(count)])
    runtime, _ = finder.a_runtime(service, sessions, arabic_replies=True)

    reply = await runtime.pick(finder.a_pick(), finder.CONTEXT)

    assert reply.reply_language is ReplyLanguage.AR
    assert "صورتك" in reply.response.message
    assert "sofa" not in reply.response.message
    assert reply.response.follow_up_question is not None
    assert reply.response.follow_up_question.startswith("هل تودّ")
    if count == 0:
        assert "لم أجد" in reply.response.message
        assert "وصف" in reply.response.follow_up_question
    elif count == 1:
        assert "أقرب منتج" in reply.response.message
    else:
        assert str(count) in reply.response.message
    saved = sessions.saved[(finder.STORE, finder.SESSION)]
    assert reply.response.message in saved.conversation.messages[-1].content
    assert reply.response.follow_up_question in saved.conversation.messages[-1].content
    assert saved.state.reply_language is ReplyLanguage.AR


@pytest.mark.parametrize("view", list(RenderView))
async def test_package_render_views_are_arabic_and_saved(view: RenderView) -> None:
    sessions = await arabic_session()
    runtime, _ = rooms.a_turn_runtime(sessions, arabic_replies=True)

    reply = await runtime.visualize(
        VisualizeRequest(session_id=rooms.SESSION, store_id=rooms.STORE, view=view),
        rooms.CONTEXT,
    )

    assert reply.reply_language is ReplyLanguage.AR
    assert "غرفة النوم" in reply.response.message
    assert VIEW_PHRASES[ReplyLanguage.AR][view] in reply.response.message
    assert sessions.saved[(rooms.STORE, rooms.SESSION)].conversation.messages[-1].content == (
        reply.response.message
    )


@pytest.mark.parametrize("dropped", [0, 1, 2])
async def test_catalog_render_localizes_style_and_unavailable_pieces(dropped: int) -> None:
    sessions = await arabic_session()
    runtime, _, _ = catalog.a_runtime(sessions, rows=[catalog.a_row(1)], arabic_replies=True)
    request = catalog.a_request(
        items=[{"product_id": i, "quantity": 1} for i in range(1, dropped + 2)]
    )

    reply = await runtime.visualize(request, catalog.CONTEXT)

    assert reply.reply_language is ReplyLanguage.AR
    assert "غرفة المعيشة بطراز" in reply.response.message
    assert "modern" not in reply.response.message.lower()
    assert "من منظور علوي مائل" in reply.response.message
    assert ("متوفر" in reply.response.message) is bool(dropped)
    if dropped == 1:
        assert "إحدى القطع" in reply.response.message
    elif dropped == 2:
        assert "قطعتين" in reply.response.message
        assert "لم تعودا متوفرتين" in reply.response.message
    assert sessions.saved[(catalog.STORE, catalog.SESSION)].conversation.messages[-1].content == (
        reply.response.message
    )


@pytest.mark.parametrize("media", ["photo", "package", "catalog"])
async def test_disabled_feature_keeps_english_even_in_arabic_session(media: str) -> None:
    sessions = await arabic_session()
    if media == "photo":
        service, _ = finder.a_finding_service()
        runtime, _ = finder.a_runtime(service, sessions)
        reply = await runtime.pick(finder.a_pick(), finder.CONTEXT)
        assert reply.response.message.startswith("Here are")
    elif media == "package":
        package_runtime, _ = rooms.a_turn_runtime(sessions)
        reply = await package_runtime.visualize(
            VisualizeRequest(session_id=rooms.SESSION, store_id=rooms.STORE), rooms.CONTEXT
        )
        assert reply.response.message.startswith("Here's your bedroom")
    else:
        catalog_runtime, _, _ = catalog.a_runtime(sessions)
        reply = await catalog_runtime.visualize(catalog.a_request(), catalog.CONTEXT)
        assert reply.response.message.startswith("Here's your modern living room")
    assert reply.reply_language is None


async def test_unset_session_language_defaults_to_english() -> None:
    runtime, _, _ = catalog.a_runtime(arabic_replies=True)
    reply = await runtime.visualize(catalog.a_request(), catalog.CONTEXT)
    assert reply.response.message == "Here's your modern living room, from above."
    assert reply.reply_language is ReplyLanguage.EN


def test_room_and_view_translations_cover_all_supported_values() -> None:
    assert set(ARABIC_ROOMS) == set(RoomType)
    for phrases in VIEW_PHRASES.values():
        assert set(phrases) == set(RenderView)
