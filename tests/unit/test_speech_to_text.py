"""Voice input: settings, the ElevenLabs adapter, the service and the route."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.api.dependencies import (
    db_session,
    resources,
    retailer_context_provider,
    speech_to_text_service,
)
from app.api.errors import register_exception_handlers
from app.api.routes.speech import router as speech_router
from app.core.config import SpeechToTextSettings
from app.core.exceptions import (
    AudioRejectedError,
    LiveSpeechOffError,
    SpeechToTextUnavailableError,
)
from app.integrations.speech_to_text import ElevenLabsTranscriber
from app.services.speech_to_text import MIN_AUDIO_BYTES, SpeechToTextService
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import build_settings
from tests.unit.test_chat_api import STORE, FakeRetailers

AUDIO = b"\x1aE\xdf\xa3" + b"\x00" * 2048  # a webm header and some frames
HEARD = {
    "text": "  ابي كنبة رمادية بأقل من 3000 ريال  ",
    "language_code": "ara",
    "language_probability": 0.97,
    "words": [],
    "audio_duration_secs": 3.2,
}


def settings(**overrides: Any) -> SpeechToTextSettings:
    return SpeechToTextSettings(api_key="xi-test-key", **overrides)


class Recorder:
    """ElevenLabs, as a test sees it: what was sent, and a fixed answer."""

    def __init__(self, status: int = 200, body: Any = HEARD, *, fail: bool = False) -> None:
        self.status, self.body, self.fail = status, body, fail
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectTimeout("timed out", request=request)
        if isinstance(self.body, bytes):
            return httpx.Response(self.status, content=self.body)
        return httpx.Response(self.status, json=self.body)


def a_transcriber(recorder: Recorder, **overrides: Any) -> ElevenLabsTranscriber:
    return ElevenLabsTranscriber(settings(**overrides), transport=httpx.MockTransport(recorder))


async def transcribe(recorder: Recorder, **kwargs: Any) -> Any:
    transcriber = a_transcriber(recorder)
    try:
        return await transcriber.transcribe(
            AUDIO, filename="recording.webm", content_type="audio/webm", **kwargs
        )
    finally:
        await transcriber.close()


# ── settings ════════════════════════════════════════════════════════════════


class TestSettings:
    def test_voice_input_is_off_without_a_key(self) -> None:
        assert build_settings().speech_to_text is None

    def test_elevenlabs_own_variable_turns_it_on_with_scribe_v3(self) -> None:
        configured = build_settings(ELEVENLABS_API_KEY="xi-key").speech_to_text
        assert configured is not None
        assert configured.model_id == "scribe_v3"
        assert configured.api_key.get_secret_value() == "xi-key"
        assert configured.numbers_format == "digits"

    def test_a_key_inside_the_group_wins(self) -> None:
        configured = build_settings(
            ELEVENLABS_API_KEY="xi-key", speech_to_text={"api_key": "own", "model_id": "scribe_v2"}
        ).speech_to_text
        assert configured is not None
        assert configured.api_key.get_secret_value() == "own"
        assert configured.model_id == "scribe_v2"

    def test_the_key_is_never_shown(self) -> None:
        configured = build_settings(ELEVENLABS_API_KEY="xi-secret").speech_to_text
        assert "xi-secret" not in repr(configured)


# ── the ElevenLabs adapter ══════════════════════════════════════════════════


class TestTranscriber:
    async def test_the_words_come_back_trimmed_with_the_language(self) -> None:
        transcript = await transcribe(Recorder())
        assert transcript.text == "ابي كنبة رمادية بأقل من 3000 ريال"
        assert transcript.language_code == "ara"
        assert transcript.duration_s == 3.2

    async def test_it_sends_the_key_the_model_and_digits(self) -> None:
        recorder = Recorder()
        await transcribe(recorder, language="ar")
        request = recorder.requests[0]
        assert request.url.path == "/v1/speech-to-text"
        assert request.headers["xi-api-key"] == "xi-test-key"
        body = request.content.decode("utf-8", "replace")
        for field, value in (
            ("model_id", "scribe_v3"),
            ("numbers_format", "digits"),
            ("language_code", "ar"),
        ):
            assert f'name="{field}"\r\n\r\n{value}' in body
        assert 'filename="recording.webm"' in body

    async def test_no_language_means_auto_detect(self) -> None:
        recorder = Recorder()
        await transcribe(recorder)
        assert 'name="language_code"' not in recorder.requests[0].content.decode("utf-8", "replace")

    @pytest.mark.parametrize("status", [400, 422])
    async def test_an_unusable_recording_is_the_customers_to_redo(self, status: int) -> None:
        with pytest.raises(AudioRejectedError):
            await transcribe(Recorder(status, {"detail": "bad audio"}))

    @pytest.mark.parametrize("status", [401, 429, 500, 503])
    async def test_anything_else_is_ours_and_temporary(self, status: int) -> None:
        with pytest.raises(SpeechToTextUnavailableError) as raised:
            await transcribe(Recorder(status, {"detail": "nope"}))
        assert "nope" not in raised.value.public_message

    async def test_a_network_failure_is_unavailable(self) -> None:
        with pytest.raises(SpeechToTextUnavailableError):
            await transcribe(Recorder(fail=True))

    @pytest.mark.parametrize("body", [b"<html>", ["a list"], {"no": "text"}])
    async def test_an_answer_without_text_is_unavailable(self, body: Any) -> None:
        with pytest.raises(SpeechToTextUnavailableError):
            await transcribe(Recorder(200, body))


# ── the service ═════════════════════════════════════════════════════════════


def a_service(recorder: Recorder | None = None, **overrides: Any) -> SpeechToTextService:
    return SpeechToTextService(
        a_transcriber(recorder or Recorder(), **overrides), settings(**overrides)
    )


async def heard(service: SpeechToTextService, audio: bytes = AUDIO, **kwargs: Any) -> Any:
    values: dict[str, Any] = {
        "filename": "recording.webm",
        "content_type": "audio/webm;codecs=opus",
        "language": None,
        "store_id": STORE,
        **kwargs,
    }
    return await service.transcribe(audio, **values)


class TestService:
    async def test_a_browser_recording_is_transcribed(self) -> None:
        reply = await heard(a_service())
        assert reply.text == "ابي كنبة رمادية بأقل من 3000 ريال"
        assert reply.language == "ara"

    @pytest.mark.parametrize("kind", ["audio/mp4", "video/mp4", "audio/mpeg", "audio/wav"])
    async def test_every_browser_and_phone_format_is_accepted(self, kind: str) -> None:
        assert (await heard(a_service(), content_type=kind)).text

    @pytest.mark.parametrize("kind", ["image/png", "application/pdf", "", None])
    async def test_a_file_that_is_not_a_recording_is_refused(self, kind: str | None) -> None:
        recorder = Recorder()
        with pytest.raises(AudioRejectedError):
            await heard(a_service(recorder), content_type=kind)
        assert recorder.requests == []

    async def test_a_recording_over_the_limit_is_refused_unsent(self) -> None:
        recorder = Recorder()
        with pytest.raises(AudioRejectedError) as raised:
            await heard(a_service(recorder, max_upload_bytes=1000), audio=b"\x00" * 1001)
        assert "too long" in raised.value.public_message
        assert recorder.requests == []

    async def test_a_click_with_nothing_said_costs_nothing(self) -> None:
        recorder = Recorder()
        reply = await heard(a_service(recorder), audio=b"\x00" * (MIN_AUDIO_BYTES - 1))
        assert reply.text == ""
        assert recorder.requests == []


# ── the route ═══════════════════════════════════════════════════════════════


def an_app(service: SpeechToTextService | None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(speech_router, prefix="/v1")
    if service is not None:
        app.dependency_overrides[speech_to_text_service] = lambda: service
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()
    return app


async def post_audio(app: FastAPI, **form: str) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post(
            "/v1/speech/transcriptions",
            data={"store_id": str(STORE), **form},
            files={"audio": ("recording.webm", AUDIO, "audio/webm")},
        )


class TestRoute:
    async def test_a_recording_comes_back_as_text(self) -> None:
        reply = await post_audio(an_app(a_service()))
        assert reply.status_code == 200
        assert reply.json() == {"text": "ابي كنبة رمادية بأقل من 3000 ريال", "language": "ara"}

    async def test_an_unknown_store_is_refused(self) -> None:
        reply = await post_audio(an_app(a_service()), store_id="999")
        assert reply.status_code == 404

    async def test_a_language_that_is_not_a_code_is_refused(self) -> None:
        reply = await post_audio(an_app(a_service()), language="arabic please")
        assert reply.status_code == 422

    async def test_elevenlabs_down_is_a_clean_503_in_our_words(self) -> None:
        reply = await post_audio(an_app(a_service(Recorder(500, {"detail": "internal"}))))
        assert reply.status_code == 503
        assert reply.json()["error"]["code"] == "speech_to_text_unavailable"
        assert "internal" not in reply.text

    async def test_it_refuses_cleanly_when_not_configured(self) -> None:
        class Unconfigured:
            settings = build_settings()
            speech_to_text = None

        app = an_app(None)
        app.dependency_overrides[resources] = lambda: Unconfigured()
        app.dependency_overrides[db_session] = lambda: None
        reply = await post_audio(app)
        assert reply.json()["error"]["message"] == "Voice input is not configured."


# ── live: words while the customer speaks ═══════════════════════════════════


class TestLiveSession:
    async def test_the_browser_gets_a_token_never_the_key(self) -> None:
        recorder = Recorder(200, {"token": "single-use-abc"})
        session = await a_service(recorder).live_session(store_id=STORE)
        assert session.token == "single-use-abc"
        assert session.model_id == "scribe_v3_live"
        assert session.url == "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
        assert (session.audio_format, session.sample_rate) == ("pcm_16000", 16000)
        assert session.commit_strategy == "vad"
        assert "xi-test-key" not in session.model_dump_json()
        request = recorder.requests[0]
        assert request.url.path == "/v1/single-use-token/realtime_scribe"
        assert request.headers["xi-api-key"] == "xi-test-key"

    async def test_switched_off_it_refuses_without_asking_elevenlabs(self) -> None:
        recorder = Recorder(200, {"token": "t"})
        with pytest.raises(LiveSpeechOffError):
            await a_service(recorder, live_enabled=False).live_session(store_id=STORE)
        assert recorder.requests == []

    @pytest.mark.parametrize(
        ("status", "body"), [(401, {"detail": "bad key"}), (500, {}), (200, {"no": "token"})]
    )
    async def test_no_token_is_unavailable(self, status: int, body: Any) -> None:
        with pytest.raises(SpeechToTextUnavailableError):
            await a_service(Recorder(status, body)).live_session(store_id=STORE)

    async def test_the_route_answers_with_the_session(self) -> None:
        app = an_app(a_service(Recorder(200, {"token": "single-use-abc"})))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            reply = await client.post("/v1/speech/live-sessions", json={"store_id": STORE})
        assert reply.status_code == 200
        assert reply.json()["token"] == "single-use-abc"

    async def test_the_route_says_when_live_is_off(self) -> None:
        app = an_app(a_service(Recorder(200, {"token": "t"}), live_enabled=False))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            reply = await client.post("/v1/speech/live-sessions", json={"store_id": STORE})
        assert reply.status_code == 404
        assert reply.json()["error"]["code"] == "live_speech_off"

    async def test_the_route_refuses_an_unknown_store(self) -> None:
        app = an_app(a_service(Recorder(200, {"token": "t"})))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            reply = await client.post("/v1/speech/live-sessions", json={"store_id": 999})
        assert reply.status_code == 404
        assert reply.json()["error"]["code"] == "store_not_found"
