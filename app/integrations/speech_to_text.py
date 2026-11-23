"""ElevenLabs Scribe, as this service calls it.

The only place that knows ElevenLabs' path, headers and wire shape. No caller
sees a transport exception: every failure becomes one of our errors, whose
message we wrote. The key is sent as a header and never logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import SpeechToTextSettings
from app.core.exceptions import AudioRejectedError, SpeechToTextUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

TRANSCRIBE_PATH = "/v1/speech-to-text"
LIVE_SESSION_PATH = "/v1/single-use-token/realtime_scribe"
KEY_HEADER = "xi-api-key"

# ElevenLabs answers these when the recording itself is unusable - the
# customer can act on that by recording again. Everything else is ours.
_REJECTED_STATUSES = {httpx.codes.BAD_REQUEST, httpx.codes.UNPROCESSABLE_ENTITY}


@dataclass(frozen=True, slots=True)
class HeardSpeech:
    text: str
    language_code: str | None
    duration_s: float | None


class ElevenLabsTranscriber:
    """Adapter over ElevenLabs' speech-to-text endpoint. One HTTP client per process."""

    def __init__(
        self,
        settings: SpeechToTextSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`transport` replaces the network in tests; production passes none."""
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.timeout_s, connect=10.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        content_type: str,
        language: str | None = None,
    ) -> HeardSpeech:
        form: dict[str, str] = {
            "model_id": self._settings.model_id,
            "tag_audio_events": "false",
            "numbers_format": self._settings.numbers_format,
        }
        if language:
            form["language_code"] = language
        try:
            response = await self._client.post(
                TRANSCRIBE_PATH,
                headers={KEY_HEADER: self._settings.api_key.get_secret_value()},
                data=form,
                files={"file": (filename, audio, content_type)},
            )
        except httpx.HTTPError as exc:
            logger.warning("speech_to_text_failed", error_type=type(exc).__name__)
            raise SpeechToTextUnavailableError(error_type=type(exc).__name__) from exc
        if response.status_code in _REJECTED_STATUSES:
            logger.info("speech_to_text_rejected_audio", status=response.status_code)
            raise AudioRejectedError(status=response.status_code)
        if response.status_code != httpx.codes.OK:
            logger.warning("speech_to_text_failed", status=response.status_code)
            raise SpeechToTextUnavailableError(status=response.status_code)
        return _heard(_json(response))

    async def live_token(self) -> str:
        """A single-use token for one live connection from the browser."""
        try:
            response = await self._client.post(
                LIVE_SESSION_PATH,
                headers={KEY_HEADER: self._settings.api_key.get_secret_value()},
            )
        except httpx.HTTPError as exc:
            logger.warning("live_speech_token_failed", error_type=type(exc).__name__)
            raise SpeechToTextUnavailableError(error_type=type(exc).__name__) from exc
        if response.status_code != httpx.codes.OK:
            logger.warning("live_speech_token_failed", status=response.status_code)
            raise SpeechToTextUnavailableError(status=response.status_code)
        token = _json(response).get("token")
        if not isinstance(token, str) or not token:
            raise SpeechToTextUnavailableError(reason="no_token")
        return token


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise SpeechToTextUnavailableError(reason="not_json") from exc
    if not isinstance(body, dict):
        raise SpeechToTextUnavailableError(reason="not_an_object")
    return body


def _heard(body: dict[str, Any]) -> HeardSpeech:
    text = body.get("text")
    if not isinstance(text, str):
        raise SpeechToTextUnavailableError(reason="no_text")
    language = body.get("language_code")
    duration = body.get("audio_duration_secs")
    return HeardSpeech(
        text=text.strip(),
        language_code=language if isinstance(language, str) and language else None,
        duration_s=float(duration) if isinstance(duration, int | float) else None,
    )
