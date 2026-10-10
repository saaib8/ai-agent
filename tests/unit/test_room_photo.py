"""Upload room photo: checked, emptied once, kept, and furnished in place.

The customer's own room is the canvas: a picture that is not a room is refused
before anything is emptied, the emptied room is kept for the session (one per
session), and every render places the pieces into it at the photo's exact size,
with no view to choose. The image and vision models are fakes; the prompts,
the store, the visualizer and the runtimes are the real ones.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from io import BytesIO
from typing import Any

import httpx
import pytest
from app.api.dependencies import retailer_context_provider, room_photo_service
from app.api.errors import register_exception_handlers
from app.api.routes.visualization import router as visualization_router
from app.core.exceptions import (
    ImageRejectedError,
    LLMUnavailableError,
    NotARoomError,
    RenderUnavailableError,
    RoomPhotoNotFoundError,
    RoomPhotoUnavailableError,
)
from app.integrations.image_generation import (
    Frame,
    GeneratedImage,
    ImageReference,
    nearest_shape,
)
from app.prompts.room_photo.v1 import EMPTY_ROOM_PROMPT
from app.repositories.room_photos import RoomPhotoStore, room_photo_key
from app.schemas.catalog import CatalogVisualizeRequest
from app.schemas.room_photo import RoomCheck, RoomPhoto
from app.schemas.visualization import RenderView, VisualizeRequest
from app.services.room_photo import RoomPhotoService, fit_exactly
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pydantic import ValidationError

from tests.unit.test_chat_api import FakeRetailers, FakeSessionStore
from tests.unit.test_room_visualization import (
    CONTEXT,
    SESSION,
    STORE,
    FakeImagesAPI,
    FakeRoomPhotos,
    a_jpeg,
    a_room_state,
    a_stored_session,
    a_turn_runtime,
    a_visualizer,
    an_openai_answer,
    decoded,
    gemini_with,
    openai_with,
    viz_settings,
)

PHOTO_ID = "a" * 32


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(data)) as image:
        return image.size


def a_room_photo(width: int = 1200, height: int = 900) -> RoomPhoto:
    return RoomPhoto(
        photo_id=PHOTO_ID,
        width=width,
        height=height,
        jpeg_b64=base64.b64encode(a_jpeg(width, height, "white")).decode("ascii"),
    )


# ── the image models keep the photo's shape ═════════════════════════════════


@pytest.mark.parametrize(
    ("width", "height", "openai", "gemini"),
    [
        (4032, 3024, "1536x1024", "4:3"),
        (3024, 4032, "1024x1536", "3:4"),
        (1920, 1080, "1536x1024", "16:9"),
        (1000, 1000, "1024x1024", "1:1"),
    ],
)
def test_each_provider_draws_on_its_canvas_nearest_the_photo(
    width: int, height: int, openai: str, gemini: str
) -> None:
    frame = Frame(width, height)
    assert nearest_shape(frame, ("1536x1024", "1024x1024", "1024x1536"), "x") == openai
    assert nearest_shape(frame, ("16:9", "3:2", "4:3", "1:1", "3:4", "2:3"), ":") == gemini


async def test_openai_edits_a_photo_on_the_canvas_nearest_its_shape() -> None:
    images = FakeImagesAPI(an_openai_answer(b"jpeg"))
    refs = [ImageReference(b"room", "Image 1: room")]

    await openai_with(images).generate("p", refs, Frame(900, 1200))

    assert images.calls[0][1]["size"] == "1024x1536"


async def test_gemini_edits_a_photo_in_its_shape_and_keeps_it_faithful() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        image = {"inlineData": {"mimeType": "image/jpeg", "data": "anBlZw=="}}
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [image]}}]})

    await gemini_with(answer).generate("p", [ImageReference(b"room", "x")], Frame(4032, 3024))

    config = json.loads(seen[0].content)["generationConfig"]
    assert config["imageConfig"] == {"aspectRatio": "4:3", "imageSize": "2K"}
    assert config["temperature"] == 0.15


def test_an_answer_is_scaled_to_the_photos_exact_size() -> None:
    fitted = fit_exactly(a_jpeg(1536, 1024), Frame(1200, 900), 90)

    assert size_of(fitted) == (1200, 900)


# ── the upload ══════════════════════════════════════════════════════════════


class FakeChecker:
    """The vision model: says whether a picture is a room."""

    def __init__(self, is_room: bool = True, error: Exception | None = None) -> None:
        self.is_room = is_room
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def parse_images(self, **kwargs: Any) -> RoomCheck:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return RoomCheck(is_room=self.is_room)


class FakeEmptier:
    def __init__(self, image: bytes | None = None, error: Exception | None = None) -> None:
        self.image = image if image is not None else a_jpeg(1536, 1024, "white")
        self.error = error
        self.calls: list[tuple[str, Sequence[ImageReference], Frame | None]] = []

    async def generate(
        self, prompt: str, references: Sequence[ImageReference], frame: Frame | None = None
    ) -> GeneratedImage:
        self.calls.append((prompt, references, frame))
        if self.error is not None:
            raise self.error
        return GeneratedImage(self.image, "image/jpeg", "gemini")


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int) -> None:
        self.values[key] = value
        self.ttls[key] = ex

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


def a_service(
    checker: FakeChecker | None = None, emptier: FakeEmptier | None = None
) -> tuple[RoomPhotoService, dict[str, Any]]:
    from app.core.config import SessionSettings

    parts: dict[str, Any] = {
        "checker": checker or FakeChecker(),
        "emptier": emptier or FakeEmptier(),
        "redis": MemoryRedis(),
    }
    store = RoomPhotoStore(parts["redis"], SessionSettings())  # type: ignore[arg-type]
    parts["store"] = store
    service = RoomPhotoService(
        parts["checker"],
        parts["emptier"],
        store,
        viz_settings(room_check_model="vision-model"),
    )
    return service, parts


async def test_a_room_is_checked_emptied_and_kept_at_its_own_size() -> None:
    service, parts = a_service()

    reply = await service.upload(a_jpeg(1200, 900), session_id=SESSION, context=CONTEXT)

    assert (reply.width, reply.height) == (1200, 900)
    assert len(parts["checker"].calls) == 1
    ((prompt, references, frame),) = parts["emptier"].calls
    assert prompt == EMPTY_ROOM_PROMPT
    assert len(references) == 1
    assert frame == Frame(1200, 900)
    kept = await parts["store"].load(STORE, SESSION, reply.room_photo_id)
    assert kept is not None
    # The model drew on its own canvas; the room is kept at the photo's size.
    assert size_of(base64.b64decode(kept.jpeg_b64)) == (1200, 900)


async def test_a_picture_that_is_not_a_room_is_refused_before_it_is_emptied() -> None:
    service, parts = a_service(checker=FakeChecker(is_room=False))

    with pytest.raises(NotARoomError) as refused:
        await service.upload(a_jpeg(1200, 900), session_id=SESSION, context=CONTEXT)

    assert refused.value.public_message == (
        "This doesn't look like a room - please upload a photo of the room."
    )
    assert parts["emptier"].calls == []
    assert parts["redis"].values == {}


async def test_a_check_that_cannot_be_made_is_not_a_no() -> None:
    service, parts = a_service(checker=FakeChecker(error=LLMUnavailableError(provider="x")))

    with pytest.raises(RoomPhotoUnavailableError):
        await service.upload(a_jpeg(1200, 900), session_id=SESSION, context=CONTEXT)

    assert parts["emptier"].calls == []


async def test_a_room_that_cannot_be_emptied_is_not_kept() -> None:
    service, parts = a_service(emptier=FakeEmptier(error=RenderUnavailableError(reason="x")))

    with pytest.raises(RoomPhotoUnavailableError):
        await service.upload(a_jpeg(1200, 900), session_id=SESSION, context=CONTEXT)

    assert parts["redis"].values == {}


async def test_an_unusable_file_is_refused_before_any_model_is_asked() -> None:
    service, parts = a_service()

    with pytest.raises(ImageRejectedError):
        await service.upload(b"not an image", session_id=SESSION, context=CONTEXT)

    assert parts["checker"].calls == []


async def test_a_new_photo_replaces_the_sessions_room() -> None:
    """One room per session: the replaced photo's id finds nothing."""
    service, parts = a_service()

    first = await service.upload(a_jpeg(1200, 900), session_id=SESSION, context=CONTEXT)
    second = await service.upload(a_jpeg(1000, 1000), session_id=SESSION, context=CONTEXT)

    assert list(parts["redis"].values) == [room_photo_key(STORE, SESSION)]
    assert await parts["store"].load(STORE, SESSION, first.room_photo_id) is None
    assert await parts["store"].load(STORE, SESSION, second.room_photo_id) is not None
    # Scoped like the session: another store or session finds nothing.
    assert await parts["store"].load(STORE + 1, SESSION, second.room_photo_id) is None
    assert await parts["store"].load(STORE, "another", second.room_photo_id) is None


# ── placing the pieces in it ════════════════════════════════════════════════


async def test_the_package_is_placed_in_the_room_at_its_exact_size() -> None:
    visualizer, parts = a_visualizer(max_references=3)

    render = await visualizer.render(
        a_room_state(), RenderView.CORNER, CONTEXT, a_room_photo(1200, 900)
    )

    (prompt,) = parts["generator"].prompts
    assert prompt.startswith("Image 1 is a photo of the customer's own room, empty.")
    (references,) = parts["generator"].references
    # The room first; the products follow, numbered from 2.
    assert references[0].data == base64.b64decode(a_room_photo(1200, 900).jpeg_b64)
    assert "image 2: Oak Bed" in prompt
    assert parts["generator"].frames == [Frame(1200, 900)]
    assert (render.width, render.height) == (1200, 900)
    assert size_of(decoded(render.image_url)) == (1200, 900)
    assert render.view is None and render.view_label == "Your room"
    assert render.room_photo_id == PHOTO_ID


async def test_the_room_takes_one_of_the_images_a_render_can_carry() -> None:
    visualizer, parts = a_visualizer(max_references=2)

    await visualizer.render(a_room_state(), RenderView.CORNER, CONTEXT, a_room_photo())

    (references,) = parts["generator"].references
    assert len(references) == 2, "the room and one product photo"


async def test_a_render_in_the_room_is_a_committed_turn_worded_for_it() -> None:
    sessions = FakeSessionStore()
    state = a_room_state()
    await a_stored_session(sessions, state)
    runtime, _ = a_turn_runtime(sessions, FakeRoomPhotos(a_room_photo()))

    reply = await runtime.visualize(
        VisualizeRequest(session_id=SESSION, store_id=STORE, room_photo_id=PHOTO_ID), CONTEXT
    )

    assert reply.response.message == "Here's your bedroom package, placed in your own room."
    saved = sessions.saved[(STORE, SESSION)]
    assert saved.state == state
    user, _ = saved.conversation.messages
    assert user.content == "[Asked to see the room package placed in a photo of their own room]"


async def test_a_room_photo_the_session_no_longer_holds_is_asked_for_again() -> None:
    sessions = FakeSessionStore()
    await a_stored_session(sessions, a_room_state())
    runtime, parts = a_turn_runtime(sessions, FakeRoomPhotos())

    with pytest.raises(RoomPhotoNotFoundError):
        await runtime.visualize(
            VisualizeRequest(session_id=SESSION, store_id=STORE, room_photo_id=PHOTO_ID), CONTEXT
        )

    assert parts["generator"].prompts == []


def test_a_catalogue_render_is_in_a_room_set_up_or_a_room_photo_never_both() -> None:
    items = [{"product_id": 1, "quantity": 1}]
    room = {"room_type": "bedroom", "style": "Modern", "length_m": 4, "width_m": 5}
    base = {"session_id": SESSION, "store_id": STORE, "items": items}

    assert CatalogVisualizeRequest.model_validate({**base, "room_photo_id": PHOTO_ID})
    assert CatalogVisualizeRequest.model_validate({**base, "room": room})
    for body in ({**base}, {**base, "room": room, "room_photo_id": PHOTO_ID}):
        with pytest.raises(ValidationError):
            CatalogVisualizeRequest.model_validate(body)


async def test_catalogue_picks_are_placed_in_the_room_with_no_room_set_up() -> None:
    from tests.unit.test_catalog import a_runtime, picks

    sessions = FakeSessionStore()
    await a_stored_session(sessions, a_room_state())
    runtime, parts, _ = a_runtime(sessions)
    runtime._room_photos = FakeRoomPhotos(a_room_photo())  # type: ignore[assignment]

    reply = await runtime.visualize(
        CatalogVisualizeRequest(
            session_id=SESSION, store_id=STORE, items=picks((1, 2)), room_photo_id=PHOTO_ID
        ),
        CONTEXT,
    )

    assert reply.response.message.startswith(
        "Here are the pieces you picked, placed in your own room."
    )
    assert parts["generator"].frames == [Frame(1200, 900)]
    assert reply.presentation is not None and reply.presentation.render is not None
    assert reply.presentation.render.room is None


# ── the route ═══════════════════════════════════════════════════════════════


def an_app(service: RoomPhotoService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(visualization_router, prefix="/v1")
    app.dependency_overrides[room_photo_service] = lambda: service
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()
    return app


async def _upload(service: RoomPhotoService, data: bytes) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=an_app(service)), base_url="http://t") as c:
        return await c.post(
            "/v1/room-photos",
            data={"session_id": SESSION, "store_id": str(STORE)},
            files={"image": ("room.jpg", data, "image/jpeg")},
        )


async def test_the_upload_answers_with_the_rooms_handle_never_the_picture() -> None:
    service, _ = a_service()

    reply = await _upload(service, a_jpeg(1200, 900))

    assert reply.status_code == 200
    assert set(reply.json()) == {"room_photo_id", "width", "height"}


async def test_the_upload_refuses_a_picture_that_is_not_a_room_in_our_words() -> None:
    service, _ = a_service(checker=FakeChecker(is_room=False))

    reply = await _upload(service, a_jpeg(1200, 900))

    assert reply.status_code == 422
    assert reply.json()["error"]["code"] == "not_a_room"


async def test_unconfigured_room_photos_are_refused_plainly() -> None:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(visualization_router, prefix="/v1")
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()

    class Unconfigured:
        settings = type("S", (), {"visualization": None})()
        room_checker = None
        empty_room_generator = None

    from app.api.dependencies import resources

    app.dependency_overrides[resources] = lambda: Unconfigured()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        reply = await c.post(
            "/v1/room-photos",
            data={"session_id": SESSION, "store_id": str(STORE)},
            files={"image": ("room.jpg", a_jpeg(), "image/jpeg")},
        )

    assert reply.status_code >= 500
    assert "not configured" in reply.json()["error"]["message"]


# ── the reply beside a render in their own room ─────────────────────────────


def test_a_render_in_their_own_room_names_no_view() -> None:
    from app.schemas.language import ReplyLanguage
    from app.services.media_wording import room_photo_reply

    package = room_photo_reply("living room", ReplyLanguage.EN)
    picked = room_photo_reply(None, ReplyLanguage.EN, dropped=2)

    assert package.message == "Here's your living room package, placed in your own room."
    assert picked.message == (
        "Here are the pieces you picked, placed in your own room."
        " 2 pieces you picked are no longer available, so I left them out."
    )


def test_a_render_in_their_own_room_is_said_in_arabic() -> None:
    import re

    from app.schemas.language import ReplyLanguage
    from app.services.media_wording import room_photo_reply

    for reply in (
        room_photo_reply("غرفة المعيشة", ReplyLanguage.AR),
        room_photo_reply(None, ReplyLanguage.AR, dropped=1),
    ):
        assert "غرفتك" in reply.message
        assert not re.search(r"[A-Za-z]", reply.message)
