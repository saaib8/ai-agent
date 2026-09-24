"""Furniture Finder: pixels, providers, the service, and a pick as a turn.

What is faked is only what leaves the process - the detector, the embedding
model, the image index, PostgreSQL and Redis. The imaging, the parsing, the
ordering, the state commit and the routes are the real code.

The claim that matters most is the last section's: a pick's products become
the list on screen, so the *next chat message* resolves "the second one"
against them. A finder whose results the conversation cannot address would be
a gallery bolted onto a chat, which is not what was built.
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
    chat_graph,
    chat_runtime,
    db_session,
    finder_turn_runtime,
    furniture_finder_service,
    resources,
    retailer_context_provider,
)
from app.api.errors import register_exception_handlers
from app.api.routes.chat import router as chat_router
from app.api.routes.furniture_finder import router as finder_router
from app.core.config import FurnitureFinderSettings, LLMSettings, SessionSettings
from app.core.exceptions import (
    DetectionUnavailableError,
    EmbeddingUnavailableError,
    FinderImageNotFoundError,
    ImageRejectedError,
    InvalidRequestError,
    LLMRequestError,
    LLMResponseInvalidError,
    LLMUnavailableError,
    SessionConflictError,
    VisualSearchUnavailableError,
)
from app.integrations.detection import ModalObjectDetector, RawDetection, parse_detections
from app.integrations.embeddings import OpenAIQueryEmbedder
from app.integrations.finder_index import (
    IndexMatch,
    PineconeFinderIndex,
    category_key,
    read_matches,
)
from app.integrations.llm import OpenAIStructuredClient
from app.schemas.dimensions import RawDimensions
from app.schemas.furniture_finder import (
    DetectedObject,
    FinderPhoto,
    FinderPickRequest,
    ImageBox,
    ObjectDescription,
)
from app.schemas.product import CommerceClassification, ProductRow
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.resolution import ResolvedProductReference
from app.schemas.retailer import RetailerContext
from app.services.finder_imaging import bounded_box, object_views, prepare_upload
from app.services.furniture_finder import FinderTurnRuntime, FurnitureFinderService
from app.services.hydration import ProductHydrationService
from app.services.object_description import ObjectDescriber
from app.services.reference_resolver import ProductReferenceResolver
from app.services.refinement_composer import SearchRefinementComposer
from app.services.similar_search import SimilarSearchBuilder
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image, ImageChops
from pydantic import SecretStr, ValidationError

from tests.conftest import build_settings
from tests.unit.test_chat_api import GRAPH, FakeRetailers, FakeSessionStore, Harness, a_selection

STORE, OTHER_STORE = 50, 51
SESSION = "sess-1"
CONTEXT = RetailerContext(store_id=STORE)
IMAGE_ID = "a" * 32

TAXONOMY = load_taxonomy()
ATTRIBUTES = load_catalog_attributes()
DIMENSIONS = load_dimension_semantics(taxonomy=TAXONOMY)


def finder_settings(**overrides: Any) -> FurnitureFinderSettings:
    values: dict[str, Any] = {
        "detector_url": "https://detector.test/segment",
        "detector_key": "wk-test",
        "detector_secret": "ws-test",
        "vision_model": "test-vision",
        "vision_reasoning_effort": "low",
        "embedding_model": "test-embedding",
        "embedding_dimensions": 4,
        "index_api_key": "pinecone-test",
        "index_name": "products-index",
        "index_namespace": "products",
        "result_limit": 3,
        "candidate_limit": 8,
        "min_image_side": 50,
        "detection_max_side": 400,
    }
    values.update(overrides)
    return FurnitureFinderSettings(**values)


def a_photo_file(
    width: int = 200, height: int = 120, color: str = "tan", fmt: str = "JPEG"
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format=fmt)
    return buffer.getvalue()


def a_detection(
    label: str, box: tuple[int, int, int, int], confidence: float = 0.9
) -> RawDetection:
    x1, y1, x2, y2 = box
    return RawDetection(
        label=label,
        confidence=confidence,
        box=box,
        polygon=((x1, y1), (x2 - 1, y1), (x2 - 1, y2 - 1), (x1, y2 - 1)),
    )


def rgb_at(image: Image.Image, x: int, y: int) -> tuple[int, ...]:
    pixel = image.convert("RGB").getpixel((x, y))
    assert isinstance(pixel, tuple)
    return pixel


# ── pixels ══════════════════════════════════════════════════════════════════


class TestPrepareUpload:
    def prepare(self, data: bytes, **overrides: Any) -> Any:
        values = {"max_bytes": 5_000_000, "min_side": 50, "max_side": 400}
        values.update(overrides)
        return prepare_upload(data, **values)

    def test_a_large_photo_is_downscaled_to_the_detection_size(self) -> None:
        prepared = self.prepare(a_photo_file(1000, 500))
        assert (prepared.width, prepared.height) == (400, 200)
        assert Image.open(BytesIO(prepared.jpeg)).size == (400, 200)

    def test_a_small_enough_photo_keeps_its_size(self) -> None:
        prepared = self.prepare(a_photo_file(300, 200))
        assert (prepared.width, prepared.height) == (300, 200)

    def test_exif_orientation_is_applied_before_anything_else(self) -> None:
        """A portrait phone photo is stored sideways with a rotation flag."""
        buffer = BytesIO()
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90° clockwise to display
        Image.new("RGB", (300, 100), "tan").save(buffer, format="JPEG", exif=exif)
        prepared = self.prepare(buffer.getvalue())
        assert (prepared.width, prepared.height) == (100, 300)

    def test_transparency_is_laid_over_white_not_black(self) -> None:
        buffer = BytesIO()
        Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(buffer, format="PNG")
        prepared = self.prepare(buffer.getvalue())
        assert min(rgb_at(Image.open(BytesIO(prepared.jpeg)), 50, 50)) > 240

    @pytest.mark.parametrize(
        ("data", "fragment"),
        [
            (b"", "empty"),
            (b"not an image at all", "JPEG, PNG or WebP"),
        ],
    )
    def test_unusable_files_are_refused_with_our_words(self, data: bytes, fragment: str) -> None:
        with pytest.raises(ImageRejectedError) as raised:
            self.prepare(data)
        assert fragment in raised.value.public_message

    def test_an_unaccepted_format_is_refused(self) -> None:
        with pytest.raises(ImageRejectedError):
            self.prepare(a_photo_file(100, 100, fmt="BMP"))

    def test_a_file_over_the_limit_is_refused_before_it_is_decoded(self) -> None:
        with pytest.raises(ImageRejectedError) as raised:
            self.prepare(a_photo_file(), max_bytes=100)
        assert "too large" in raised.value.public_message

    def test_a_tiny_photo_is_refused_with_the_minimum_named(self) -> None:
        with pytest.raises(ImageRejectedError) as raised:
            self.prepare(a_photo_file(40, 300))
        assert "50 pixels" in raised.value.public_message


class TestObjectViews:
    def test_the_tight_view_is_the_object_cut_out_on_white(self) -> None:
        photo = a_photo_file(200, 200, color="navy")
        triangle = ((50, 50), (149, 50), (50, 149))
        tight, _ = object_views(
            photo, box=ImageBox(x1=50, y1=50, x2=150, y2=150), polygon=triangle, padding=0.15
        )
        image = Image.open(BytesIO(tight)).convert("RGB")
        assert image.size == (100, 100)
        inside = rgb_at(image, 10, 10)
        outside = rgb_at(image, 95, 95)
        assert max(inside) < 150, "inside the outline keeps the photo's pixels"
        assert min(outside) > 240, "outside the outline is white"

    def test_the_medium_view_adds_context_and_stays_inside_the_photo(self) -> None:
        photo = a_photo_file(200, 200)
        square = ((50, 50), (149, 50), (149, 149), (50, 149))
        _, medium = object_views(
            photo, box=ImageBox(x1=50, y1=50, x2=150, y2=150), polygon=square, padding=0.15
        )
        assert Image.open(BytesIO(medium)).size == (130, 130), "15 px on each side"

        _, edge = object_views(
            photo, box=ImageBox(x1=0, y1=0, x2=100, y2=100), polygon=square, padding=0.5
        )
        assert Image.open(BytesIO(edge)).size == (150, 150), "clamped at the photo's edge"

    def test_an_outline_covering_nothing_leaves_the_crop_unmasked(self) -> None:
        """A solid white cut-out would match the plainest product photo."""
        photo = a_photo_file(200, 200, color="navy")
        elsewhere = ((0, 0), (5, 0), (0, 5))
        tight, _ = object_views(
            photo, box=ImageBox(x1=100, y1=100, x2=150, y2=150), polygon=elsewhere, padding=0
        )
        original = Image.open(BytesIO(photo)).convert("RGB").crop((100, 100, 150, 150))
        cropped = Image.open(BytesIO(tight)).convert("RGB")
        worst = max(ImageChops.difference(original, cropped).tobytes())
        assert worst < 40, "only JPEG noise differs"


def test_boxes_are_clamped_into_the_photo_or_dropped() -> None:
    assert bounded_box(-5, -5, 50, 60, 40, 40) == ImageBox(x1=0, y1=0, x2=40, y2=40)
    assert bounded_box(50, 50, 80, 80, 40, 40) is None
    assert bounded_box(10, 10, 10, 20, 40, 40) is None


# ── the detector ════════════════════════════════════════════════════════════


def modal_body(*objects: dict[str, Any], frame: list[int] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"detected_objects": list(objects)}
    if frame is not None:
        result["frame_size"] = frame
        result["original_dimensions"] = [200, 100]
    return {"status": "success", "result": result}


def modal_object(label: str = "sofa", **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "label": label,
        "confidence": 0.8,
        "mask_polygon": [[10, 10], [60, 10], [60, 40], [10, 40]],
        "bbox_from_mask": [10.0, 10.0, 60.0, 40.0],
    }
    value.update(overrides)
    return value


class TestParseDetections:
    def test_outlines_become_detections_with_a_box_around_the_outline(self) -> None:
        (sofa,) = parse_detections(modal_body(modal_object(" Sofa ")))
        assert sofa.label == "sofa"
        assert sofa.box == (10, 10, 61, 41)
        assert sofa.polygon[0] == (10, 10)

    @pytest.mark.parametrize(
        "broken",
        [
            modal_object(label=""),
            modal_object(confidence=1.5),
            modal_object(mask_polygon=[[1, 2], [3, 4]]),
            modal_object(mask_polygon=[[1, 2], [3], [5, 6]]),
            "not an object",
        ],
    )
    def test_one_bad_outline_does_not_cost_the_others(self, broken: Any) -> None:
        parsed = parse_detections(modal_body(broken, modal_object("chair")))
        assert [d.label for d in parsed] == ["chair"]

    @pytest.mark.parametrize(
        "body",
        [
            {"status": "error", "error": "GPU out of memory"},
            {"status": "success", "result": {"no": "objects"}},
            ["not", "a", "dict"],
        ],
    )
    def test_an_envelope_that_is_not_a_detection_is_a_failure(self, body: Any) -> None:
        with pytest.raises(DetectionUnavailableError):
            parse_detections(body)

    def test_coordinates_are_returned_in_the_sent_images_space(self) -> None:
        (sofa,) = parse_detections(modal_body(modal_object(), frame=[100, 50]))
        assert sofa.polygon[0] == (20, 20), "frame 100x50 scaled back to 200x100"


class TestModalDetector:
    async def test_sends_the_photo_with_proxy_auth_and_parses_the_answer(self) -> None:
        seen: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=modal_body(modal_object()))

        detector = ModalObjectDetector(finder_settings(), transport=httpx.MockTransport(answer))
        detections = await detector.detect(b"jpeg-bytes")
        await detector.close()

        assert [d.label for d in detections] == ["sofa"]
        request = seen[0]
        assert request.headers["Modal-Key"] == "wk-test"
        assert request.headers["Modal-Secret"] == "ws-test"
        sent = json.loads(request.content)
        assert base64.b64decode(sent["image"]) == b"jpeg-bytes"
        assert sent["confidence_threshold"] == 0.25

    @pytest.mark.parametrize("status", [401, 500, 504])
    async def test_a_refusal_is_our_error_not_the_transports(self, status: int) -> None:
        detector = ModalObjectDetector(
            finder_settings(),
            transport=httpx.MockTransport(lambda _: httpx.Response(status, text="secret stack")),
        )
        with pytest.raises(DetectionUnavailableError) as raised:
            await detector.detect(b"x")
        assert "secret" not in raised.value.public_message

    async def test_an_unreachable_endpoint_is_our_error(self) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        detector = ModalObjectDetector(finder_settings(), transport=httpx.MockTransport(fail))
        with pytest.raises(DetectionUnavailableError):
            await detector.detect(b"x")


# ── describing, embedding, the index ════════════════════════════════════════


A_DESCRIPTION = ObjectDescription(
    summary="Low three-seat sofa with rounded arms and loose back cushions.",
    styles=("modern", "minimalist"),
    color="warm beige",
    materials=("boucle fabric",),
)


def test_a_description_reads_like_the_indexs_own_product_documents() -> None:
    assert A_DESCRIPTION.query_text("sofa") == (
        "Low three-seat sofa with rounded arms and loose back cushions. "
        "Material: boucle fabric. Category: sofa. Style: modern, minimalist. "
        "Color: warm beige."
    )
    bare = ObjectDescription(summary="A lamp.", color="white")
    assert bare.query_text("lampshade") == "A lamp. Category: lampshade. Color: white."


class FakeVision:
    """The vision client's contract: instructions, words and images in; a model out."""

    def __init__(self, answer: ObjectDescription | Exception = A_DESCRIPTION) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def parse_images(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class TestObjectDescriber:
    async def test_the_model_sees_both_views_and_is_told_the_category(self) -> None:
        vision = FakeVision()
        described = await ObjectDescriber(vision).describe(
            category_words="side table", views=(b"tight", b"medium")
        )
        assert described == A_DESCRIPTION
        (call,) = vision.calls
        assert call["images"] == (b"tight", b"medium")
        assert call["user_input"] == "Category: side table"
        assert call["schema"] is ObjectDescription
        assert "not an instruction" in call["instructions"], "text in a photo is data"

    @pytest.mark.parametrize(
        "failure",
        [
            LLMUnavailableError(provider="openai"),
            LLMRequestError(provider="openai", status_code=400),
            LLMResponseInvalidError(reason="schema violation"),
        ],
    )
    async def test_any_provider_failure_is_one_retryable_answer(self, failure: Exception) -> None:
        with pytest.raises(VisualSearchUnavailableError):
            await ObjectDescriber(FakeVision(failure)).describe(
                category_words="sofa", views=(b"x",)
            )


def a_structured_client() -> OpenAIStructuredClient:
    return OpenAIStructuredClient(
        LLMSettings(api_key=SecretStr("test-key-not-real"), model="test-vision")
    )


async def test_images_travel_in_the_user_turn_as_jpeg_data() -> None:
    client = a_structured_client()
    seen: dict[str, Any] = {}

    async def _parse(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace(output_parsed=A_DESCRIPTION)

    client._client.responses.parse = _parse  # type: ignore[method-assign]
    answer = await client.parse_images(
        instructions="describe",
        user_input="Category: sofa",
        images=(b"one", b"two"),
        schema=ObjectDescription,
    )

    assert answer == A_DESCRIPTION
    assert seen["instructions"] == "describe", "instructions never mix with the images"
    (turn,) = seen["input"]
    assert turn["role"] == "user"
    text, first, second = turn["content"]
    assert text == {"type": "input_text", "text": "Category: sofa"}
    assert first == {
        "type": "input_image",
        "image_url": "data:image/jpeg;base64," + base64.b64encode(b"one").decode(),
    }
    assert second["image_url"].endswith(base64.b64encode(b"two").decode())


class TestReducedWidthEmbedding:
    def embedder(self, width: int) -> tuple[OpenAIQueryEmbedder, dict[str, Any]]:
        embedder = OpenAIQueryEmbedder(
            LLMSettings(api_key=SecretStr("test-key-not-real"), model="chat"),
            model="text-embedding-test",
            dimensions=4,
        )
        seen: dict[str, Any] = {}

        async def _create(**kwargs: Any) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.5] * width)])

        embedder._client.embeddings.create = _create  # type: ignore[method-assign]
        return embedder, seen

    async def test_the_width_is_requested_from_the_model(self) -> None:
        embedder, seen = self.embedder(4)
        assert await embedder.embed_query("a sofa") == (0.5, 0.5, 0.5, 0.5)
        assert seen["model"] == "text-embedding-test"
        assert seen["dimensions"] == 4

    async def test_an_answer_of_another_width_is_refused(self) -> None:
        embedder, _ = self.embedder(3072)
        with pytest.raises(EmbeddingUnavailableError):
            await embedder.embed_query("a sofa")


def test_the_semantic_ranking_embedder_still_asks_for_the_native_width() -> None:
    settings = LLMSettings(
        api_key=SecretStr("test-key-not-real"), model="chat", embedding_model="native-embedding"
    )
    embedder = OpenAIQueryEmbedder(settings)
    assert embedder.model == "native-embedding"
    assert embedder._dimensions is None


def test_index_matches_are_read_in_order_from_either_shape() -> None:
    as_dict = {
        "matches": [
            {"id": "v1", "score": 0.9, "metadata": {"product_url": "https://x/1"}},
            {"id": "v2", "score": 0.8, "metadata": {}},
        ]
    }

    class Match:
        def __init__(self, id: str, score: float, metadata: Any) -> None:  # noqa: A002
            self.id, self.score, self.metadata = id, score, metadata

    response = SimpleNamespace(matches=[Match("v3", 0.7, None)])

    assert read_matches(as_dict) == (
        IndexMatch("v1", 0.9, "https://x/1"),
        IndexMatch("v2", 0.8, None),
    )
    assert read_matches(response) == (IndexMatch("v3", 0.7, None),)


def test_categories_follow_the_catalogs_spelling() -> None:
    assert category_key(" Side Table ") == "side-table"
    assert category_key("2-seater-sofa") == "2-seater-sofa"


async def test_the_index_is_asked_within_one_store_and_one_category() -> None:
    queries: list[dict[str, Any]] = []

    def _query(**kwargs: Any) -> Any:
        queries.append(kwargs)
        return {"matches": [{"id": "prod-1", "score": 0.8, "metadata": {"product_url": "u"}}]}

    index = PineconeFinderIndex.__new__(PineconeFinderIndex)
    index._index = SimpleNamespace(query=_query)
    index._index_name, index._namespace = "products-index", "products"

    matches = await index.nearest(vector=(0.1, 0.2), store_id=50, category="sofa", top_k=7)

    assert matches == (IndexMatch("prod-1", 0.8, "u"),)
    (query,) = queries
    assert query["namespace"] == "products"
    assert query["filter"] == {"store_id": {"$eq": 50}, "category": {"$eq": "sofa"}}
    assert query["top_k"] == 7
    assert query["include_values"] is False


async def test_an_index_failure_is_ours() -> None:
    def _query(**_: Any) -> Any:
        raise RuntimeError("401 Unauthorized: pcsk_secret")

    index = PineconeFinderIndex.__new__(PineconeFinderIndex)
    index._index = SimpleNamespace(query=_query)
    index._index_name, index._namespace = "products-index", "products"

    with pytest.raises(VisualSearchUnavailableError) as raised:
        await index.nearest(vector=(0.1,), store_id=50, category="sofa", top_k=1)
    assert "pcsk" not in raised.value.public_message


# ── the service ═════════════════════════════════════════════════════════════


def a_row(
    product_id: int,
    *,
    store_id: int = STORE,
    subcategory: str | None = "sofa",
    category: str | None = "seating",
) -> ProductRow:
    return ProductRow(
        id=product_id,
        uuid=uuid4(),
        store_id=store_id,
        name_english=f"Sofa {product_id}",
        name_arabic="كنبة",
        price_amount=Decimal("1000"),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        visual_category="sofa",
        commerce=CommerceClassification(category=category, subcategory=subcategory),
        dimensions=RawDimensions(),
        main_color="Beige",
        styles=("Modern",),
        is_active=True,
    )


class FakeCatalog:
    """The repository's finder reads and `get_by_ids`, over an in-memory store."""

    def __init__(
        self,
        rows: Sequence[ProductRow] = (),
        *,
        categories: frozenset[str] = frozenset({"sofa", "side-table"}),
        vectors: dict[str, int] | None = None,
        urls: dict[str, int] | None = None,
    ) -> None:
        self.rows = {row.id: row for row in rows}
        self.categories = categories
        self.vectors = vectors or {}
        self.urls = urls or {}
        self.match_calls: list[tuple[list[str], list[str]]] = []

    async def visual_categories(self, context: RetailerContext) -> frozenset[str]:
        return self.categories

    async def ids_for_visual_matches(
        self, pinecone_ids: Sequence[str], product_urls: Sequence[str], context: RetailerContext
    ) -> tuple[dict[str, int], dict[str, int]]:
        self.match_calls.append((list(pinecone_ids), list(product_urls)))
        owned = {i for i, row in self.rows.items() if row.store_id == context.store_id}
        return (
            {k: v for k, v in self.vectors.items() if v in owned},
            {k: v for k, v in self.urls.items() if v in owned},
        )

    async def get_by_ids(
        self, product_ids: Sequence[int], context: RetailerContext
    ) -> list[ProductRow]:
        return [
            self.rows[i]
            for i in sorted(set(product_ids))
            if i in self.rows and self.rows[i].store_id == context.store_id
        ]


class FakeDetector:
    def __init__(self, detections: Sequence[RawDetection] = ()) -> None:
        self.detections = tuple(detections)
        self.calls = 0

    async def detect(self, image_jpeg: bytes) -> tuple[RawDetection, ...]:
        self.calls += 1
        Image.open(BytesIO(image_jpeg))  # it really is the prepared JPEG
        return self.detections


class FakeEmbedder:
    def __init__(self, error: Exception | None = None) -> None:
        self.texts: list[str] = []
        self.error = error

    @property
    def model(self) -> str:
        return "test-embedding"

    async def embed_query(self, text: str) -> tuple[float, ...]:
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        return (1.0, 0.0, 0.0, 0.0)


class FakeIndex:
    def __init__(self, matches: Sequence[IndexMatch] = ()) -> None:
        self.matches = tuple(matches)
        self.calls: list[dict[str, Any]] = []

    async def nearest(self, **kwargs: Any) -> tuple[IndexMatch, ...]:
        self.calls.append(kwargs)
        return self.matches


class FakePhotos:
    def __init__(self) -> None:
        self.saved: dict[tuple[int, str, str], FinderPhoto] = {}

    async def save(self, store_id: int, session_id: str, photo: FinderPhoto) -> None:
        self.saved[(store_id, session_id, photo.image_id)] = photo

    async def load(self, store_id: int, session_id: str, image_id: str) -> FinderPhoto | None:
        return self.saved.get((store_id, session_id, image_id))


def a_service(
    *,
    catalog: FakeCatalog | None = None,
    detector: FakeDetector | None = None,
    index: FakeIndex | None = None,
    photos: FakePhotos | None = None,
    vision: FakeVision | None = None,
    embedder: FakeEmbedder | None = None,
    **settings: Any,
) -> tuple[FurnitureFinderService, dict[str, Any]]:
    parts: dict[str, Any] = {
        "catalog": catalog or FakeCatalog(),
        "detector": detector or FakeDetector(),
        "vision": vision or FakeVision(),
        "embedder": embedder or FakeEmbedder(),
        "index": index or FakeIndex(),
        "photos": photos or FakePhotos(),
    }
    service = FurnitureFinderService(
        parts["detector"],
        ObjectDescriber(parts["vision"]),
        parts["embedder"],
        parts["index"],
        parts["photos"],
        parts["catalog"],
        ProductHydrationService(parts["catalog"]),
        finder_settings(**settings),
    )
    return service, parts


def a_stored_photo(
    photos: FakePhotos, *, store_id: int = STORE, session_id: str = SESSION
) -> FinderPhoto:
    photo = FinderPhoto(
        image_id=IMAGE_ID,
        width=200,
        height=120,
        jpeg_b64=base64.b64encode(a_photo_file()).decode(),
        objects=(
            DetectedObject(
                object_id=1,
                label="sofa",
                confidence=0.9,
                box=ImageBox(x1=20, y1=20, x2=180, y2=100),
                polygon=((20, 20), (179, 20), (179, 99), (20, 99)),
            ),
        ),
    )
    photos.saved[(store_id, session_id, IMAGE_ID)] = photo
    return photo


class TestUpload:
    async def test_only_objects_this_catalog_can_match_are_offered(self) -> None:
        service, parts = a_service(
            detector=FakeDetector(
                [
                    a_detection("plate", (5, 5, 20, 20)),
                    a_detection("side-table", (10, 10, 40, 40)),
                    a_detection("sofa", (40, 20, 190, 110)),
                ]
            )
        )
        reply = await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)

        assert [(o.object_id, o.label) for o in reply.objects] == [(1, "sofa"), (2, "side-table")]
        assert reply.objects[1].display_label == "side table"
        assert reply.unmatched_count == 1
        assert (reply.width, reply.height) == (200, 120)

        stored = parts["photos"].saved[(STORE, SESSION, reply.image_id)]
        assert [o.label for o in stored.objects] == ["sofa", "side-table"]
        assert Image.open(BytesIO(base64.b64decode(stored.jpeg_b64))).size == (200, 120)

    async def test_outlines_are_clamped_into_the_stored_photo(self) -> None:
        service, _ = a_service(detector=FakeDetector([a_detection("sofa", (-30, -10, 500, 90))]))
        reply = await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)
        (sofa,) = reply.objects
        assert sofa.box == ImageBox(x1=0, y1=0, x2=200, y2=90)
        assert all(0 <= x < 200 and 0 <= y < 120 for x, y in sofa.polygon)

    async def test_an_unusable_upload_is_refused_before_the_detector_is_paid(self) -> None:
        service, parts = a_service()
        with pytest.raises(ImageRejectedError):
            await service.upload(b"garbage", session_id=SESSION, context=CONTEXT)
        assert parts["detector"].calls == 0
        assert parts["photos"].saved == {}


class TestFind:
    async def test_neighbours_resolve_to_this_stores_products_in_index_order(self) -> None:
        catalog = FakeCatalog(
            [a_row(1), a_row(2), a_row(3), a_row(4), a_row(9, store_id=OTHER_STORE)],
            vectors={"v-a": 3, "v-b": 1, "v-other": 9},
            urls={"https://page/2": 2, "https://page/4": 4},
        )
        index = FakeIndex(
            [
                IndexMatch("v-a", 0.95, None),
                IndexMatch("v-other", 0.94, None),  # another retailer's: gone
                IndexMatch("prod-77", 0.93, "https://page/2"),  # joined by page
                IndexMatch("v-a-second-photo", 0.92, None),  # unresolvable: gone
                IndexMatch("v-b", 0.91, None),
                IndexMatch("v-a", 0.90, None),  # the same product again: once
                IndexMatch("v-late", 0.50, "https://page/4"),  # past the limit
            ]
        )
        service, parts = a_service(catalog=catalog, index=index)
        photo = a_stored_photo(parts["photos"])

        products, description = await service.find(photo, photo.objects[0], CONTEXT)

        assert [p.product_id for p in products] == [3, 2, 1]
        assert description == A_DESCRIPTION
        assert parts["index"].calls[0] == {
            "vector": (1.0, 0.0, 0.0, 0.0),
            "store_id": STORE,
            "category": "sofa",
            "top_k": 8,
        }

    async def test_the_object_is_described_from_its_crops_and_searched_as_words(self) -> None:
        service, parts = a_service()
        photo = a_stored_photo(parts["photos"])

        await service.find(photo, photo.objects[0], CONTEXT)

        (call,) = parts["vision"].calls
        tight, medium = call["images"]
        assert Image.open(BytesIO(tight)).size == (160, 80)
        assert Image.open(BytesIO(medium)).size == (200, 120)
        assert call["user_input"] == "Category: sofa"
        assert parts["embedder"].texts == [A_DESCRIPTION.query_text("sofa")]

    async def test_nothing_resolvable_is_an_empty_answer_not_a_failure(self) -> None:
        service, parts = a_service(index=FakeIndex([IndexMatch("v-x", 0.9, None)]))
        photo = a_stored_photo(parts["photos"])
        products, _ = await service.find(photo, photo.objects[0], CONTEXT)
        assert products == ()

    async def test_a_failed_description_searches_nothing(self) -> None:
        service, parts = a_service(vision=FakeVision(LLMUnavailableError(provider="openai")))
        photo = a_stored_photo(parts["photos"])
        with pytest.raises(VisualSearchUnavailableError):
            await service.find(photo, photo.objects[0], CONTEXT)
        assert parts["embedder"].texts == []
        assert parts["index"].calls == []

    async def test_a_failed_embedding_is_the_same_retryable_answer(self) -> None:
        service, parts = a_service(embedder=FakeEmbedder(EmbeddingUnavailableError(reason="x")))
        photo = a_stored_photo(parts["photos"])
        with pytest.raises(VisualSearchUnavailableError):
            await service.find(photo, photo.objects[0], CONTEXT)
        assert parts["index"].calls == []


# ── a pick, as a turn ═══════════════════════════════════════════════════════


def a_runtime(
    service: FurnitureFinderService, sessions: FakeSessionStore | None = None
) -> tuple[FinderTurnRuntime, FakeSessionStore]:
    store = sessions or FakeSessionStore()
    runtime = FinderTurnRuntime(
        service,
        store,  # type: ignore[arg-type]
        SessionSettings(max_history_messages=6),
        SimilarSearchBuilder(TAXONOMY, ATTRIBUTES),
        SearchRefinementComposer(ATTRIBUTES, DIMENSIONS),
    )
    return runtime, store


def a_pick(**overrides: Any) -> FinderPickRequest:
    values: dict[str, Any] = {
        "session_id": SESSION,
        "store_id": STORE,
        "image_id": IMAGE_ID,
        "object_id": 1,
    }
    values.update(overrides)
    return FinderPickRequest(**values)


def a_finding_service(
    rows: Sequence[ProductRow] | None = None,
) -> tuple[FurnitureFinderService, dict[str, Any]]:
    rows = rows if rows is not None else [a_row(31), a_row(32), a_row(33)]
    catalog = FakeCatalog(rows, vectors={f"v{row.id}": row.id for row in rows})
    index = FakeIndex([IndexMatch(f"v{row.id}", 0.9, None) for row in rows])
    service, parts = a_service(catalog=catalog, index=index)
    a_stored_photo(parts["photos"])
    return service, parts


class TestPick:
    async def test_the_matches_become_the_list_on_screen(self) -> None:
        service, _ = a_finding_service()
        runtime, sessions = a_runtime(service)

        reply = await runtime.pick(a_pick(), CONTEXT)

        assert reply.session_revision == 1
        assert reply.response.message == "Here are the 3 closest matches to the sofa in your photo."
        assert reply.presentation is not None
        assert [p.presented_ordinal for p in reply.presentation.products] == [1, 2, 3]
        assert [p.grounding_ref for p in reply.presentation.products] == [1, 2, 3]

        state = sessions.saved[(STORE, SESSION)].state
        assert state.product_interaction.presented_product_ids == (31, 32, 33)
        assert state.active_search is not None
        assert (
            state.product_interaction.presented_search_revision == state.active_search.revision == 1
        )

    async def test_the_search_left_behind_is_seeded_from_the_closest_match(self) -> None:
        """So "anything cheaper?" refines a real search - and one that has
        not quietly excluded the product at the top of the screen."""
        service, _ = a_finding_service([a_row(31, subcategory="sectional-sofa"), a_row(32)])
        runtime, sessions = a_runtime(service)

        await runtime.pick(a_pick(), CONTEXT)

        search = sessions.saved[(STORE, SESSION)].state.active_search
        assert search is not None
        assert search.request.commerce_category == "seating"
        assert search.request.commerce_subcategory == "sectional-sofa"
        assert search.request.exclude_product_ids == ()
        assert {p.canonical_value for p in search.semantic_preferences} >= {"Beige", "Modern"}

    async def test_an_unclassified_closest_match_seeds_from_the_next_one(self) -> None:
        service, _ = a_finding_service([a_row(31, category=None, subcategory=None), a_row(32)])
        runtime, sessions = a_runtime(service)

        await runtime.pick(a_pick(), CONTEXT)

        state = sessions.saved[(STORE, SESSION)].state
        assert state.product_interaction.presented_product_ids == (31, 32)

    async def test_with_nothing_to_seed_from_the_cards_show_but_are_not_addressable(self) -> None:
        service, _ = a_finding_service([a_row(31, category=None, subcategory=None)])
        runtime, sessions = a_runtime(service)

        reply = await runtime.pick(a_pick(), CONTEXT)

        assert reply.presentation is not None
        assert reply.presentation.products[0].presented_ordinal is None
        stored = sessions.saved[(STORE, SESSION)]
        assert stored.state.product_interaction.presented_product_ids == ()
        assert stored.session_revision == 1, "the exchange itself is still recorded"

    async def test_the_exchange_is_recorded_in_words(self) -> None:
        service, _ = a_finding_service()
        runtime, sessions = a_runtime(service)

        await runtime.pick(a_pick(), CONTEXT)

        messages = sessions.saved[(STORE, SESSION)].conversation.messages
        assert [m.role.value for m in messages] == ["user", "assistant"]
        assert "picked the sofa" in messages[0].content
        assert "rounded arms and loose back cushions" in messages[0].content
        assert messages[1].content.startswith("Here are the 3 closest matches")
        assert messages[1].content.endswith("hear more about one?"), "the offer is kept"

    async def test_no_matches_is_said_plainly_and_changes_no_results(self) -> None:
        service, _ = a_finding_service([])
        runtime, sessions = a_runtime(service)

        reply = await runtime.pick(a_pick(), CONTEXT)

        assert reply.presentation is None
        assert "couldn't find anything" in reply.response.message
        assert sessions.saved[(STORE, SESSION)].state.active_search is None

    async def test_a_stale_screen_is_refused_before_anything_is_paid_for(self) -> None:
        service, parts = a_finding_service()
        runtime, sessions = a_runtime(service)

        with pytest.raises(SessionConflictError):
            await runtime.pick(a_pick(expected_session_revision=3), CONTEXT)
        assert parts["vision"].calls == []
        assert sessions.saves == 0

    async def test_a_photo_from_another_session_or_store_is_not_found(self) -> None:
        service, _ = a_finding_service()
        runtime, _ = a_runtime(service)

        with pytest.raises(FinderImageNotFoundError):
            await runtime.pick(a_pick(session_id="someone-else"), CONTEXT)
        with pytest.raises(FinderImageNotFoundError):
            await runtime.pick(a_pick(store_id=OTHER_STORE), RetailerContext(store_id=OTHER_STORE))

    async def test_an_object_not_in_the_photo_is_refused(self) -> None:
        service, _ = a_finding_service()
        runtime, _ = a_runtime(service)
        with pytest.raises(InvalidRequestError):
            await runtime.pick(a_pick(object_id=7), CONTEXT)

    async def test_losing_the_race_returns_nothing(self) -> None:
        service, _ = a_finding_service()

        class Racing(FakeSessionStore):
            async def save_if_revision(self, *args: Any, **kwargs: Any) -> bool:
                return False

        runtime, _ = a_runtime(service, Racing())
        with pytest.raises(SessionConflictError):
            await runtime.pick(a_pick(), CONTEXT)


async def test_the_second_one_resolves_to_the_photos_second_match() -> None:
    """The real resolver, against the state a pick committed."""
    service, parts = a_finding_service()
    runtime, sessions = a_runtime(service)
    await runtime.pick(a_pick(), CONTEXT)

    resolver = ProductReferenceResolver(parts["catalog"], ATTRIBUTES)
    resolved = await resolver.resolve(
        PresentedOrdinal(position=2), sessions.saved[(STORE, SESSION)].state, CONTEXT
    )

    assert resolved == ResolvedProductReference(product_id=32)


# ── the routes ══════════════════════════════════════════════════════════════


def an_app(
    service: FurnitureFinderService | None = None,
    runtime: FinderTurnRuntime | None = None,
    *,
    harness: Harness | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(finder_router, prefix="/v1")
    app.include_router(chat_router, prefix="/v1")
    if service is not None:
        app.dependency_overrides[furniture_finder_service] = lambda: service
    if runtime is not None:
        app.dependency_overrides[finder_turn_runtime] = lambda: runtime
    if harness is not None:
        app.dependency_overrides[chat_runtime] = harness._runtime
        app.dependency_overrides[chat_graph] = lambda: GRAPH
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()
    return app


async def post_photo(client: AsyncClient, data: bytes, **form: Any) -> httpx.Response:
    fields = {"session_id": SESSION, "store_id": str(STORE), **form}
    return await client.post(
        "/v1/furniture-finder/photos",
        data=fields,
        files={"image": ("room.jpg", data, "image/jpeg")},
    )


class TestRoutes:
    async def test_an_upload_answers_with_the_pickable_objects(self) -> None:
        service, _ = a_service(detector=FakeDetector([a_detection("sofa", (40, 20, 190, 110))]))
        async with AsyncClient(
            transport=ASGITransport(app=an_app(service)), base_url="http://t"
        ) as client:
            reply = await post_photo(client, a_photo_file())

        assert reply.status_code == 200
        payload = reply.json()
        assert len(payload["image_id"]) == 32
        assert payload["objects"][0]["label"] == "sofa"
        assert payload["objects"][0]["display_label"] == "sofa"
        assert "jpeg_b64" not in reply.text, "the stored photo never goes back out"

    @pytest.mark.parametrize(
        "form",
        [{"session_id": "bad:id"}, {"store_id": "0"}, {"session_id": ""}],
    )
    async def test_a_malformed_form_is_a_validation_error(self, form: dict[str, str]) -> None:
        service, parts = a_service()
        async with AsyncClient(
            transport=ASGITransport(app=an_app(service)), base_url="http://t"
        ) as client:
            reply = await post_photo(client, a_photo_file(), **form)
        assert reply.status_code == 422
        assert parts["detector"].calls == 0

    async def test_a_bad_file_is_refused_with_a_message_a_customer_can_act_on(self) -> None:
        service, _ = a_service()
        async with AsyncClient(
            transport=ASGITransport(app=an_app(service)), base_url="http://t"
        ) as client:
            reply = await post_photo(client, b"%PDF-1.4 not a photo")
        assert reply.status_code == 422
        assert reply.json()["error"]["code"] == "image_rejected"

    async def test_an_unknown_store_is_refused_before_detection(self) -> None:
        service, parts = a_service()
        async with AsyncClient(
            transport=ASGITransport(app=an_app(service)), base_url="http://t"
        ) as client:
            reply = await post_photo(client, a_photo_file(), store_id="999")
        assert reply.status_code == 404
        assert parts["detector"].calls == 0

    async def test_the_finder_refuses_cleanly_when_not_configured(self) -> None:
        """No finder settings: the route answers, and says why, in our words."""

        class Unconfigured:
            settings = build_settings()
            detector = finder_vision = finder_embedder = finder_index = None

        app = an_app()
        app.dependency_overrides[resources] = lambda: Unconfigured()
        app.dependency_overrides[db_session] = lambda: None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            reply = await post_photo(client, a_photo_file())
        assert reply.json()["error"]["message"] == "Furniture Finder is not configured."

    async def test_a_pick_answers_like_a_chat_turn(self) -> None:
        service, _ = a_finding_service()
        runtime, _ = a_runtime(service)
        async with AsyncClient(
            transport=ASGITransport(app=an_app(runtime=runtime)), base_url="http://t"
        ) as client:
            reply = await client.post(
                "/v1/furniture-finder/picks", json=a_pick().model_dump(exclude_none=True)
            )
        assert reply.status_code == 200
        payload = reply.json()
        assert payload["session_revision"] == 1
        assert len(payload["presentation"]["products"]) == 3
        for forbidden in ("product_id", "store_id", "pinecone", "vector"):
            assert f'"{forbidden}' not in reply.text, forbidden

    @pytest.mark.parametrize(
        "bad",
        [{"image_id": "../../etc"}, {"object_id": 0}, {"session_id": "a b"}, {"extra": 1}],
    )
    async def test_a_malformed_pick_is_a_validation_error(self, bad: dict[str, Any]) -> None:
        service, parts = a_finding_service()
        runtime, _ = a_runtime(service)
        body = {**a_pick().model_dump(exclude_none=True), **bad}
        async with AsyncClient(
            transport=ASGITransport(app=an_app(runtime=runtime)), base_url="http://t"
        ) as client:
            reply = await client.post("/v1/furniture-finder/picks", json=body)
        assert reply.status_code == 422
        assert parts["vision"].calls == []


async def test_the_next_chat_message_sees_the_photos_matches() -> None:
    """Across HTTP requests: a pick, then "I like the second one" in chat.

    The chat turn is handed the state the pick committed - the photo's
    matches as the presented list - and the pick's words in its history.
    """
    sessions = FakeSessionStore()
    service, _ = a_finding_service()
    runtime, _ = a_runtime(service, sessions)
    harness = Harness(a_selection(position=2), store=sessions)
    app = an_app(runtime=runtime, harness=harness)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        pick = await client.post(
            "/v1/furniture-finder/picks", json=a_pick().model_dump(exclude_none=True)
        )
        chat = await client.post(
            "/v1/chat",
            json={
                "session_id": SESSION,
                "store_id": STORE,
                "message": "I like the second one",
                "expected_session_revision": pick.json()["session_revision"],
            },
        )

    assert chat.status_code == 200
    assert chat.json()["session_revision"] == 2
    selector, state = harness.parts[0]["references"].calls[0]
    assert selector == PresentedOrdinal(position=2)
    assert state.product_interaction.presented_product_ids == (31, 32, 33)
    turn = harness.responses.calls[0][0]
    assert "picked the sofa" in turn.conversation.messages[0].content


def test_settings_keep_the_candidate_pool_at_least_as_large_as_the_results() -> None:
    with pytest.raises(ValidationError):
        finder_settings(result_limit=10, candidate_limit=5)


def test_no_secret_is_rendered_in_the_startup_summary() -> None:
    settings = build_settings(
        furniture_finder=finder_settings().model_dump(mode="json")
        | {
            "detector_key": "wk-real",
            "detector_secret": "ws-real",
            "google_api_key": "AIza-real",
            "index_api_key": "pcsk-real",
        }
    )
    rendered = json.dumps(settings.redacted())
    assert settings.redacted()["furniture_finder_configured"] is True
    for secret in ("wk-real", "ws-real", "pcsk-real"):
        assert secret not in rendered
