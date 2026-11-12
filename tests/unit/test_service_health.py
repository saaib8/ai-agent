"""The public health check (`GET /v1/health`).

It is reachable from outside, so it must stay cheap and say nothing about the
estate: which dependency is down, never why or where. The outside services are
probed with calls that cost nothing, at most once per cache window, and a slow
or broken probe is that service being down - never a failed health check.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
import pytest
from app.api import dependencies as deps
from app.core.config import FurnitureFinderSettings, HealthSettings, LLMSettings, Settings
from app.core.lifespan import AppResources
from app.integrations.llm import OpenAIStructuredClient
from app.integrations.pinecone import PineconeSemanticIndex, SemanticIndex
from app.integrations.postgres import Database
from app.integrations.redis import RedisClient
from app.integrations.zory_finder import HEALTH_PATH, ZoryFinderClient
from app.main import create_app
from app.orchestration.graph import ChatGraphRunner
from app.services.health import HealthService
from app.services.service_health import (
    OutsideHealthCache,
    OverallStatus,
    ServiceHealthService,
    ServiceStatus,
)
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import load_room_pieces
from app.taxonomy.seating import load_seating_semantics
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openai import AsyncOpenAI
from pydantic import SecretStr, ValidationError

from tests.conftest import build_settings


def finder_settings() -> FurnitureFinderSettings:
    return FurnitureFinderSettings(
        zory_base_url="https://zory.test",
        stored_image_base_url="https://images.test",
        local_widget_key="furniture-detection",
    )


class _Probe:
    """Answers a health check and counts how often it was asked."""

    def __init__(self, healthy: bool = True, *, delay_s: float = 0.0) -> None:
        self.healthy = healthy
        self.delay_s = delay_s
        self.calls = 0

    async def check_health(self) -> bool:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return self.healthy


class _Broken:
    async def check_health(self) -> bool:
        raise RuntimeError("driver said: host db.internal:5432 refused")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Estate:
    """Every dependency the public check reads, each a counting probe."""

    def __init__(
        self,
        *,
        postgres: bool = True,
        redis: bool = True,
        llm: bool = True,
        pinecone: bool | None = True,
        finder: bool | None = True,
    ) -> None:
        self.postgres = _Probe(postgres)
        self.redis = _Probe(redis)
        self.llm = _Probe(llm)
        self.pinecone = None if pinecone is None else _Probe(pinecone)
        self.finder = None if finder is None else _Probe(finder)

    def core(self) -> HealthService:
        return HealthService(cast(Database, self.postgres), cast(RedisClient, self.redis))


def _app(settings: Settings, estate: _Estate) -> FastAPI:
    application = create_app(settings)
    taxonomy = load_taxonomy()
    resources = AppResources(
        chat_graph=ChatGraphRunner(),
        settings=settings,
        database=cast(Database, estate.postgres),
        redis=cast(RedisClient, estate.redis),
        llm=cast(OpenAIStructuredClient, estate.llm),
        taxonomy=taxonomy,
        attributes=load_catalog_attributes(),
        dimensions=load_dimension_semantics(taxonomy=taxonomy),
        seating=load_seating_semantics(taxonomy=taxonomy),
        rooms=load_room_pieces(taxonomy=taxonomy),
        semantic_index=cast(SemanticIndex | None, estate.pinecone),
        zory_finder=cast(ZoryFinderClient | None, estate.finder),
    )
    application.dependency_overrides[deps.resources] = lambda: resources
    application.dependency_overrides[deps.health_service] = estate.core
    return application


async def _get(app: FastAPI, path: str = "/v1/health") -> httpx.Response:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def _service(
    estate: _Estate,
    *,
    cache: OutsideHealthCache | None = None,
    settings: HealthSettings | None = None,
) -> ServiceHealthService:
    return ServiceHealthService(
        estate.core(),
        llm=estate.llm,
        semantic_index=estate.pinecone,
        zory_finder=estate.finder,
        cache=cache or OutsideHealthCache(),
        settings=settings or HealthSettings(),
    )


# ── the endpoint ────────────────────────────────────────────────────────────


async def test_everything_up_is_ok(settings: Settings) -> None:
    response = await _get(_app(settings, _Estate()))

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "zory-agent",
        "environment": "test",
        "dependencies": {
            "postgres": "up",
            "redis": "up",
            "llm": "up",
            "pinecone": "up",
            "zory_finder": "up",
        },
    }


async def test_an_unconfigured_feature_is_not_a_fault(settings: Settings) -> None:
    response = await _get(_app(settings, _Estate(pinecone=None, finder=None)))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dependencies"]["pinecone"] == "not_configured"
    assert body["dependencies"]["zory_finder"] == "not_configured"


@pytest.mark.parametrize(
    ("estate", "down"),
    [
        (_Estate(postgres=False), "postgres"),
        (_Estate(redis=False), "redis"),
        (_Estate(llm=False), "llm"),
    ],
)
async def test_no_chat_possible_is_503(settings: Settings, estate: _Estate, down: str) -> None:
    response = await _get(_app(settings, estate))

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "down"
    assert body["dependencies"][down] == "down"
    assert [name for name, s in body["dependencies"].items() if s == "down"] == [down]


@pytest.mark.parametrize(
    ("estate", "down"),
    [(_Estate(pinecone=False), "pinecone"), (_Estate(finder=False), "zory_finder")],
)
async def test_a_feature_down_is_degraded_not_down(
    settings: Settings, estate: _Estate, down: str
) -> None:
    response = await _get(_app(settings, estate))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"][down] == "down"


async def test_it_is_never_cached_by_a_proxy_and_is_traceable(settings: Settings) -> None:
    response = await _get(_app(settings, _Estate()))

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]


async def test_a_broken_probe_names_the_service_and_nothing_else(settings: Settings) -> None:
    estate = _Estate()
    estate.finder = cast(Any, _Broken())

    response = await _get(_app(settings, estate))

    assert response.status_code == 200
    assert response.json()["dependencies"]["zory_finder"] == "down"
    rendered = response.text
    for detail in ("db.internal", "5432", "refused", "RuntimeError", "zory.test", "localhost"):
        assert detail not in rendered


async def test_the_container_probe_is_unchanged(settings: Settings) -> None:
    """`/health` still reads PostgreSQL and Redis only: the model provider
    being down must not get the container restarted."""
    response = await _get(_app(settings, _Estate(llm=False)), "/health")

    assert response.status_code == 200
    assert response.json()["dependencies"] == {"postgres": "up", "redis": "up"}


async def test_it_follows_the_api_prefix(settings: Settings) -> None:
    prefixed = build_settings(api={"prefix": "/v2"})
    app = _app(prefixed, _Estate())

    assert (await _get(app, "/v2/health")).status_code == 200
    assert (await _get(app, "/v1/health")).status_code == 404


# ── the outside probes: cheap, cached, bounded ──────────────────────────────


async def test_outside_services_are_probed_once_per_cache_window() -> None:
    estate, clock = _Estate(), _Clock()
    service = _service(estate, cache=OutsideHealthCache(clock))

    await service.check()
    clock.now = 59.0
    await service.check()

    assert estate.llm.calls == 1
    assert estate.pinecone is not None and estate.pinecone.calls == 1
    assert estate.finder is not None and estate.finder.calls == 1
    # PostgreSQL and Redis are local and cheap: always this moment's answer.
    assert estate.postgres.calls == 2
    assert estate.redis.calls == 2


async def test_a_stale_window_probes_again_and_sees_the_change() -> None:
    estate, clock = _Estate(), _Clock()
    service = _service(estate, cache=OutsideHealthCache(clock))
    first = await service.check()

    estate.llm.healthy = False
    clock.now = 60.0
    second = await service.check()

    assert estate.llm.calls == 2
    assert first.status is OverallStatus.OK
    assert second.status is OverallStatus.DOWN
    assert second.dependencies["llm"] is ServiceStatus.DOWN


async def test_a_service_seen_down_is_asked_again_within_seconds() -> None:
    """One slow first connection must not read as an outage for a minute."""
    estate, clock = _Estate(), _Clock()
    estate.llm.healthy = False
    service = _service(estate, cache=OutsideHealthCache(clock))
    first = await service.check()

    estate.llm.healthy = True
    clock.now = 9.0
    still_cached = await service.check()
    clock.now = 10.0
    recovered = await service.check()

    assert first.status is OverallStatus.DOWN
    assert still_cached.status is OverallStatus.DOWN
    assert recovered.status is OverallStatus.OK
    assert estate.llm.calls == 2


async def test_probes_arriving_together_share_one_round() -> None:
    estate = _Estate()
    estate.llm.delay_s = 0.02
    service = _service(estate)

    reports = await asyncio.gather(*(service.check() for _ in range(5)))

    assert estate.llm.calls == 1
    assert all(r.status is OverallStatus.OK for r in reports)


async def test_a_slow_probe_counts_as_down() -> None:
    estate = _Estate()
    estate.llm.delay_s = 1.0
    service = _service(estate, settings=HealthSettings(outside_timeout_s=0.01))

    report = await service.check()

    assert report.dependencies["llm"] is ServiceStatus.DOWN
    assert report.status is OverallStatus.DOWN


def test_health_settings_are_bounded() -> None:
    assert HealthSettings().outside_cache_s == 60.0
    assert HealthSettings().outside_down_cache_s == 10.0
    assert HealthSettings().outside_timeout_s == 10.0
    with pytest.raises(ValidationError):
        HealthSettings(outside_cache_s=0)
    with pytest.raises(ValidationError):
        HealthSettings(outside_down_cache_s=0)
    with pytest.raises(ValidationError):
        HealthSettings(outside_timeout_s=-1)


# ── each probe, on the wire ─────────────────────────────────────────────────


async def test_the_finder_probe_calls_the_platforms_open_health_check() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "OK"})

    client = ZoryFinderClient(finder_settings(), transport=httpx.MockTransport(handler))
    try:
        assert await client.check_health() is True
    finally:
        await client.close()

    assert [(r.method, r.url.path) for r in seen] == [("GET", HEALTH_PATH)]
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_the_finder_probe_reads_a_refusal_as_down(status: int) -> None:
    client = ZoryFinderClient(
        finder_settings(), transport=httpx.MockTransport(lambda _: httpx.Response(status))
    )
    try:
        assert await client.check_health() is False
    finally:
        await client.close()


async def test_the_finder_probe_reads_no_connection_as_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = ZoryFinderClient(finder_settings(), transport=httpx.MockTransport(handler))
    try:
        assert await client.check_health() is False
    finally:
        await client.close()


def _llm(handler: Any) -> tuple[OpenAIStructuredClient, AsyncOpenAI]:
    client = OpenAIStructuredClient(
        LLMSettings(api_key=SecretStr("test-key-not-real"), model="test-model", max_retries=3)
    )
    wire = AsyncOpenAI(
        api_key="test-key-not-real",
        base_url="https://llm.test/v1",
        max_retries=3,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client._client = wire
    return client, wire


async def test_the_llm_probe_looks_up_the_configured_model_and_generates_nothing() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"id": "test-model", "object": "model", "created": 0, "owned_by": "x"}
        )

    client, wire = _llm(handler)
    try:
        assert await client.check_health() is True
    finally:
        await wire.close()

    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/models/test-model")]


@pytest.mark.parametrize("status", [401, 404, 500])
async def test_the_llm_probe_is_down_at_once_without_retrying(status: int) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"error": {"message": "no"}})

    client, wire = _llm(handler)
    try:
        assert await client.check_health() is False
    finally:
        await wire.close()

    assert len(seen) == 1


class _IndexHandle:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def describe_index_stats(self) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"total_vector_count": 0}


def _index(handle: _IndexHandle) -> PineconeSemanticIndex:
    index = object.__new__(PineconeSemanticIndex)
    index._index = handle  # type: ignore[assignment, unused-ignore]
    index._index_name = "ai-agent"
    return index


async def test_the_pinecone_probe_reads_the_index_stats() -> None:
    handle = _IndexHandle()

    assert await _index(handle).check_health() is True
    assert handle.calls == 1


async def test_the_pinecone_probe_reads_an_error_as_down() -> None:
    assert await _index(_IndexHandle(RuntimeError("unauthorized"))).check_health() is False
