"""Voice input: what the microphone route answers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

LANGUAGE_PATTERN = r"^[a-z]{2,3}$"
"""An ISO 639-1 or 639-3 code ("ar", "en", "ara"); none means auto-detect."""


class SpeechTextResponse(BaseModel):
    """The words heard, for the customer to check and send. Empty when nothing
    was said; `language` is the one detected, when the model reports it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    language: str | None = None
