"""Voice input endpoint.

Transport only (CLAUDE.md 3.2): validate the form, resolve the retailer scope,
hand the recording to the service. The words come back to the customer, who
sends them as a message like anything typed.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile

from app.api.dependencies import RetailerContextProviderDep, SpeechToTextServiceDep
from app.schemas.speech import (
    LANGUAGE_PATTERN,
    LiveSpeechRequest,
    LiveSpeechSession,
    SpeechTextResponse,
)

router = APIRouter(prefix="/speech", tags=["speech"])


@router.post("/transcriptions", response_model=SpeechTextResponse)
async def transcribe(
    store_id: Annotated[int, Form(ge=1)],
    audio: Annotated[UploadFile, File()],
    speech: SpeechToTextServiceDep,
    retailers: RetailerContextProviderDep,
    language: Annotated[str | None, Form(pattern=LANGUAGE_PATTERN)] = None,
) -> SpeechTextResponse:
    """Turn a recording into text. Reads one byte past the limit so an
    oversized upload is recognised without reading all of it into memory."""
    context = await retailers.resolve(store_id)
    data = await audio.read(speech.max_upload_bytes + 1)
    return await speech.transcribe(
        data,
        filename=audio.filename,
        content_type=audio.content_type,
        language=language,
        store_id=context.store_id,
    )


@router.post("/live-sessions", response_model=LiveSpeechSession)
async def live_session(
    request: LiveSpeechRequest,
    speech: SpeechToTextServiceDep,
    retailers: RetailerContextProviderDep,
) -> LiveSpeechSession:
    """A single-use token and the connection details: the browser streams the
    microphone to ElevenLabs and shows the words as the customer speaks."""
    context = await retailers.resolve(request.store_id)
    return await speech.live_session(store_id=context.store_id)
