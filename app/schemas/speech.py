"""Voice input: what the microphone route answers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

LANGUAGE_PATTERN = r"^[a-z]{2,3}$"
"""An ISO 639-1 or 639-3 code ("ar", "en", "ara"); none means auto-detect."""


class SpeechTextResponse(BaseModel):
    """The words heard, for the customer to check and send. Empty when nothing
    was said; `language` is the one detected, when the model reports it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    language: str | None = None


class LiveSpeechRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    store_id: int = Field(ge=1)


class LiveSpeechSession(BaseModel):
    """What the browser needs to stream the microphone to ElevenLabs itself:
    a single-use token (15 minutes, one connection) and how to connect. The
    API key is never in it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str
    url: str
    model_id: str
    audio_format: str
    sample_rate: int
    commit_strategy: str
