"""Voice input: a recording in, the words heard out.

The text is never sent as a message here - it goes back to the customer, who
checks it and sends it like anything they typed.
"""

from __future__ import annotations

from app.core.config import SpeechToTextSettings
from app.core.exceptions import AudioRejectedError
from app.core.logging import get_logger
from app.integrations.speech_to_text import ElevenLabsTranscriber
from app.schemas.speech import SpeechTextResponse

logger = get_logger(__name__)

MIN_AUDIO_BYTES = 256
"""Shorter than any real recording: a click on the microphone and nothing said."""

_AUDIO_TYPES = ("audio/", "video/")
"""Browsers record to audio/webm (Chrome, Firefox) or audio/mp4 (Safari);
a phone's voice memo may arrive as video/mp4 or video/webm."""

TOO_LARGE = "That recording is too long. Please send it in shorter parts."
NOT_AUDIO = "That file is not a recording. Please use the microphone."


class SpeechToTextService:
    def __init__(self, transcriber: ElevenLabsTranscriber, settings: SpeechToTextSettings) -> None:
        self._transcriber = transcriber
        self._settings = settings

    @property
    def max_upload_bytes(self) -> int:
        return self._settings.max_upload_bytes

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str | None,
        content_type: str | None,
        language: str | None,
        store_id: int,
    ) -> SpeechTextResponse:
        kind = (content_type or "").split(";")[0].strip().lower()
        if not kind.startswith(_AUDIO_TYPES):
            raise AudioRejectedError(public_message=NOT_AUDIO, content_type=kind)
        if len(audio) > self._settings.max_upload_bytes:
            raise AudioRejectedError(public_message=TOO_LARGE, size=len(audio))
        if len(audio) < MIN_AUDIO_BYTES:
            return SpeechTextResponse(text="", language=None)
        heard = await self._transcriber.transcribe(
            audio,
            filename=filename or "recording",
            content_type=kind,
            language=language,
        )
        logger.info(
            "speech_transcribed",
            store_id=store_id,
            duration_s=heard.duration_s,
            language=heard.language_code,
            characters=len(heard.text),
        )
        return SpeechTextResponse(text=heard.text, language=heard.language_code)
