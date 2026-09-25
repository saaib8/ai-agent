"""Room visualisation: the prompt, the image providers, storage, and the turn.

Faked is only what leaves the process: the image models, S3, the photo host,
PostgreSQL and Redis. The prompt, the fallback, the photo guard, the piece
assembly and the session commit are the real code.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from app.api.dependencies import (
    db_session,
    resources,
    retailer_context_provider,
    visualization_turn_runtime,
)
from app.api.errors import register_exception_handlers
from app.api.routes.visualization import router as visualization_router
from app.core.config import SessionSettings, VisualizationSettings
from app.core.exceptions import (
    NothingToVisualizeError,
    RenderUnavailableError,
    SessionConflictError,
)
from app.integrations.image_generation import (
    FallbackImageGenerator,
    GeminiImageGenerator,
    GeneratedImage,
    ImageReference,
    OpenAIImageGenerator,
)
from app.integrations.product_images import ProductImageFetcher, _is_public
from app.integrations.render_store import S3RenderStore
from app.prompts.visualization.v1 import RenderPiece, RenderRoom, build_prompt
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_state import (
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    RoomProjectState,
)
from app.schemas.dimensions import RawDimensions
from app.schemas.geometry import RoomGeometry, RoomMeasurement, RoomMeasurementRole
from app.schemas.product import CommerceClassification, ProductRow
from app.schemas.query import ConstraintStrength, SemanticPreference
from app.schemas.retailer import RetailerContext
from app.schemas.session import SessionEnvelope, new_session
from app.schemas.visualization import RenderView, VisualizeRequest
from app.services.room_visualization import RoomVisualizer, VisualizationTurnRuntime
from app.taxonomy.attributes import AttributeFamily
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pydantic import ValidationError

from tests.conftest import build_settings
from tests.unit.test_chat_api import FakeRetailers, FakeSessionStore

STORE = 50
SESSION = "sess-1"
CONTEXT = RetailerContext(store_id=STORE)


def viz_settings(**overrides: Any) -> VisualizationSettings:
    values: dict[str, Any] = {
        "openai_model": "image-model-a",
        "gemini_model": "image-model-b",
        "gemini_api_key": "google-test",
        "store_bucket": "renders-bucket",
        "store_region": "ap-south-1",
        "store_prefix": "ai-agent-renders/test",
        "public_base_url": "https://cdn.example.test/",
        "max_references": 3,
    }
    values.update(overrides)
    return VisualizationSettings(**values)


def a_jpeg(width: int = 300, height: int = 200, color: str = "tan") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


# ── settings ════════════════════════════════════════════════════════════════


class TestSettings:
    def test_the_primary_must_be_configured(self) -> None:
        with pytest.raises(ValidationError):
            viz_settings(primary="gemini", gemini_model=None, gemini_api_key=None)

    def test_gemini_needs_its_key(self) -> None:
        with pytest.raises(ValidationError):
            viz_settings(gemini_api_key=None)

    def test_the_public_base_must_be_https(self) -> None:
        with pytest.raises(ValidationError):
            viz_settings(public_base_url="http://cdn.example.test")

    def test_no_secret_reaches_the_startup_summary(self) -> None:
        settings = build_settings(
            visualization=viz_settings(gemini_api_key="AIza-real").model_dump(mode="json")
            | {"gemini_api_key": "AIza-real"}
        )
        summary = settings.redacted()
        assert summary["visualization_configured"] is True
        assert "AIza-real" not in json.dumps(summary)


# ── the prompt ══════════════════════════════════════════════════════════════


ROOM = RenderRoom(room_label="bedroom", style="modern", length_m=4.0, width_m=5.0)
PIECES = (
    RenderPiece(reference=1, name="Oak Bed", kind="bed", size_cm=(217.0, 140.0, 100.0), quantity=1),
    RenderPiece(reference=2, name="White Nightstand", kind="nightstand", size_cm=(), quantity=2),
    RenderPiece(reference=None, name="Wool Rug", kind="carpet", size_cm=(230.0, 330.0), quantity=1),
)


def test_every_view_has_a_camera_and_a_phrase() -> None:
    """A view added to the enum without its prompt wording would fail at the
    first customer's click; this fails at the first test run instead."""
    from app.prompts.visualization.v1 import VIEWS
    from app.services.room_visualization import _VIEW_PHRASES

    assert set(VIEWS) == set(RenderView)
    assert set(_VIEW_PHRASES) == set(RenderView)


class TestPrompt:
    def test_it_names_exactly_the_pieces_and_counts_the_units(self) -> None:
        prompt = build_prompt(ROOM, PIECES, RenderView.CORNER)
        assert prompt.startswith(
            "Create a single photorealistic render of a modern bedroom, "
            "approximately 4.0 m by 5.0 m."
        )
        assert "(4 pieces in total)" in prompt
        assert "- image 1: Oak Bed (bed, 217 x 140 x 100 cm)" in prompt
        assert "- image 2: White Nightstand (nightstand) - 2 of them" in prompt
        assert "- (no photo) Wool Rug (carpet, 230 x 330 cm): draw it faithfully" in prompt
        assert prompt.rstrip().endswith("nothing unlisted was added.")

    def test_the_photo_is_the_authority_on_appearance(self) -> None:
        """Catalog colours can be wrong; the text only identifies and sizes."""
        prompt = build_prompt(ROOM, PIECES, RenderView.CORNER)
        assert "The image is the authority on how a product looks" in prompt
        assert "no bedding" in prompt

    @pytest.mark.parametrize(
        ("view", "phrase"),
        [
            (RenderView.CORNER, "from one corner of the room"),
            (RenderView.EYE_LEVEL, "eye-level interior photograph"),
            (RenderView.ISOMETRIC, "isometric three-quarter aerial view"),
            (RenderView.TOP_DOWN, "strict top-down orthographic view"),
        ],
    )
    def test_each_view_has_its_own_camera(self, view: RenderView, phrase: str) -> None:
        assert phrase in build_prompt(ROOM, PIECES, view)

    def test_an_unknown_size_and_style_are_left_out_not_guessed(self) -> None:
        bare = RenderRoom(room_label="room", style=None, length_m=None, width_m=None)
        prompt = build_prompt(bare, PIECES[:1], RenderView.CORNER)
        assert prompt.startswith("Create a single photorealistic render of a room.")


# ── the image providers ═════════════════════════════════════════════════════


class FakeImagesAPI:
    def __init__(self, answer: Any = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def edit(self, **kwargs: Any) -> Any:
        self.calls.append(("edit", kwargs))
        if self.error:
            raise self.error
        return self.answer

    async def generate(self, **kwargs: Any) -> Any:
        self.calls.append(("generate", kwargs))
        if self.error:
            raise self.error
        return self.answer


def openai_with(images: FakeImagesAPI) -> OpenAIImageGenerator:
    return OpenAIImageGenerator(viz_settings(), api_key="k", client=SimpleNamespace(images=images))


def an_openai_answer(data: bytes) -> Any:
    return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(data).decode())])


REFS = (ImageReference(b"one", "Image 1: Oak Bed (bed)"), ImageReference(b"two", "Image 2: x"))


class TestOpenAIGenerator:
    async def test_references_go_up_as_numbered_files_in_prompt_order(self) -> None:
        images = FakeImagesAPI(an_openai_answer(b"jpeg"))
        result = await openai_with(images).generate("the prompt", REFS)

        assert result == GeneratedImage(data=b"jpeg", mime="image/jpeg", provider="openai")
        ((endpoint, call),) = images.calls
        assert endpoint == "edit"
        assert call["image"] == [
            ("ref1.jpg", b"one", "image/jpeg"),
            ("ref2.jpg", b"two", "image/jpeg"),
        ]
        assert call["model"] == "image-model-a"
        assert (call["quality"], call["size"], call["output_format"]) == (
            "medium",
            "1536x1024",
            "jpeg",
        )

    async def test_with_no_photos_it_generates_from_text(self) -> None:
        images = FakeImagesAPI(an_openai_answer(b"jpeg"))
        await openai_with(images).generate("the prompt", ())
        assert images.calls[0][0] == "generate"

    async def test_a_provider_error_is_ours(self) -> None:
        from openai import APIConnectionError

        error = APIConnectionError(request=httpx.Request("POST", "https://api.openai.test"))
        with pytest.raises(RenderUnavailableError):
            await openai_with(FakeImagesAPI(error=error)).generate("p", REFS)

    async def test_an_answer_with_no_image_is_a_failure(self) -> None:
        with pytest.raises(RenderUnavailableError):
            await openai_with(FakeImagesAPI(SimpleNamespace(data=[]))).generate("p", REFS)


def gemini_with(handler: Any) -> GeminiImageGenerator:
    return GeminiImageGenerator(viz_settings(), transport=httpx.MockTransport(handler))


def a_gemini_answer(data: bytes) -> dict[str, Any]:
    image = {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(data).decode()}}
    return {"candidates": [{"content": {"parts": [{"text": "here"}, image]}}]}


class TestGeminiGenerator:
    async def test_each_photo_travels_with_its_caption(self) -> None:
        seen: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=a_gemini_answer(b"jpeg"))

        result = await gemini_with(answer).generate("the prompt", REFS)

        assert result.data == b"jpeg" and result.provider == "gemini"
        request = seen[0]
        assert request.url.path.endswith("/models/image-model-b:generateContent")
        assert request.headers["x-goog-api-key"] == "google-test"
        body = json.loads(request.content)
        parts = body["contents"][0]["parts"]
        assert parts[0] == {"text": "the prompt"}
        assert parts[1] == {"text": "Image 1: Oak Bed (bed)"}
        assert base64.b64decode(parts[2]["inline_data"]["data"]) == b"one"
        assert body["generationConfig"] == {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "3:2", "imageSize": "2K"},
        }

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(429, text="quota"),
            httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "no"}]}}]}),
            httpx.Response(200, json={"candidates": []}),
        ],
    )
    async def test_no_image_is_a_failure(self, response: httpx.Response) -> None:
        with pytest.raises(RenderUnavailableError):
            await gemini_with(lambda _: response).generate("p", REFS)


class ScriptedGenerator:
    def __init__(self, outcome: GeneratedImage | Exception) -> None:
        self.outcome = outcome
        self.calls = 0

    async def generate(self, prompt: str, references: Sequence[ImageReference]) -> GeneratedImage:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class TestFallback:
    async def test_the_fallback_answers_when_the_primary_fails(self) -> None:
        primary = ScriptedGenerator(RenderUnavailableError(reason="x"))
        fallback = ScriptedGenerator(GeneratedImage(b"img", "image/jpeg", "gemini"))
        result = await FallbackImageGenerator(primary, fallback).generate("p", ())
        assert result.provider == "gemini"
        assert (primary.calls, fallback.calls) == (1, 1)

    async def test_the_fallback_is_not_asked_when_the_primary_answers(self) -> None:
        primary = ScriptedGenerator(GeneratedImage(b"img", "image/jpeg", "openai"))
        fallback = ScriptedGenerator(GeneratedImage(b"x", "image/jpeg", "gemini"))
        await FallbackImageGenerator(primary, fallback).generate("p", ())
        assert fallback.calls == 0

    async def test_without_a_fallback_the_failure_stands(self) -> None:
        primary = ScriptedGenerator(RenderUnavailableError(reason="x"))
        with pytest.raises(RenderUnavailableError):
            await FallbackImageGenerator(primary, None).generate("p", ())


# ── storage and photos ══════════════════════════════════════════════════════


class FakeS3:
    def __init__(self, error: Exception | None = None) -> None:
        self.puts: list[dict[str, Any]] = []
        self.error = error

    def put_object(self, **kwargs: Any) -> None:
        if self.error:
            raise self.error
        self.puts.append(kwargs)


class TestS3Store:
    async def test_it_writes_under_the_prefix_and_returns_the_public_url(self) -> None:
        s3 = FakeS3()
        url = await S3RenderStore(viz_settings(), client=s3).save(
            "store-50/x.jpg", b"img", "image/jpeg"
        )
        assert url == "https://cdn.example.test/ai-agent-renders/test/store-50/x.jpg"
        (put,) = s3.puts
        assert put["Bucket"] == "renders-bucket"
        assert put["Key"] == "ai-agent-renders/test/store-50/x.jpg"
        assert put["ContentType"] == "image/jpeg"
        assert "immutable" in put["CacheControl"]

    async def test_a_storage_failure_is_ours(self) -> None:
        store = S3RenderStore(viz_settings(), client=FakeS3(RuntimeError("AccessDenied: arn")))
        with pytest.raises(RenderUnavailableError) as raised:
            await store.save("k.jpg", b"img", "image/jpeg")
        assert "arn" not in raised.value.public_message


def a_fetcher(handler: Any, **overrides: Any) -> ProductImageFetcher:
    values: dict[str, Any] = {"timeout_s": 5, "max_bytes": 1_000_000, "resolve_public": False}
    values.update(overrides)
    return ProductImageFetcher(transport=httpx.MockTransport(handler), **values)


class TestProductImageFetcher:
    async def test_a_photo_comes_back_as_a_bounded_jpeg(self) -> None:
        big = a_jpeg(2400, 1600)
        data = await a_fetcher(lambda _: httpx.Response(200, content=big)).fetch(
            "https://shop.test/a.jpg"
        )
        assert data is not None
        assert max(Image.open(BytesIO(data)).size) == 1024

    @pytest.mark.parametrize("url", ["http://shop.test/a.jpg", "file:///etc/passwd", "ftp://x/y"])
    async def test_only_https_is_requested(self, url: str) -> None:
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(200, content=a_jpeg())

        assert await a_fetcher(handler).fetch(url) is None
        assert requested == []

    async def test_redirects_are_followed_and_rechecked(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/a.jpg":
                return httpx.Response(302, headers={"location": "http://inside.test/b.jpg"})
            return httpx.Response(200, content=a_jpeg())

        assert await a_fetcher(handler).fetch("https://shop.test/a.jpg") is None

    async def test_an_oversized_or_undecodable_body_is_refused(self) -> None:
        big = await a_fetcher(
            lambda _: httpx.Response(200, content=b"x" * 2000), max_bytes=1000
        ).fetch("https://shop.test/a.jpg")
        garbage = await a_fetcher(lambda _: httpx.Response(200, content=b"not an image")).fetch(
            "https://shop.test/a.jpg"
        )
        assert big is None and garbage is None

    @pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"])
    async def test_private_addresses_are_not_public(self, host: str) -> None:
        assert await _is_public(host) is False


# ── the visualizer ══════════════════════════════════════════════════════════


def a_row(product_id: int, *, name: str | None = None, image: str | None = None) -> ProductRow:
    return ProductRow(
        id=product_id,
        uuid=uuid4(),
        store_id=STORE,
        name_english=name or f"Piece {product_id}",
        name_arabic="قطعة",
        price_amount=Decimal("100"),
        price_unit="SAR",
        image_url=image or f"https://shop.test/{product_id}.jpg",
        product_url=f"https://shop.test/p/{product_id}",
        visual_category="bed",
        commerce=CommerceClassification(category="bedroom", subcategory="bed"),
        dimensions=RawDimensions(
            length=Decimal("200"), width=Decimal("150"), height=Decimal("90"), unit="cm"
        ),
        main_color="Beige",
        styles=("Modern",),
        is_active=True,
    )


class FakeCatalog:
    def __init__(self, rows: Sequence[ProductRow]) -> None:
        self.rows = {row.id: row for row in rows}

    async def get_by_ids(self, ids: Sequence[int], context: RetailerContext) -> list[ProductRow]:
        return [self.rows[i] for i in sorted(set(ids)) if i in self.rows]


class FakePhotos:
    def __init__(self, missing: set[str] | None = None) -> None:
        self.missing = missing or set()
        self.urls: list[str] = []

    async def fetch(self, url: str) -> bytes | None:
        self.urls.append(url)
        return None if url in self.missing else f"photo:{url}".encode()


class RecordingGenerator:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.references: list[Sequence[ImageReference]] = []

    async def generate(self, prompt: str, references: Sequence[ImageReference]) -> GeneratedImage:
        self.prompts.append(prompt)
        self.references.append(references)
        return GeneratedImage(a_jpeg(1536, 1024), "image/jpeg", "openai")


class RecordingStore:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def save(self, key: str, data: bytes, mime: str) -> str:
        self.keys.append(key)
        return f"https://cdn.example.test/{key}"


def a_room_state(lines: Sequence[tuple[int, int]] = ((10, 1), (11, 2), (10, 1))) -> AgentStateV1:
    return AgentStateV1(
        room_project=RoomProjectState(
            room_type="Bedroom",
            geometry=RoomGeometry(
                measurements=(
                    RoomMeasurement(
                        role=RoomMeasurementRole.ROOM_LENGTH, centimetres=Decimal("400")
                    ),
                    RoomMeasurement(
                        role=RoomMeasurementRole.ROOM_WIDTH, centimetres=Decimal("500")
                    ),
                )
            ),
            design_preferences=(
                SemanticPreference(
                    family=AttributeFamily.STYLE,
                    raw_value="modern",
                    canonical_value="Modern",
                    strength=ConstraintStrength.PREFERRED,
                ),
            ),
            bundle_items=tuple(
                BundleItemState(
                    line_id=n,
                    product_id=pid,
                    quantity=qty,
                    acquisition=BundleAcquisition.TO_BUY,
                    status=BundleItemStatus.SUGGESTED,
                )
                for n, (pid, qty) in enumerate(lines, start=1)
            ),
            next_bundle_line_id=len(lines) + 1,
        )
    )


def a_visualizer(
    rows: Sequence[ProductRow] = (), photos: FakePhotos | None = None, **settings: Any
) -> tuple[RoomVisualizer, dict[str, Any]]:
    parts: dict[str, Any] = {
        "catalog": FakeCatalog(rows or [a_row(10, name="Oak Bed"), a_row(11, name="Nightstand")]),
        "photos": photos or FakePhotos(),
        "generator": RecordingGenerator(),
        "store": RecordingStore(),
    }
    visualizer = RoomVisualizer(
        parts["catalog"],
        parts["photos"],
        parts["generator"],
        parts["store"],
        viz_settings(**settings),
    )
    return visualizer, parts


class TestVisualizer:
    async def test_the_session_package_is_what_gets_rendered(self) -> None:
        visualizer, parts = a_visualizer()
        render = await visualizer.render(a_room_state(), RenderView.CORNER, CONTEXT)

        (prompt,) = parts["generator"].prompts
        assert "modern bedroom, approximately 4.0 m by 5.0 m" in prompt
        assert "(4 pieces in total)" in prompt, "the bed's two lines are one piece of 2"
        assert "- image 1: Oak Bed (bed, 200 x 150 x 90 cm) - 2 of them" in prompt
        assert "- image 2: Nightstand (bed, 200 x 150 x 90 cm) - 2 of them" in prompt
        assert render.view is RenderView.CORNER and render.view_label == "Corner"
        assert (render.width, render.height) == (1536, 1024)
        assert [(i.name_english, i.quantity) for i in render.items] == [
            ("Oak Bed", 2),
            ("Nightstand", 2),
        ]

    async def test_the_public_key_names_the_store_and_nothing_about_the_customer(self) -> None:
        visualizer, parts = a_visualizer()
        render = await visualizer.render(a_room_state(), RenderView.CORNER, CONTEXT)
        (key,) = parts["store"].keys
        assert key.startswith("store-50/") and key.endswith(".jpg")
        assert SESSION not in key
        assert render.image_url.endswith(key)

    async def test_a_missing_photo_is_described_instead_and_numbering_stays_honest(self) -> None:
        photos = FakePhotos(missing={"https://shop.test/10.jpg"})
        visualizer, parts = a_visualizer(photos=photos)
        await visualizer.render(a_room_state(), RenderView.CORNER, CONTEXT)
        prompt = parts["generator"].prompts[0]
        assert "- (no photo) Oak Bed" in prompt
        assert "- image 1: Nightstand" in prompt
        (references,) = parts["generator"].references
        assert [r.caption for r in references] == ["Image 1: Nightstand (bed)"]

    async def test_photos_stop_at_the_reference_limit(self) -> None:
        rows = [a_row(i) for i in range(1, 6)]
        visualizer, parts = a_visualizer(rows=rows, max_references=2)
        await visualizer.render(
            a_room_state([(i, 1) for i in range(1, 6)]), RenderView.CORNER, CONTEXT
        )
        assert len(parts["photos"].urls) == 2
        assert parts["generator"].prompts[0].count("- (no photo)") == 3

    async def test_products_gone_from_the_catalog_are_left_out(self) -> None:
        visualizer, _ = a_visualizer(rows=[a_row(10, name="Oak Bed")])
        render = await visualizer.render(a_room_state(), RenderView.CORNER, CONTEXT)
        assert [i.name_english for i in render.items] == ["Oak Bed"]

    @pytest.mark.parametrize(
        "state",
        [AgentStateV1(), AgentStateV1(room_project=RoomProjectState(room_type="bedroom"))],
    )
    async def test_no_package_is_refused_before_anything_is_paid(self, state: AgentStateV1) -> None:
        visualizer, parts = a_visualizer()
        with pytest.raises(NothingToVisualizeError):
            await visualizer.render(state, RenderView.CORNER, CONTEXT)
        assert parts["generator"].prompts == []

    async def test_a_package_whose_pieces_are_all_gone_is_refused(self) -> None:
        visualizer, parts = a_visualizer(rows=[a_row(99)])
        with pytest.raises(NothingToVisualizeError):
            await visualizer.render(a_room_state(), RenderView.CORNER, CONTEXT)
        assert parts["generator"].prompts == []


# ── the turn ════════════════════════════════════════════════════════════════


async def a_stored_session(store: FakeSessionStore, state: AgentStateV1) -> None:
    fresh: SessionEnvelope = new_session()
    envelope = fresh.advanced(state=state, conversation=fresh.conversation)
    assert await store.save_if_revision(STORE, SESSION, expected_revision=0, envelope=envelope)


def a_turn_runtime(store: FakeSessionStore) -> tuple[VisualizationTurnRuntime, dict[str, Any]]:
    visualizer, parts = a_visualizer()
    return (
        VisualizationTurnRuntime(visualizer, store, SessionSettings(max_history_messages=6)),  # type: ignore[arg-type]
        parts,
    )


class TestTurn:
    async def test_a_render_is_a_committed_turn_that_changes_no_state(self) -> None:
        sessions = FakeSessionStore()
        state = a_room_state()
        await a_stored_session(sessions, state)
        runtime, _ = a_turn_runtime(sessions)

        reply = await runtime.visualize(
            VisualizeRequest(session_id=SESSION, store_id=STORE, view=RenderView.EYE_LEVEL), CONTEXT
        )

        assert reply.session_revision == 2
        assert reply.response.message == "Here's your bedroom, at eye level."
        assert reply.presentation is not None and reply.presentation.render is not None
        saved = sessions.saved[(STORE, SESSION)]
        assert saved.state == state, "a picture is not a fact about the room"
        user, assistant = saved.conversation.messages
        assert user.content == "[Asked to see the room package visualised, eye-level view]"
        assert assistant.content == "Here's your bedroom, at eye level."

    async def test_a_stale_screen_is_refused_before_an_image_is_paid_for(self) -> None:
        sessions = FakeSessionStore()
        await a_stored_session(sessions, a_room_state())
        runtime, parts = a_turn_runtime(sessions)
        with pytest.raises(SessionConflictError):
            await runtime.visualize(
                VisualizeRequest(session_id=SESSION, store_id=STORE, expected_session_revision=7),
                CONTEXT,
            )
        assert parts["generator"].prompts == []


# ── the route ═══════════════════════════════════════════════════════════════


def an_app(runtime: VisualizationTurnRuntime | None = None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(visualization_router, prefix="/v1")
    if runtime is not None:
        app.dependency_overrides[visualization_turn_runtime] = lambda: runtime
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()
    return app


class TestRoute:
    async def test_it_answers_like_a_chat_turn_with_a_render(self) -> None:
        sessions = FakeSessionStore()
        await a_stored_session(sessions, a_room_state())
        runtime, _ = a_turn_runtime(sessions)
        async with AsyncClient(
            transport=ASGITransport(app=an_app(runtime)), base_url="http://t"
        ) as client:
            reply = await client.post(
                "/v1/visualizations",
                json={"session_id": SESSION, "store_id": STORE, "view": "isometric"},
            )
        assert reply.status_code == 200
        render = reply.json()["presentation"]["render"]
        assert render["view"] == "isometric" and render["view_label"] == "Isometric"
        for forbidden in ("provider", "model", "prompt", "product_id"):
            assert f'"{forbidden}' not in reply.text, forbidden

    @pytest.mark.parametrize(
        "body",
        [
            {"session_id": SESSION, "store_id": STORE, "view": "birds_eye"},
            {"session_id": "a b", "store_id": STORE},
            {"session_id": SESSION, "store_id": STORE, "products": [1, 2]},
        ],
    )
    async def test_a_malformed_request_is_refused(self, body: dict[str, Any]) -> None:
        sessions = FakeSessionStore()
        runtime, parts = a_turn_runtime(sessions)
        async with AsyncClient(
            transport=ASGITransport(app=an_app(runtime)), base_url="http://t"
        ) as client:
            reply = await client.post("/v1/visualizations", json=body)
        assert reply.status_code == 422
        assert parts["generator"].prompts == []

    async def test_no_package_answers_with_our_words(self) -> None:
        runtime, _ = a_turn_runtime(FakeSessionStore())
        async with AsyncClient(
            transport=ASGITransport(app=an_app(runtime)), base_url="http://t"
        ) as client:
            reply = await client.post(
                "/v1/visualizations", json={"session_id": SESSION, "store_id": STORE}
            )
        assert reply.status_code == 409
        assert reply.json()["error"]["code"] == "nothing_to_visualize"

    async def test_it_refuses_cleanly_when_not_configured(self) -> None:
        class Unconfigured:
            settings = build_settings()
            render_generator = render_store = render_photos = None

        app = an_app()
        app.dependency_overrides[resources] = lambda: Unconfigured()
        app.dependency_overrides[db_session] = lambda: None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            reply = await client.post(
                "/v1/visualizations", json={"session_id": SESSION, "store_id": STORE}
            )
        assert reply.json()["error"]["message"] == "Room visualisation is not configured."
