"""The main platform's furniture finder, as this service calls it.

Zory owns the finder: it stores the photo, runs its detector, and compares a
picked piece's pixels with every product photo in the store. This module is
the only place that knows its paths, its wire shapes and its headers, and no
caller ever sees a transport exception - every failure becomes one of our
errors, whose message we wrote.

Every call runs as a shopper: the widget session the gateway forwarded, or an
anonymous visitor this service signed in for a local run. The token is passed
on and never logged.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.config import FurnitureFinderSettings
from app.core.exceptions import (
    DetectionUnavailableError,
    FinderAccessRefusedError,
    VisualSearchUnavailableError,
    ZoryError,
    ZoryTokenRejectedError,
)
from app.core.logging import get_logger
from app.schemas.furniture_finder import (
    ImageBox,
    Point,
    ShopperCredentials,
    is_plain_key,
)

logger = get_logger(__name__)

ANONYMOUS_PATH = "/api/widgets/auth/anonymous/"
UPLOAD_PATH = "/api/widgets/uploads/"
PROCESS_PATH = "/api/process_furniture_finder/"
DETECTION_PATH = "/api/object-detection/{scan_id}/"
RECOMMEND_PATH = "/api/get-recommendations/"

WIDGET_KEY_HEADER = "X-Zory-Widget-Key"
WIDGET_STORE_HEADER = "X-Zory-Widget-Store-Id"
INTEGRATION_HEADER = "X-Zory-Integration-Id"

_UNSUPPORTED_CATEGORY = "category not supported"


@dataclass(frozen=True, slots=True)
class RawDetection:
    """One outline, exactly as the detector drew it, before any filtering."""

    label: str
    confidence: float
    box: tuple[int, int, int, int]
    polygon: tuple[Point, ...]


@dataclass(frozen=True, slots=True)
class ZoryScan:
    """A detection the platform ran and keeps: what it found, on which copy."""

    scan_id: str
    stored_image: str
    detections: tuple[RawDetection, ...]
    size: tuple[int, int] | None
    """The stored copy's width and height, when the detector reported it."""


@dataclass(frozen=True, slots=True)
class ZoryMatch:
    """A product the platform proposed for a piece, best first."""

    pinecone_id: str
    product_url: str | None
    score: float


def category_key(label: str) -> str:
    """A detector label as the catalog's visual `category` spells it."""
    return label.strip().lower().replace(" ", "-")


class ZoryFinderClient:
    """Adapter over the platform's finder endpoints. One HTTP client per process."""

    def __init__(
        self,
        settings: FurnitureFinderSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`transport` replaces the network in tests; production passes none."""
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.zory_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.timeout_s, connect=10.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ── who the finder runs as ──────────────────────────────────────────────

    async def anonymous_visitor(
        self, *, widget_key: str, store_public_id: str, anonymous_id: str
    ) -> ShopperCredentials:
        """Sign in an anonymous visitor on a store's widget, as the widget does."""
        response = await self._send(
            "post",
            ANONYMOUS_PATH,
            step="anonymous",
            headers={WIDGET_KEY_HEADER: widget_key, INTEGRATION_HEADER: store_public_id},
            json={"anonymous_id": anonymous_id},
        )
        body = _json(response, step="anonymous")
        tokens = body.get("tokens") if isinstance(body, dict) else None
        access = tokens.get("access") if isinstance(tokens, dict) else None
        if not isinstance(access, str) or not access:
            raise FinderAccessRefusedError(reason="no_visitor_token", step="anonymous")
        return ShopperCredentials(
            token=SecretStr(access), widget_key=widget_key, store_public_id=store_public_id
        )

    # ── detection ───────────────────────────────────────────────────────────

    async def scan(self, shopper: ShopperCredentials, image_jpeg: bytes) -> ZoryScan:
        """Upload the photo as the shopper, detect what is in it, read it back.

        The detection is sent as a step of the ai-agent turn (`step_type`), so
        the platform does not charge for it again.
        """
        headers = _shopper_headers(shopper)
        uploaded = _json(
            await self._send(
                "post",
                UPLOAD_PATH,
                step="upload",
                headers=headers,
                files={"file": ("photo.jpg", image_jpeg, "image/jpeg")},
            ),
            step="upload",
        )
        key = uploaded.get("key") if isinstance(uploaded, dict) else None
        if not isinstance(key, str) or not key:
            raise DetectionUnavailableError(reason="malformed_upload", step="upload")

        processed = _json(
            await self._send(
                "post",
                PROCESS_PATH,
                step="process",
                headers=headers,
                data={
                    "image_url": key,
                    "step_type": self._settings.step_type,
                    "type": "furniture_finder",
                },
            ),
            step="process",
        )
        payload = processed.get("data", processed) if isinstance(processed, dict) else None
        scan_id = payload.get("uuid") if isinstance(payload, dict) else None
        stored = payload.get("img_url") if isinstance(payload, dict) else None
        if scan_id in (None, "") or not is_plain_key(stored):
            raise DetectionUnavailableError(reason="malformed_process", step="process")

        # A widget session reads its own detection back. `permission` is the
        # dashboard's flag and would refuse it.
        detection = _json(
            await self._send(
                "get",
                DETECTION_PATH.format(scan_id=scan_id),
                step="detection",
                headers=headers,
            ),
            step="detection",
        )
        result = detection.get("data") if isinstance(detection, dict) else None
        detections, size = parse_detection_result(result)
        return ZoryScan(
            scan_id=str(scan_id),
            stored_image=str(stored),
            detections=detections,
            size=size,
        )

    # ── recommendations ─────────────────────────────────────────────────────

    async def recommend(
        self,
        shopper: ShopperCredentials | None,
        *,
        image_url: str,
        category: str,
        box: ImageBox,
        polygon: tuple[Point, ...],
        size: tuple[int, int],
        top_k: int,
        store_id: int,
    ) -> tuple[ZoryMatch, ...]:
        """Products like the outlined piece, nearest first, within one store.

        With a shopper the platform scopes to the store their session is for;
        without one, to `store_id`. A category the platform has no products
        for is an empty answer, not a failure.
        """
        response = await self._send(
            "post",
            RECOMMEND_PATH,
            step="recommend",
            headers=_shopper_headers(shopper),
            json={
                "image_url": image_url,
                "category": category,
                "top_k": top_k,
                "bbox": [box.x1, box.y1, box.x2, box.y2],
                "mask_polygon": [[x, y] for x, y in polygon],
                "frame_size": [size[0], size[1]],
                "store_id": store_id,
            },
            unsupported_is_empty=True,
        )
        if response is None:
            return ()
        body = _json(response, step="recommend")
        recommendations = body.get("recommendations") if isinstance(body, dict) else None
        matches = (
            recommendations.get("matches")
            if isinstance(recommendations, dict)
            else body.get("matches")
            if isinstance(body, dict)
            else None
        )
        if not isinstance(matches, list):
            raise VisualSearchUnavailableError(reason="malformed_recommendations")
        return tuple(match for item in matches if (match := _match(item)) is not None)

    # ── transport ───────────────────────────────────────────────────────────

    async def _send(
        self,
        method: str,
        path: str,
        *,
        step: str,
        unsupported_is_empty: bool = False,
        **kwargs: Any,
    ) -> httpx.Response | None:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("zory_finder_unreachable", step=step, error_type=type(exc).__name__)
            raise _unavailable(step, reason="unreachable") from exc
        if response.status_code < 400:
            return response
        code = _error_code(response)
        if (
            unsupported_is_empty
            and response.status_code == 400
            and _UNSUPPORTED_CATEGORY in code.lower()
        ):
            logger.info("zory_finder_category_unsupported", step=step)
            return None
        logger.warning(
            "zory_finder_refused", step=step, status=response.status_code, zory_code=code
        )
        raise _refusal(step, response.status_code, code)


# ── parsing ─────────────────────────────────────────────────────────────────


def parse_detection_result(
    result: Any,
) -> tuple[tuple[RawDetection, ...], tuple[int, int] | None]:
    """The detector's JSON as detections, in the stored copy's pixel space.

    A malformed *object* is skipped - one bad outline should not cost the
    customer the other seventeen. A malformed *result* is a failure: it means
    the platform answered something other than a detection.
    """
    if not isinstance(result, dict) or not isinstance(result.get("detected_objects"), list):
        raise DetectionUnavailableError(reason="malformed_result", step="detection")
    scale_x, scale_y = _scale(result)
    detections: list[RawDetection] = []
    skipped = 0
    for item in result["detected_objects"]:
        parsed = _detection(item, scale_x, scale_y)
        if parsed is None:
            skipped += 1
        else:
            detections.append(parsed)
    if skipped:
        logger.warning("detections_skipped_as_malformed", skipped=skipped)
    return tuple(detections), _size(result)


def _size(result: Mapping[str, Any]) -> tuple[int, int] | None:
    """The image the detector was sent: its original dimensions, else its frame."""
    for key in ("original_dimensions", "frame_size"):
        value = result.get(key)
        if (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(v, int | float) and v > 0 for v in value)
        ):
            return round(value[0]), round(value[1])
    return None


def _scale(result: Mapping[str, Any]) -> tuple[float, float]:
    """Frame -> stored-copy factors. 1.0 when they agree, which is the norm."""
    frame = result.get("frame_size")
    original = result.get("original_dimensions")
    if (
        isinstance(frame, list)
        and isinstance(original, list)
        and len(frame) == 2
        and len(original) == 2
        and all(isinstance(v, int | float) and v > 0 for v in (*frame, *original))
    ):
        return float(original[0]) / float(frame[0]), float(original[1]) / float(frame[1])
    return 1.0, 1.0


def _detection(item: Any, scale_x: float, scale_y: float) -> RawDetection | None:
    if not isinstance(item, dict):
        return None
    label = item.get("label")
    confidence = item.get("confidence")
    if not isinstance(label, str) or not label.strip():
        return None
    if not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
        return None
    points = _points(item.get("mask_polygon"), scale_x, scale_y)
    if points is None:
        # No outline: the box stands in for one, as the platform's own finder
        # does, so a piece it detected is never lost for want of a mask.
        points = _box_outline(item.get("bbox_from_mask"), scale_x, scale_y)
    if points is None:
        return None
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    # The box is drawn around the outline rather than read from the response:
    # it is the outline that is searched, so the crop must match it exactly.
    return RawDetection(
        label=label.strip().lower(),
        confidence=float(confidence),
        box=(min(xs), min(ys), max(xs) + 1, max(ys) + 1),
        polygon=points,
    )


def _points(polygon: Any, scale_x: float, scale_y: float) -> tuple[Point, ...] | None:
    if not isinstance(polygon, list):
        return None
    points: list[Point] = []
    for point in polygon:
        if (
            not isinstance(point, list | tuple)
            or len(point) != 2
            or not all(isinstance(v, int | float) and math.isfinite(v) for v in point)
        ):
            return None
        points.append((round(point[0] * scale_x), round(point[1] * scale_y)))
    return tuple(points) if len(points) >= 3 else None


def _box_outline(box: Any, scale_x: float, scale_y: float) -> tuple[Point, ...] | None:
    if (
        not isinstance(box, list | tuple)
        or len(box) != 4
        or not all(isinstance(v, int | float) and math.isfinite(v) for v in box)
    ):
        return None
    x1, y1, x2, y2 = (
        round(box[0] * scale_x),
        round(box[1] * scale_y),
        round(box[2] * scale_x),
        round(box[3] * scale_y),
    )
    if x2 <= x1 or y2 <= y1:
        return None
    return ((x1, y1), (x2, y1), (x2, y2), (x1, y2))


def _match(item: Any) -> ZoryMatch | None:
    if not isinstance(item, dict):
        return None
    raw_metadata = item.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    pinecone_id = metadata.get("pinecone_id") or item.get("id")
    score = item.get("score")
    if not isinstance(pinecone_id, str | int) or str(pinecone_id) == "":
        return None
    if not isinstance(score, int | float) or not math.isfinite(score):
        return None
    product_url = metadata.get("product_url")
    return ZoryMatch(
        pinecone_id=str(pinecone_id),
        product_url=product_url if isinstance(product_url, str) and product_url else None,
        score=float(score),
    )


def _shopper_headers(shopper: ShopperCredentials | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if shopper is None:
        return headers
    headers["Authorization"] = f"Bearer {shopper.token.get_secret_value()}"
    if shopper.widget_key:
        headers[WIDGET_KEY_HEADER] = shopper.widget_key
    if shopper.store_public_id:
        headers[WIDGET_STORE_HEADER] = shopper.store_public_id
    return headers


def _json(response: httpx.Response | None, *, step: str) -> Any:
    if response is None:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise _unavailable(step, reason="not_json") from exc


def _error_code(response: httpx.Response) -> str:
    """The platform's own error code, for the logs. Never shown to anyone."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    for key in ("code", "error", "detail"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value[:120]
    return ""


def _unavailable(step: str, *, reason: str) -> ZoryError:
    if step == "recommend":
        return VisualSearchUnavailableError(reason=reason, step=step)
    return DetectionUnavailableError(reason=reason, step=step)


def _refusal(step: str, status: int, code: str) -> ZoryError:
    if status == 401:
        return ZoryTokenRejectedError(reason=code or "unauthorized", step=step, status=status)
    if status >= 500:
        return _unavailable(step, reason=f"status_{status}")
    if step == "recommend" and status == 400:
        return VisualSearchUnavailableError(reason=code or "bad_request", step=step)
    return FinderAccessRefusedError(reason=code or f"status_{status}", step=step, status=status)
