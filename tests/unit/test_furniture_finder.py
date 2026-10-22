"""Furniture Finder: pixels, settings, and a finder that refuses while being rebuilt.

The old engine - the Modal detector, the vision description and the text
index - is gone. Until the main platform's finder is wired in, what can be
tested is the pixel work the new finder keeps, the settings, and that a photo
or a pick is refused in our own words rather than half-handled.

The other half of a pick - turning matches into a committed chat turn - did
not change, so it is still driven here by a stand-in finder that answers with
the rows it is given. Other suites reuse these helpers for the same reason.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Sequence
from decimal import Decimal
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from app.api.dependencies import (
    chat_graph,
    chat_runtime,
    click_tracking_service,
    db_session,
    finder_turn_runtime,
    furniture_finder_service,
    resources,
    retailer_context_provider,
)
from app.api.errors import register_exception_handlers
from app.api.routes.chat import router as chat_router
from app.api.routes.furniture_finder import router as finder_router
from app.core.config import FurnitureFinderSettings, SessionSettings
from app.core.exceptions import (
    DetectionUnavailableError,
    FinderAccessRefusedError,
    FinderImageNotFoundError,
    ImageRejectedError,
    InvalidRequestError,
    SessionConflictError,
    VisualSearchUnavailableError,
    ZoryTokenRejectedError,
)
from app.integrations.zory_finder import (
    RawDetection,
    ZoryFinderClient,
    ZoryMatch,
    ZoryScan,
    category_key,
    parse_detection_result,
)
from app.schemas.agent_state import AgentStateV1
from app.schemas.dimensions import RawDimensions
from app.schemas.furniture_finder import (
    DetectedObject,
    FinderPhoto,
    FinderPickRequest,
    ImageBox,
    ShopperCredentials,
)
from app.schemas.language import ReplyLanguage
from app.schemas.product import CommerceClassification, ProductRow
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.resolution import ResolvedProductReference
from app.schemas.retailer import RetailerContext, StoreRow
from app.schemas.session import new_session
from app.services.finder_imaging import bounded_box, prepare_upload
from app.services.furniture_finder import FinderTurnRuntime, FurnitureFinderService
from app.services.hydration import ProductHydrationService
from app.services.reference_resolver import ProductReferenceResolver
from app.services.refinement_composer import SearchRefinementComposer
from app.services.similar_search import SimilarSearchBuilder
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pydantic import SecretStr, ValidationError

from tests.conftest import build_settings
from tests.unit.click_ids import no_click_ids
from tests.unit.test_chat_api import GRAPH, FakeRetailers, FakeSessionStore, Harness, a_selection

STORE, OTHER_STORE = 50, 51
SESSION = "sess-1"
CONTEXT = RetailerContext(store_id=STORE)
IMAGE_ID = "a" * 32
STORE_UUID = UUID("527106f8-8bdf-4e69-8d1d-834045147283")
STORED_IMAGE = "furniture-finder-stage/20261014-user-96.png"

SHOPPER = ShopperCredentials(
    token=SecretStr("aaa.bbb.ccc"), widget_key="ai-agent", store_public_id=str(STORE_UUID)
)

TAXONOMY = load_taxonomy()
ATTRIBUTES = load_catalog_attributes()
DIMENSIONS = load_dimension_semantics(taxonomy=TAXONOMY)

__all__ = ["DIMENSIONS", "FakeSessionStore"]


def finder_settings(**overrides: Any) -> FurnitureFinderSettings:
    values: dict[str, Any] = {
        "zory_base_url": "https://zory.test",
        "stored_image_base_url": "https://images.test",
        "local_widget_key": "furniture-detection",
        "result_limit": 3,
        "candidate_limit": 8,
        "min_short_side": 100,
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


def test_boxes_are_clamped_into_the_photo_or_dropped() -> None:
    assert bounded_box(-5, -5, 50, 60, 40, 40) == ImageBox(x1=0, y1=0, x2=40, y2=40)
    assert bounded_box(50, 50, 80, 80, 40, 40) is None
    assert bounded_box(10, 10, 10, 20, 40, 40) is None


# ── the platform client, on the wire ════════════════════════════════════════


Handler = Callable[[httpx.Request], httpx.Response]


def a_client(handler: Handler) -> ZoryFinderClient:
    return ZoryFinderClient(finder_settings(), transport=httpx.MockTransport(handler))


async def sign_in(client: ZoryFinderClient) -> Any:
    return await client.anonymous_visitor(
        widget_key="furniture-detection",
        store_public_id=str(STORE_UUID),
        anonymous_id="ai-agent-0123456789abcdef",
    )


class TestSignIn:
    async def test_a_visitor_is_signed_in_as_the_widget_does(self) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200, json={"tokens": {"access": "v.t.k", "refresh": "r"}, "anonymous": True}
            )

        visitor = await sign_in(a_client(handle))

        assert str(seen[0].url) == "https://zory.test/api/widgets/auth/anonymous/"
        assert seen[0].headers["X-Zory-Widget-Key"] == "furniture-detection"
        assert seen[0].headers["X-Zory-Integration-Id"] == str(STORE_UUID)
        assert "Authorization" not in seen[0].headers, "no password, no token: it is a sign-in"
        assert json.loads(seen[0].content) == {"anonymous_id": "ai-agent-0123456789abcdef"}
        assert visitor.token.get_secret_value() == "v.t.k"
        assert visitor.widget_key == "furniture-detection"
        assert visitor.store_public_id == str(STORE_UUID)
        assert "v.t.k" not in repr(visitor), "the token never prints"

    async def test_an_answer_without_a_token_is_a_refusal(self) -> None:
        client = a_client(lambda _: httpx.Response(200, json={"anonymous": True}))
        with pytest.raises(FinderAccessRefusedError):
            await sign_in(client)

    @pytest.mark.parametrize(
        ("status", "body", "error"),
        [
            (401, {"code": "token_not_valid"}, ZoryTokenRejectedError),
            (403, {"code": "feature_not_entitled"}, FinderAccessRefusedError),
            (429, {"code": "visitor_daily_limit"}, FinderAccessRefusedError),
            (502, {}, DetectionUnavailableError),
        ],
    )
    async def test_a_refusal_becomes_our_error(
        self, status: int, body: dict[str, Any], error: type[Exception]
    ) -> None:
        client = a_client(lambda _: httpx.Response(status, json=body))
        with pytest.raises(error):
            await sign_in(client)

    async def test_an_unreachable_platform_is_our_error(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        with pytest.raises(DetectionUnavailableError):
            await sign_in(a_client(handle))


# ── the platform's detection, parsed ════════════════════════════════════════


def zory_result(
    *objects: dict[str, Any],
    frame: list[int] | None = None,
    original: list[int] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"detected_objects": list(objects)}
    if frame is not None:
        result["frame_size"] = frame
    if original is not None:
        result["original_dimensions"] = original
    return result


def zory_object(label: str = "sofa", **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "label": label,
        "confidence": 0.9,
        "mask_polygon": [[10, 10], [50, 10], [50, 40], [10, 40]],
        "bbox_from_mask": [10, 10, 50, 40],
    }
    item.update(overrides)
    return item


class TestParseDetectionResult:
    def test_outlines_become_detections_with_a_box_around_the_outline(self) -> None:
        detections, _ = parse_detection_result(zory_result(zory_object(" Side-Table ")))
        assert detections == (
            RawDetection(
                label="side-table",
                confidence=0.9,
                box=(10, 10, 51, 41),
                polygon=((10, 10), (50, 10), (50, 40), (10, 40)),
            ),
        )

    @pytest.mark.parametrize(
        "broken",
        [
            {"label": ""},
            {"confidence": 1.5},
            {"mask_polygon": [[1, 2], [3]], "bbox_from_mask": None},
            {"mask_polygon": [[1, 2], [3, 4]], "bbox_from_mask": None},
        ],
    )
    def test_one_bad_outline_does_not_cost_the_others(self, broken: dict[str, Any]) -> None:
        detections, _ = parse_detection_result(zory_result(zory_object(**broken), zory_object()))
        assert len(detections) == 1

    @pytest.mark.parametrize("result", [None, {}, {"detected_objects": "sofa"}, ["sofa"]])
    def test_a_result_that_is_not_a_detection_is_a_failure(self, result: Any) -> None:
        with pytest.raises(DetectionUnavailableError):
            parse_detection_result(result)

    def test_coordinates_are_in_the_stored_copys_space(self) -> None:
        detections, size = parse_detection_result(
            zory_result(zory_object(), frame=[100, 100], original=[200, 300])
        )
        assert detections[0].polygon[1] == (100, 30)
        assert size == (200, 300)

    def test_a_piece_with_no_outline_is_kept_by_its_box(self) -> None:
        """As the platform's own finder does, rather than losing the piece."""
        detections, _ = parse_detection_result(zory_result(zory_object(mask_polygon=None)))
        assert detections[0].polygon == ((10, 10), (50, 10), (50, 40), (10, 40))

    def test_the_size_falls_back_to_the_frame_and_then_to_nothing(self) -> None:
        assert parse_detection_result(zory_result(frame=[640, 480]))[1] == (640, 480)
        assert parse_detection_result(zory_result())[1] is None

    def test_labels_are_spelled_as_the_catalogs_categories(self) -> None:
        assert category_key("Side Table ") == "side-table"


# ── the scan, on the wire ═══════════════════════════════════════════════════


def scan_handler(seen: list[httpx.Request], **overrides: Any) -> Handler:
    """The platform's three finder calls, answered as stage answered them."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/widgets/uploads/":
            return httpx.Response(
                201, json={"key": "widget-uploads/s/visitor-9/abc.jpg", "url": "https://t/x"}
            )
        if path == "/api/process_furniture_finder/":
            return httpx.Response(
                200, json=overrides.get("process", {"uuid": 1626, "img_url": STORED_IMAGE})
            )
        if path == "/api/object-detection/1626/":
            return httpx.Response(
                200,
                json={"data": zory_result(zory_object(), frame=[200, 120], original=[200, 120])},
            )
        return httpx.Response(404)

    return handle


class TestScan:
    async def test_a_scan_uploads_detects_and_reads_back_as_the_shopper(self) -> None:
        seen: list[httpx.Request] = []
        client = a_client(scan_handler(seen))

        scan = await client.scan(SHOPPER, a_photo_file())

        assert [r.url.path for r in seen] == [
            "/api/widgets/uploads/",
            "/api/process_furniture_finder/",
            "/api/object-detection/1626/",
        ]
        for request in seen:
            assert request.headers["Authorization"] == "Bearer aaa.bbb.ccc"
            assert request.headers["X-Zory-Widget-Key"] == "ai-agent"
            assert request.headers["X-Zory-Widget-Store-Id"] == str(STORE_UUID)
        form = dict(httpx.QueryParams(seen[1].content.decode()))
        assert form == {
            "image_url": "widget-uploads/s/visitor-9/abc.jpg",
            "step_type": "ai_agent",
            "type": "furniture_finder",
        }, "an inner step of the ai-agent turn, on the shopper's own upload"
        assert not seen[2].url.query, "the dashboard's `permission` flag would refuse a widget"
        assert scan == ZoryScan(
            scan_id="1626",
            stored_image=STORED_IMAGE,
            detections=scan.detections,
            size=(200, 120),
        )
        assert [d.label for d in scan.detections] == ["sofa"]
        await client.close()

    @pytest.mark.parametrize("stored", ["https://evil.test/x.png", "../x.png", "", None])
    async def test_a_stored_copy_that_is_not_a_plain_key_is_refused(self, stored: Any) -> None:
        client = a_client(scan_handler([], process={"uuid": 1626, "img_url": stored}))
        with pytest.raises(DetectionUnavailableError):
            await client.scan(SHOPPER, a_photo_file())

    @pytest.mark.parametrize(
        ("status", "error"),
        [
            (401, ZoryTokenRejectedError),
            (403, FinderAccessRefusedError),
            (429, FinderAccessRefusedError),
            (502, DetectionUnavailableError),
        ],
    )
    async def test_a_refusal_becomes_our_error(self, status: int, error: type[Exception]) -> None:
        client = a_client(lambda _: httpx.Response(status, json={"code": "refused"}))
        with pytest.raises(error):
            await client.scan(SHOPPER, a_photo_file())

    async def test_an_unreachable_platform_is_our_error(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        with pytest.raises(DetectionUnavailableError):
            await a_client(handle).scan(SHOPPER, a_photo_file())


# ── recommendations, on the wire ════════════════════════════════════════════


async def recommend(client: ZoryFinderClient, shopper: ShopperCredentials | None) -> Any:
    return await client.recommend(
        shopper,
        image_url=f"https://images.test/{STORED_IMAGE}",
        category="sofa",
        box=ImageBox(x1=10, y1=10, x2=51, y2=41),
        polygon=((10, 10), (50, 10), (50, 40)),
        size=(200, 120),
        top_k=8,
        store_id=STORE,
    )


class TestRecommend:
    async def test_recommendations_are_asked_for_one_piece_in_one_store(self) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "recommendations": {
                        "matches": [
                            {"id": "x", "score": 0.95, "metadata": {"pinecone_id": "873009"}},
                            {"id": "112233", "score": 0.81, "metadata": {"product_url": "u"}},
                            {"score": "high"},
                        ]
                    },
                    "price_range": {},
                    "brands": {},
                },
            )

        matches = await recommend(a_client(handle), None)

        assert seen[0].url.path == "/api/get-recommendations/"
        assert json.loads(seen[0].content) == {
            "image_url": f"https://images.test/{STORED_IMAGE}",
            "category": "sofa",
            "top_k": 8,
            "bbox": [10, 10, 51, 41],
            "mask_polygon": [[10, 10], [50, 10], [50, 40]],
            "frame_size": [200, 120],
            "store_id": STORE,
        }
        assert "Authorization" not in seen[0].headers, "no session: the store says the scope"
        assert matches == (
            ZoryMatch(pinecone_id="873009", product_url=None, score=0.95),
            ZoryMatch(pinecone_id="112233", product_url="u", score=0.81),
        ), "the id comes from metadata, else the match's own id; a broken match is skipped"

    async def test_a_shopper_is_sent_along_when_there_is_one(self) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"recommendations": {"matches": []}})

        await recommend(a_client(handle), SHOPPER)

        assert seen[0].headers["Authorization"] == "Bearer aaa.bbb.ccc"
        assert seen[0].headers["X-Zory-Widget-Key"] == "ai-agent"

    async def test_a_category_the_platform_has_no_products_for_is_an_empty_answer(self) -> None:
        client = a_client(lambda _: httpx.Response(400, json={"error": "Category not supported."}))
        assert await recommend(client, SHOPPER) == ()

    @pytest.mark.parametrize(
        ("status", "body", "error"),
        [
            (400, {"error": "bbox is required"}, VisualSearchUnavailableError),
            (401, {"code": "token_not_valid"}, ZoryTokenRejectedError),
            (403, {"code": "feature_not_entitled"}, FinderAccessRefusedError),
            (502, {}, VisualSearchUnavailableError),
            (200, {"recommendations": "none"}, VisualSearchUnavailableError),
        ],
    )
    async def test_a_refusal_becomes_our_error(
        self, status: int, body: dict[str, Any], error: type[Exception]
    ) -> None:
        client = a_client(lambda _: httpx.Response(status, json=body))
        with pytest.raises(error):
            await recommend(client, SHOPPER)

    async def test_an_unreachable_platform_is_our_error(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        with pytest.raises(VisualSearchUnavailableError):
            await recommend(a_client(handle), SHOPPER)


# ── photo prep ══════════════════════════════════════════════════════════════


class TestEnlargingSmallPhotos:
    """The platform's finder refuses images under 500 pixels on either side."""

    def test_a_small_photo_is_enlarged_until_its_short_side_reaches_the_minimum(self) -> None:
        prepared = prepare_upload(
            a_photo_file(300, 200),
            max_bytes=5_000_000,
            min_side=None,
            max_side=1600,
            upscale_short_side_to=512,
        )
        assert (prepared.width, prepared.height) == (768, 512), "aspect kept"

    def test_enlarging_never_passes_the_longest_side_allowed(self) -> None:
        prepared = prepare_upload(
            a_photo_file(1500, 100),
            max_bytes=5_000_000,
            min_side=None,
            max_side=1600,
            upscale_short_side_to=512,
        )
        assert prepared.width == 1600

    def test_a_large_enough_photo_is_left_alone(self) -> None:
        prepared = prepare_upload(
            a_photo_file(900, 600),
            max_bytes=5_000_000,
            min_side=None,
            max_side=1600,
            upscale_short_side_to=512,
        )
        assert (prepared.width, prepared.height) == (900, 600)


# ── the upload ══════════════════════════════════════════════════════════════


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


def a_token(expires_in_s: int) -> str:
    claims = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + expires_in_s}).encode())
        .decode()
        .rstrip("=")
    )
    return f"eyJhbGciOiJIUzI1NiJ9.{claims}.signature"


class FakeZory:
    """The platform's finder: what it was asked, as whom, and what it answers."""

    def __init__(
        self,
        detections: Sequence[RawDetection] = (),
        *,
        size: tuple[int, int] | None = (200, 120),
        matches: Sequence[ZoryMatch] = (),
        scan_error: Exception | None = None,
        recommend_error: Exception | None = None,
        reject_first_scan: bool = False,
        visitor_access: str = "visitor.access.one",
    ) -> None:
        self.detections = tuple(detections)
        self.size = size
        self.matches = tuple(matches)
        self.scan_error = scan_error
        self.recommend_error = recommend_error
        self.reject_first_scan = reject_first_scan
        self.visitor_access = visitor_access
        self.scan_calls: list[tuple[ShopperCredentials, tuple[int, int]]] = []
        self.visitor_calls: list[dict[str, str]] = []
        self.recommend_calls: list[dict[str, Any]] = []

    async def anonymous_visitor(
        self, *, widget_key: str, store_public_id: str, anonymous_id: str
    ) -> ShopperCredentials:
        self.visitor_calls.append(
            {
                "widget_key": widget_key,
                "store_public_id": store_public_id,
                "anonymous_id": anonymous_id,
            }
        )
        return ShopperCredentials(
            token=SecretStr(self.visitor_access),
            widget_key=widget_key,
            store_public_id=store_public_id,
        )

    async def scan(self, shopper: ShopperCredentials, image_jpeg: bytes) -> ZoryScan:
        self.scan_calls.append((shopper, Image.open(BytesIO(image_jpeg)).size))
        if self.reject_first_scan and len(self.scan_calls) == 1:
            raise ZoryTokenRejectedError(reason="token_not_valid")
        if self.scan_error is not None:
            raise self.scan_error
        return ZoryScan(
            scan_id="1626", stored_image=STORED_IMAGE, detections=self.detections, size=self.size
        )

    async def recommend(
        self, shopper: ShopperCredentials | None, **kwargs: Any
    ) -> tuple[ZoryMatch, ...]:
        self.recommend_calls.append({"shopper": shopper, **kwargs})
        if self.recommend_error is not None:
            raise self.recommend_error
        return self.matches


class FakePhotos:
    def __init__(self) -> None:
        self.saved: dict[tuple[int, str, str], FinderPhoto] = {}
        self.visitors: dict[tuple[int, str], ShopperCredentials] = {}
        self.visitor_ttls: list[int] = []

    async def save(self, store_id: int, session_id: str, photo: FinderPhoto) -> None:
        self.saved[(store_id, session_id, photo.image_id)] = photo

    async def load(self, store_id: int, session_id: str, image_id: str) -> FinderPhoto | None:
        return self.saved.get((store_id, session_id, image_id))

    async def save_visitor(
        self, store_id: int, session_id: str, visitor: ShopperCredentials, ttl_s: int
    ) -> None:
        self.visitors[(store_id, session_id)] = visitor
        self.visitor_ttls.append(ttl_s)

    async def load_visitor(self, store_id: int, session_id: str) -> ShopperCredentials | None:
        return self.visitors.get((store_id, session_id))

    async def forget_visitor(self, store_id: int, session_id: str) -> None:
        self.visitors.pop((store_id, session_id), None)


class FakeStores:
    async def get_active(self, store_id: int) -> StoreRow | None:
        if store_id != STORE:
            return None
        return StoreRow(id=store_id, uuid=STORE_UUID, name_english="Cityw", active_status=True)


def a_service(
    *,
    catalog: FakeCatalog | None = None,
    zory: FakeZory | None = None,
    photos: FakePhotos | None = None,
    sessions: FakeSessionStore | None = None,
    local: bool = True,
    arabic_replies: bool = False,
    **settings: Any,
) -> tuple[FurnitureFinderService, dict[str, Any]]:
    parts: dict[str, Any] = {
        "catalog": catalog or FakeCatalog(),
        "zory": zory or FakeZory(),
        "photos": photos or FakePhotos(),
        "sessions": sessions or FakeSessionStore(),
    }
    service = FurnitureFinderService(
        parts["zory"],
        parts["photos"],
        parts["catalog"],
        ProductHydrationService(parts["catalog"]),
        FakeStores(),  # type: ignore[arg-type]
        parts["sessions"],
        finder_settings(**settings),
        local_visitors=local,
        arabic_replies=arabic_replies,
    )
    return service, parts


async def an_arabic_session(sessions: FakeSessionStore) -> None:
    fresh = new_session()
    state = AgentStateV1().model_copy(update={"reply_language": ReplyLanguage.AR})
    envelope = fresh.advanced(state=state, conversation=fresh.conversation)
    assert await sessions.save_if_revision(STORE, SESSION, expected_revision=0, envelope=envelope)


class TestUpload:
    async def test_the_platform_finders_clean_up_is_applied(self) -> None:
        """Unsure outlines dropped, one piece labelled twice kept once, the
        pieces people shop for first - the sofa, not the side table."""
        service, _ = a_service(
            zory=FakeZory(
                [
                    a_detection("side-table", (150, 80, 180, 110), confidence=0.95),
                    a_detection("sofa", (10, 10, 140, 100), confidence=0.6),
                    a_detection("3-seater-sofa", (12, 12, 140, 100), confidence=0.5),
                    a_detection("side-table", (5, 100, 20, 115), confidence=0.2),
                ]
            ),
            catalog=FakeCatalog(categories=frozenset({"sofa", "side-table", "3-seater-sofa"})),
        )

        reply = await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)

        assert [(o.object_id, o.label) for o in reply.objects] == [(1, "sofa"), (2, "side-table")]
        assert reply.unmatched_count == 0

    async def test_only_pieces_this_store_sells_are_offered_and_the_rest_counted(self) -> None:
        service, _ = a_service(
            zory=FakeZory(
                [
                    a_detection("sofa", (10, 10, 140, 100)),
                    a_detection("lampshade", (150, 5, 190, 50)),
                ]
            )
        )

        reply = await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)

        assert [o.label for o in reply.objects] == ["sofa"]
        assert reply.unmatched_count == 1

    async def test_only_so_many_pieces_are_offered(self) -> None:
        detections = [a_detection("sofa", (i * 30, 10, i * 30 + 25, 60)) for i in range(6)]
        service, _ = a_service(zory=FakeZory(detections), max_objects=4)

        reply = await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)

        assert len(reply.objects) == 4

    async def test_a_dropped_piece_is_not_counted_as_unmatched(self) -> None:
        """unmatched_count means "found, but this store sells nothing like it" -
        not "too unsure" and not "the same piece twice"."""
        service, _ = a_service(
            zory=FakeZory(
                [
                    a_detection("sofa", (10, 10, 140, 100), confidence=0.9),
                    a_detection("sofa", (11, 11, 140, 100), confidence=0.7),
                    a_detection("lampshade", (150, 5, 190, 50), confidence=0.1),
                ]
            )
        )

        reply = await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)

        assert [o.label for o in reply.objects] == ["sofa"]
        assert reply.unmatched_count == 0

    async def test_the_photo_is_kept_with_the_platforms_scan_and_its_copys_size(self) -> None:
        service, parts = a_service(
            zory=FakeZory([a_detection("sofa", (10, 10, 140, 100))], size=(180, 108))
        )

        reply = await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)

        assert (reply.width, reply.height) == (180, 108)
        photo = parts["photos"].saved[(STORE, SESSION, reply.image_id)]
        assert (photo.scan_id, photo.stored_image) == ("1626", STORED_IMAGE)
        assert "jpeg_b64" not in photo.model_dump(), "the photo stays with the platform"

    async def test_outlines_are_clamped_into_the_stored_copy(self) -> None:
        service, _ = a_service(zory=FakeZory([a_detection("sofa", (-5, 10, 300, 100))]))

        reply = await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)

        box = reply.objects[0].box
        assert (box.x1, box.x2) == (0, 200)

    async def test_a_forwarded_shopper_is_who_the_platform_scans_as(self) -> None:
        service, parts = a_service()

        await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT, shopper=SHOPPER)

        assert parts["zory"].scan_calls[0][0] is SHOPPER
        assert parts["zory"].visitor_calls == []
        assert parts["photos"].visitors == {}, "nothing of our own is signed in or saved"

    async def test_a_forwarded_shopper_is_never_retried_as_anyone_else(self) -> None:
        service, parts = a_service(zory=FakeZory(reject_first_scan=True))

        with pytest.raises(FinderAccessRefusedError):
            await service.upload(
                a_photo_file(), session_id=SESSION, context=CONTEXT, shopper=SHOPPER
            )
        assert len(parts["zory"].scan_calls) == 1
        assert parts["zory"].visitor_calls == []

    async def test_a_forwarded_shopper_works_where_no_visitor_of_our_own_may(self) -> None:
        """A deployment: not local, so no visitor of ours - but the shopper's own."""
        service, parts = a_service(local=False)

        await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT, shopper=SHOPPER)

        assert parts["zory"].scan_calls[0][0] is SHOPPER

    async def test_locally_one_anonymous_visitor_is_signed_in_per_session(self) -> None:
        service, parts = a_service()

        await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)
        await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)
        await service.upload(a_photo_file(), session_id="sess-2", context=CONTEXT)

        calls = parts["zory"].visitor_calls
        assert len(calls) == 2, "the session's visitor is kept, not signed in again"
        assert {c["widget_key"] for c in calls} == {"furniture-detection"}
        assert {c["store_public_id"] for c in calls} == {str(STORE_UUID)}
        assert calls[0]["anonymous_id"] != calls[1]["anonymous_id"]
        assert all(SESSION not in c["anonymous_id"] for c in calls), "no session id leaves"
        assert 16 <= len(calls[0]["anonymous_id"]) <= 100

    async def test_a_visitor_is_kept_only_until_its_token_expires(self) -> None:
        service, parts = a_service(zory=FakeZory(visitor_access=a_token(3600)))

        await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)

        assert 3500 <= parts["photos"].visitor_ttls[0] <= 3540

    async def test_an_expired_visitor_is_signed_in_again_once(self) -> None:
        service, parts = a_service(zory=FakeZory(reject_first_scan=True))

        await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)

        assert len(parts["zory"].visitor_calls) == 2
        assert len(parts["zory"].scan_calls) == 2

    async def test_without_a_shopper_a_deployment_refuses_before_asking_anything(self) -> None:
        service, parts = a_service(local=False)

        with pytest.raises(FinderAccessRefusedError) as raised:
            await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)
        assert raised.value.code == "finder_access_refused"
        assert parts["zory"].scan_calls == []
        assert parts["zory"].visitor_calls == []

    async def test_a_store_that_is_not_active_is_refused(self) -> None:
        service, parts = a_service()

        with pytest.raises(FinderAccessRefusedError):
            await service.upload(
                a_photo_file(), session_id=SESSION, context=RetailerContext(store_id=99)
            )
        assert parts["zory"].visitor_calls == []

    async def test_an_unusable_upload_is_refused_before_the_platform_is_asked(self) -> None:
        service, parts = a_service()
        with pytest.raises(ImageRejectedError):
            await service.upload(b"not a photo", session_id=SESSION, context=CONTEXT)
        assert parts["zory"].scan_calls == []

    async def test_a_refusal_reads_in_the_sessions_language(self) -> None:
        sessions = FakeSessionStore()
        await an_arabic_session(sessions)
        service, _ = a_service(
            zory=FakeZory(scan_error=FinderAccessRefusedError(reason="feature_not_entitled")),
            sessions=sessions,
            arabic_replies=True,
        )

        with pytest.raises(FinderAccessRefusedError) as raised:
            await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)
        assert raised.value.public_message == "البحث عن الأثاث من صورة غير متاح حاليًا."

    async def test_a_failed_scan_reads_in_the_sessions_language(self) -> None:
        sessions = FakeSessionStore()
        await an_arabic_session(sessions)
        service, _ = a_service(
            zory=FakeZory(scan_error=DetectionUnavailableError(reason="status_502")),
            sessions=sessions,
            arabic_replies=True,
        )

        with pytest.raises(DetectionUnavailableError) as raised:
            await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)
        assert raised.value.public_message.startswith("تعذّر تحليل")

    async def test_with_arabic_off_a_refusal_stays_english(self) -> None:
        sessions = FakeSessionStore()
        await an_arabic_session(sessions)
        service, _ = a_service(
            zory=FakeZory(scan_error=DetectionUnavailableError(reason="status_502")),
            sessions=sessions,
        )

        with pytest.raises(DetectionUnavailableError) as raised:
            await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)
        assert raised.value.public_message.startswith("We couldn't analyse")

    async def test_an_english_chat_keeps_english_refusals(self) -> None:
        service, _ = a_service(
            zory=FakeZory(scan_error=FinderAccessRefusedError(reason="feature_not_entitled")),
            arabic_replies=True,
        )

        with pytest.raises(FinderAccessRefusedError) as raised:
            await service.upload(a_photo_file(), session_id=SESSION, context=CONTEXT)
        assert raised.value.public_message.startswith("Finding furniture")

    async def test_a_small_photo_is_enlarged_before_it_is_sent(self) -> None:
        service, parts = a_service(min_short_side=240)

        await service.upload(a_photo_file(200, 120), session_id=SESSION, context=CONTEXT)

        assert parts["zory"].scan_calls[0][1] == (400, 240)


# ── finding products like a piece ═══════════════════════════════════════════


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


def a_stored_photo(
    photos: FakePhotos, *, store_id: int = STORE, session_id: str = SESSION
) -> FinderPhoto:
    photo = FinderPhoto(
        image_id=IMAGE_ID,
        width=200,
        height=120,
        scan_id="1626",
        stored_image=STORED_IMAGE,
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


class TestFind:
    async def test_matches_resolve_to_this_stores_products_in_the_platforms_order(self) -> None:
        catalog = FakeCatalog(
            [a_row(31), a_row(32), a_row(33)], vectors={"v31": 31, "v32": 32, "v33": 33}
        )
        zory = FakeZory(
            matches=[
                ZoryMatch("v33", None, 0.95),
                ZoryMatch("v31", None, 0.9),
                ZoryMatch("unknown", None, 0.88),
                ZoryMatch("v31", None, 0.7),
            ]
        )
        service, parts = a_service(catalog=catalog, zory=zory)
        photo = a_stored_photo(parts["photos"])

        products = await service.find(photo, photo.objects[0], CONTEXT)

        assert [p.product_id for p in products] == [33, 31], "Zory's order, each once"
        assert zory.recommend_calls[0] == {
            "shopper": None,
            "image_url": f"https://images.test/{STORED_IMAGE}",
            "category": "sofa",
            "box": photo.objects[0].box,
            "polygon": photo.objects[0].polygon,
            "size": (200, 120),
            "top_k": 8,
            "store_id": STORE,
        }

    async def test_a_match_is_found_by_its_page_when_its_id_is_not_known(self) -> None:
        catalog = FakeCatalog([a_row(31)], urls={"https://example.test/31": 31})
        zory = FakeZory(matches=[ZoryMatch("other-index-id", "https://example.test/31", 0.9)])
        service, parts = a_service(catalog=catalog, zory=zory)
        photo = a_stored_photo(parts["photos"])

        products = await service.find(photo, photo.objects[0], CONTEXT)

        assert [p.product_id for p in products] == [31]

    async def test_results_are_capped_after_the_store_decides(self) -> None:
        rows = [a_row(31 + i) for i in range(5)]
        catalog = FakeCatalog(rows, vectors={f"v{r.id}": r.id for r in rows})
        zory = FakeZory(matches=[ZoryMatch(f"v{r.id}", None, 0.9) for r in rows])
        service, parts = a_service(catalog=catalog, zory=zory, result_limit=2)
        photo = a_stored_photo(parts["photos"])

        products = await service.find(photo, photo.objects[0], CONTEXT)

        assert [p.product_id for p in products] == [31, 32]

    async def test_another_stores_product_is_never_shown(self) -> None:
        catalog = FakeCatalog(
            [a_row(31), a_row(90, store_id=OTHER_STORE)], vectors={"v31": 31, "v90": 90}
        )
        zory = FakeZory(matches=[ZoryMatch("v90", None, 0.99), ZoryMatch("v31", None, 0.9)])
        service, parts = a_service(catalog=catalog, zory=zory)
        photo = a_stored_photo(parts["photos"])

        products = await service.find(photo, photo.objects[0], CONTEXT)

        assert [p.product_id for p in products] == [31]

    async def test_the_shopper_is_passed_on(self) -> None:
        service, parts = a_service()
        photo = a_stored_photo(parts["photos"])

        await service.find(photo, photo.objects[0], CONTEXT, SHOPPER)

        assert parts["zory"].recommend_calls[0]["shopper"] is SHOPPER

    async def test_nothing_proposed_is_an_empty_answer_not_a_failure(self) -> None:
        service, parts = a_service()
        photo = a_stored_photo(parts["photos"])
        assert await service.find(photo, photo.objects[0], CONTEXT) == ()


# ── a pick, as a turn ═══════════════════════════════════════════════════════


def a_finding_service(
    rows: Sequence[ProductRow] | None = None,
) -> tuple[FurnitureFinderService, dict[str, Any]]:
    rows = rows if rows is not None else [a_row(31), a_row(32), a_row(33)]
    catalog = FakeCatalog(rows, vectors={f"v{row.id}": row.id for row in rows})
    zory = FakeZory(matches=[ZoryMatch(f"v{row.id}", None, 0.9) for row in rows])
    service, parts = a_service(catalog=catalog, zory=zory)
    a_stored_photo(parts["photos"])
    return service, parts


def a_runtime(
    service: FurnitureFinderService,
    sessions: FakeSessionStore | None = None,
    *,
    arabic_replies: bool = False,
) -> tuple[FinderTurnRuntime, FakeSessionStore]:
    store = sessions or FakeSessionStore()
    runtime = FinderTurnRuntime(
        service,
        store,  # type: ignore[arg-type]
        SessionSettings(max_history_messages=6),
        SimilarSearchBuilder(TAXONOMY, ATTRIBUTES),
        SearchRefinementComposer(ATTRIBUTES, DIMENSIONS),
        arabic_replies=arabic_replies,
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
        assert messages[0].content == (
            "[Shared a photo and picked the sofa in it] Show me products like this sofa."
        )
        assert messages[1].content.startswith("Here are the 3 closest matches")
        assert messages[1].content.endswith("hear more about one?"), "the offer is kept"

    async def test_no_matches_is_said_plainly_and_changes_no_results(self) -> None:
        service, _ = a_finding_service([])
        runtime, sessions = a_runtime(service)

        reply = await runtime.pick(a_pick(), CONTEXT)

        assert reply.presentation is not None
        assert reply.presentation.products == ()
        assert [choice.label for choice in reply.presentation.choices] == ["Yes", "No"]
        assert "couldn't find anything" in reply.response.message
        assert sessions.saved[(STORE, SESSION)].state.active_search is None

    async def test_a_stale_screen_is_refused_before_the_platform_is_asked(self) -> None:
        service, parts = a_finding_service()
        runtime, sessions = a_runtime(service)

        with pytest.raises(SessionConflictError):
            await runtime.pick(a_pick(expected_session_revision=3), CONTEXT)
        assert parts["zory"].recommend_calls == []
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

    async def test_the_pick_runs_as_the_forwarded_shopper(self) -> None:
        service, parts = a_finding_service()
        runtime, _ = a_runtime(service)

        await runtime.pick(a_pick(), CONTEXT, SHOPPER)

        assert parts["zory"].recommend_calls[0]["shopper"] is SHOPPER

    async def test_a_failed_search_reads_in_the_sessions_language(self) -> None:
        sessions = FakeSessionStore()
        await an_arabic_session(sessions)
        service, parts = a_finding_service()
        parts["zory"].recommend_error = VisualSearchUnavailableError(reason="status_502")
        runtime, _ = a_runtime(service, sessions, arabic_replies=True)

        with pytest.raises(VisualSearchUnavailableError) as raised:
            await runtime.pick(a_pick(), CONTEXT)
        assert raised.value.public_message.startswith("تعذّر البحث")


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
    app.dependency_overrides[click_tracking_service] = no_click_ids
    return app


async def post_photo(
    client: AsyncClient, data: bytes, headers: dict[str, str] | None = None, **form: Any
) -> httpx.Response:
    fields = {"session_id": SESSION, "store_id": str(STORE), **form}
    return await client.post(
        "/v1/furniture-finder/photos",
        data=fields,
        files={"image": ("room.jpg", data, "image/jpeg")},
        headers=headers,
    )


WIDGET_HEADERS = {
    "X-Zory-Widget-Token": "aaa.bbb.ccc",
    "X-Zory-Widget-Key": "ai-agent",
    "X-Zory-Widget-Store-Id": str(STORE_UUID),
}


class TestRoutes:
    async def test_an_upload_answers_with_the_pickable_objects(self) -> None:
        service, _ = a_service(zory=FakeZory([a_detection("sofa", (40, 20, 190, 110))]))
        async with AsyncClient(
            transport=ASGITransport(app=an_app(service)), base_url="http://t"
        ) as client:
            reply = await post_photo(client, a_photo_file())

        assert reply.status_code == 200
        payload = reply.json()
        assert len(payload["image_id"]) == 32
        assert payload["objects"][0]["label"] == "sofa"
        assert payload["objects"][0]["display_label"] == "sofa"
        for kept in ("stored_image", "scan_id", STORED_IMAGE, "jpeg_b64"):
            assert kept not in reply.text, "what the platform keeps never goes back out"

    async def test_the_shoppers_widget_session_is_passed_on(self) -> None:
        service, parts = a_finding_service()
        runtime, _ = a_runtime(service)
        app = an_app(service, runtime)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            await post_photo(client, a_photo_file(), headers=WIDGET_HEADERS)
            await client.post(
                "/v1/furniture-finder/picks",
                json=a_pick().model_dump(exclude_none=True),
                headers=WIDGET_HEADERS,
            )

        scanned_as = parts["zory"].scan_calls[0][0]
        assert scanned_as.token.get_secret_value() == "aaa.bbb.ccc"
        assert (scanned_as.widget_key, scanned_as.store_public_id) == ("ai-agent", str(STORE_UUID))
        assert parts["zory"].recommend_calls[0]["shopper"] == scanned_as
        assert parts["zory"].visitor_calls == []

    async def test_without_the_headers_a_local_run_uses_its_own_visitor(self) -> None:
        service, parts = a_service()
        async with AsyncClient(
            transport=ASGITransport(app=an_app(service)), base_url="http://t"
        ) as client:
            await post_photo(client, a_photo_file())

        assert len(parts["zory"].visitor_calls) == 1
        assert parts["zory"].scan_calls[0][0].widget_key == "furniture-detection"

    @pytest.mark.parametrize(
        "headers",
        [
            {"X-Zory-Widget-Token": "not a token"},
            {"X-Zory-Widget-Token": "a.b.c", "X-Zory-Widget-Key": "Bad Key"},
            {"X-Zory-Widget-Token": "a.b.c", "X-Zory-Widget-Store-Id": "../x"},
        ],
    )
    async def test_a_malformed_widget_header_is_refused_before_anything_runs(
        self, headers: dict[str, str]
    ) -> None:
        service, parts = a_service()
        async with AsyncClient(
            transport=ASGITransport(app=an_app(service)), base_url="http://t"
        ) as client:
            reply = await post_photo(client, a_photo_file(), headers=headers)
        assert reply.status_code == 422
        assert parts["zory"].scan_calls == []

    async def test_without_a_shopper_a_deployment_says_so_in_our_words(self) -> None:
        service, _ = a_service(local=False)
        async with AsyncClient(
            transport=ASGITransport(app=an_app(service)), base_url="http://t"
        ) as client:
            reply = await post_photo(client, a_photo_file())
        assert reply.status_code == 403
        assert reply.json()["error"] == {
            "code": "finder_access_refused",
            "message": "Finding furniture from a photo isn't available right now.",
            "trace_id": None,
        }

    @pytest.mark.parametrize(
        "form",
        [{"session_id": "bad:id"}, {"store_id": "0"}, {"session_id": ""}],
    )
    async def test_a_malformed_form_is_a_validation_error(self, form: dict[str, str]) -> None:
        service, parts = a_service()
        async with AsyncClient(
            transport=ASGITransport(app=an_app(service)), base_url="http://t"
        ) as client:
            reply = await post_photo(client, a_photo_file(), None, **form)
        assert reply.status_code == 422
        assert parts["zory"].scan_calls == []

    async def test_a_bad_file_is_refused_with_a_message_a_customer_can_act_on(self) -> None:
        service, _ = a_service()
        async with AsyncClient(
            transport=ASGITransport(app=an_app(service)), base_url="http://t"
        ) as client:
            reply = await post_photo(client, b"%PDF-1.4 not a photo")
        assert reply.status_code == 422
        assert reply.json()["error"]["code"] == "image_rejected"

    async def test_an_unknown_store_is_refused_before_anything_is_asked(self) -> None:
        service, parts = a_service()
        async with AsyncClient(
            transport=ASGITransport(app=an_app(service)), base_url="http://t"
        ) as client:
            reply = await post_photo(client, a_photo_file(), store_id="999")
        assert reply.status_code == 404
        assert parts["zory"].scan_calls == []

    async def test_the_finder_refuses_cleanly_when_not_configured(self) -> None:
        """No finder settings: the route answers, and says why, in our words."""

        class Unconfigured:
            settings = build_settings()
            zory_finder = None

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
        assert parts["zory"].recommend_calls == []


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
    selector, state = harness.references.calls[0]
    assert selector == PresentedOrdinal(position=2)
    assert state.product_interaction.presented_product_ids == (31, 32, 33)
    assert "picked the sofa" in harness.told(0)[1]


# ── settings ════════════════════════════════════════════════════════════════


def test_settings_keep_the_candidate_pool_at_least_as_large_as_the_results() -> None:
    with pytest.raises(ValidationError):
        finder_settings(result_limit=10, candidate_limit=5)


def test_the_zory_host_and_the_stored_copy_have_no_default() -> None:
    with pytest.raises(ValidationError) as raised:
        FurnitureFinderSettings()  # type: ignore[call-arg]
    missing = {error["loc"][0] for error in raised.value.errors()}
    assert missing == {"zory_base_url", "stored_image_base_url"}


def test_the_defaults_follow_the_platform_finder() -> None:
    settings = FurnitureFinderSettings(
        zory_base_url="https://zory.test", stored_image_base_url="https://images.test"
    )
    assert (settings.min_confidence, settings.duplicate_overlap, settings.max_objects) == (
        0.3,
        0.6,
        12,
    )
    assert settings.min_short_side == 512
    assert settings.result_limit == 10
    assert settings.candidate_limit == 20
    assert settings.step_type == "ai_agent"
    assert settings.local_widget_key is None, "no visitor of our own unless configured"


def test_the_finder_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZORY_FURNITURE_FINDER__ZORY_BASE_URL", "https://stageapi.zory.ai")
    monkeypatch.setenv("ZORY_FURNITURE_FINDER__STORED_IMAGE_BASE_URL", "https://bucket.test")
    monkeypatch.setenv("ZORY_FURNITURE_FINDER__LOCAL_WIDGET_KEY", "furniture-detection")
    finder = build_settings().furniture_finder
    assert finder is not None
    assert finder.zory_base_url == "https://stageapi.zory.ai"
    assert finder.stored_image_base_url == "https://bucket.test"
    assert finder.local_widget_key == "furniture-detection"


def test_the_startup_summary_names_the_platform_it_calls() -> None:
    settings = build_settings(furniture_finder=finder_settings().model_dump(mode="json"))
    summary = settings.redacted()
    assert summary["furniture_finder_configured"] is True
    assert summary["furniture_finder_upstream"] == "https://zory.test"
