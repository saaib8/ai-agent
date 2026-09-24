"""The object detection boundary.

The detector is a model deployed on Modal and owned by the main platform; this
service only calls it. It takes one image and returns outlined objects, each
with a visual class label, a confidence and a polygon in the pixel space of
the image it was sent.

Nothing outside this module knows the endpoint's wire shape or its proxy-auth
headers, and no caller ever sees a transport exception: every failure becomes
:class:`DetectionUnavailableError`, whose message we wrote.
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import FurnitureFinderSettings
from app.core.exceptions import DetectionUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RawDetection:
    """One outline, exactly as the detector drew it, before any filtering."""

    label: str
    confidence: float
    box: tuple[int, int, int, int]
    polygon: tuple[tuple[int, int], ...]


class ObjectDetector(Protocol):
    async def detect(self, image_jpeg: bytes) -> tuple[RawDetection, ...]: ...


class ModalObjectDetector:
    """Adapter over the synchronous Modal endpoint. One HTTP client per process."""

    def __init__(
        self,
        settings: FurnitureFinderSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`transport` exists for tests; production uses httpx's default."""
        self._url = settings.detector_url
        self._confidence = settings.detector_confidence
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=settings.detector_timeout_s,
            headers={
                "Modal-Key": settings.detector_key.get_secret_value(),
                "Modal-Secret": settings.detector_secret.get_secret_value(),
            },
        )

    async def detect(self, image_jpeg: bytes) -> tuple[RawDetection, ...]:
        payload = {
            "image": base64.b64encode(image_jpeg).decode("ascii"),
            "confidence_threshold": self._confidence,
        }
        try:
            response = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            logger.warning("detection_request_failed", error_type=type(exc).__name__)
            raise DetectionUnavailableError(reason=type(exc).__name__) from exc
        if response.status_code != httpx.codes.OK:
            # Status only: the body may echo the request, which is an image.
            logger.warning("detection_rejected", status=response.status_code)
            raise DetectionUnavailableError(status=response.status_code)
        try:
            body = response.json()
        except ValueError as exc:
            raise DetectionUnavailableError(reason="non_json_response") from exc
        return parse_detections(body)

    async def close(self) -> None:
        await self._client.aclose()


def parse_detections(body: Any) -> tuple[RawDetection, ...]:
    """The endpoint's JSON as detections, or a controlled failure.

    A malformed *object* is skipped - one bad outline should not cost the
    customer the other seventeen. A malformed *envelope* is a failure: it
    means the endpoint answered something other than a detection.

    Coordinates are rescaled when the detector reports a frame other than the
    one it was sent, so callers can always read them in the sent image's space.
    """
    if not isinstance(body, dict) or str(body.get("status", "")).lower() not in (
        "success",
        "completed",
        "ok",
    ):
        logger.warning("detection_failed_status")
        raise DetectionUnavailableError(reason="unsuccessful_status")
    result = body.get("result", body)
    if not isinstance(result, dict) or not isinstance(result.get("detected_objects"), list):
        raise DetectionUnavailableError(reason="malformed_result")

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
    return tuple(detections)


def _scale(result: dict[str, Any]) -> tuple[float, float]:
    """Frame -> sent-image factors. 1.0 when they agree, which is the norm."""
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
    polygon = item.get("mask_polygon")
    if not isinstance(label, str) or not label.strip():
        return None
    if not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
        return None
    if not isinstance(polygon, list):
        return None
    points: list[tuple[int, int]] = []
    for point in polygon:
        if (
            not isinstance(point, list | tuple)
            or len(point) != 2
            or not all(isinstance(v, int | float) and math.isfinite(v) for v in point)
        ):
            return None
        points.append((round(point[0] * scale_x), round(point[1] * scale_y)))
    if len(points) < 3:
        return None

    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    # The box is derived from the outline rather than read from the
    # response: it is the outline that is searched, so the crop must be
    # drawn around exactly that.
    box = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
    return RawDetection(
        label=label.strip().lower(),
        confidence=float(confidence),
        box=box,
        polygon=tuple(points),
    )
