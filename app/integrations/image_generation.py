"""The image-generation boundary.

One capability: a prompt plus product photos in, one room image out. Two
providers stand behind it - OpenAI's image models through the edits endpoint,
and Gemini's through `generateContent` - and a fallback wrapper tries the
second once when the first fails. Callers never see which answered, a provider
object, or a provider exception: every failure is a
:class:`RenderUnavailableError` whose message we wrote.

Model identifiers, quality and size are configuration (CLAUDE.md 31).
"""

from __future__ import annotations

import base64
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from openai import AsyncOpenAI, OpenAIError

from app.core.config import VisualizationSettings
from app.core.exceptions import RenderUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_JPEG_QUALITY = 88

_OPENAI_SIZES = ("1536x1024", "1024x1024", "1024x1536")
"""The canvases gpt-image models draw on."""

_GEMINI_RATIOS = ("21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16")
"""The aspect ratios Gemini image models accept."""


@dataclass(frozen=True, slots=True)
class ImageReference:
    """A product photo, and the label a captioning model may show beside it."""

    data: bytes
    caption: str


@dataclass(frozen=True, slots=True)
class Frame:
    """The shape of the customer's own photo, which an edit of it must keep.

    Each provider draws on the canvas nearest this shape; the caller scales
    the answer back to the photo's exact size.
    """

    width: int
    height: int


def nearest_shape(frame: Frame, shapes: Sequence[str], separator: str) -> str:
    """The provider's canvas closest in proportion to the photo's.

    Compared on a log scale, so 3:2 is as far from 1:1 as 2:3 is.
    """

    def distance(shape: str) -> float:
        across, down = (int(part) for part in shape.split(separator))
        return abs(math.log(across / down) - math.log(frame.width / frame.height))

    return min(shapes, key=distance)


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    data: bytes
    mime: str
    provider: str


class ImageGenerator(Protocol):
    async def generate(
        self,
        prompt: str,
        references: Sequence[ImageReference],
        frame: Frame | None = None,
    ) -> GeneratedImage:
        """`frame` is set when the first reference is the customer's photo and
        the answer must keep its shape; unset, the configured canvas is used."""
        ...


class OpenAIImageGenerator:
    """gpt-image models. References go up as numbered files, in prompt order."""

    provider = "openai"

    def __init__(
        self,
        settings: VisualizationSettings,
        *,
        api_key: str,
        base_url: str | None = None,
        client: Any = None,
    ) -> None:
        """`client` exists for tests; production builds its own."""
        if settings.openai_model is None:
            raise ValueError("openai_model is not configured")
        self._model = settings.openai_model
        self._quality = settings.openai_quality
        self._size = settings.openai_size
        # No SDK retries: a failed render falls back to the other provider,
        # which is a better second attempt than the same one again.
        self._client = client or AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=settings.timeout_s, max_retries=0
        )

    async def generate(
        self,
        prompt: str,
        references: Sequence[ImageReference],
        frame: Frame | None = None,
    ) -> GeneratedImage:
        common: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "size": nearest_shape(frame, _OPENAI_SIZES, "x") if frame else self._size,
            "quality": self._quality,
            "n": 1,
            "output_format": "jpeg",
            "output_compression": _JPEG_QUALITY,
        }
        try:
            if references:
                result = await self._client.images.edit(
                    image=[
                        (f"ref{number}.jpg", ref.data, "image/jpeg")
                        for number, ref in enumerate(references, start=1)
                    ],
                    **common,
                )
            else:
                result = await self._client.images.generate(**common)
        except OpenAIError as exc:
            # Class only: the SDK's message can echo the prompt.
            logger.warning(
                "render_provider_failed", provider=self.provider, error_type=type(exc).__name__
            )
            raise RenderUnavailableError(provider=self.provider, reason=type(exc).__name__) from exc
        data = result.data[0].b64_json if result.data else None
        if not data:
            raise RenderUnavailableError(provider=self.provider, reason="no_image")
        return GeneratedImage(
            data=base64.b64decode(data), mime="image/jpeg", provider=self.provider
        )

    async def close(self) -> None:
        await self._client.close()


class GeminiImageGenerator:
    """Gemini image models. Each photo travels with its own caption."""

    provider = "gemini"

    def __init__(
        self,
        settings: VisualizationSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`transport` exists for tests; production uses httpx's default."""
        if settings.gemini_model is None or settings.gemini_api_key is None:
            raise ValueError("gemini_model and gemini_api_key are required")
        self._url = _GEMINI_URL.format(model=settings.gemini_model)
        self._aspect_ratio = settings.gemini_aspect_ratio
        self._image_size = settings.gemini_image_size
        self._photo_temperature = settings.photo_edit_temperature
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=settings.timeout_s,
            headers={"x-goog-api-key": settings.gemini_api_key.get_secret_value()},
        )

    async def generate(
        self,
        prompt: str,
        references: Sequence[ImageReference],
        frame: Frame | None = None,
    ) -> GeneratedImage:
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for ref in references:
            parts.append({"text": ref.caption})
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(ref.data).decode("ascii"),
                    }
                }
            )
        generation: dict[str, Any] = {
            "responseModalities": ["IMAGE"],
            "imageConfig": {
                "aspectRatio": (
                    nearest_shape(frame, _GEMINI_RATIOS, ":") if frame else self._aspect_ratio
                ),
                "imageSize": self._image_size,
            },
        }
        if frame is not None and self._photo_temperature is not None:
            # Editing their photo: it must come back as itself.
            generation["temperature"] = self._photo_temperature
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": generation}
        try:
            response = await self._client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            logger.warning(
                "render_provider_failed", provider=self.provider, error_type=type(exc).__name__
            )
            raise RenderUnavailableError(provider=self.provider, reason=type(exc).__name__) from exc
        if response.status_code != httpx.codes.OK:
            logger.warning(
                "render_provider_rejected", provider=self.provider, status=response.status_code
            )
            raise RenderUnavailableError(provider=self.provider, status=response.status_code)
        return GeneratedImage(
            data=_gemini_image(response), mime="image/jpeg", provider=self.provider
        )

    async def close(self) -> None:
        await self._client.aclose()


def _gemini_image(response: httpx.Response) -> bytes:
    """The first image in the answer, or a controlled failure.

    A blocked or empty answer has no image part; it is a failure like any other,
    so the caller's fallback gets its turn.
    """
    try:
        payload = response.json()
        for candidate in payload.get("candidates") or ():
            for part in (candidate.get("content") or {}).get("parts") or ():
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise RenderUnavailableError(provider="gemini", reason="malformed_response") from exc
    logger.warning("render_provider_returned_no_image", provider="gemini")
    raise RenderUnavailableError(provider="gemini", reason="no_image")


class FallbackImageGenerator:
    """The primary, and - when it fails - the fallback, once."""

    def __init__(self, primary: ImageGenerator, fallback: ImageGenerator | None) -> None:
        self._primary = primary
        self._fallback = fallback

    async def generate(
        self,
        prompt: str,
        references: Sequence[ImageReference],
        frame: Frame | None = None,
    ) -> GeneratedImage:
        try:
            return await self._primary.generate(prompt, references, frame)
        except RenderUnavailableError:
            if self._fallback is None:
                raise
            logger.warning("render_falling_back")
            return await self._fallback.generate(prompt, references, frame)
