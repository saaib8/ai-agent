"""The health endpoint reports readiness without describing the estate."""

from __future__ import annotations

from typing import cast

import pytest
from app.api import dependencies as deps
from app.core.config import Settings
from app.core.lifespan import AppResources
from app.integrations.llm import OpenAIStructuredClient
from app.integrations.postgres import Database
from app.integrations.redis import RedisClient
from app.main import create_app
from app.services.health import HealthService
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


class _Stub:
    def __init__(self, healthy: bool) -> None:
        self._healthy = healthy

    async def check_health(self) -> bool:
        return self._healthy


def _app(settings: Settings, *, postgres_up: bool, redis_up: bool) -> FastAPI:
    application = create_app(settings)
    database = cast(Database, _Stub(postgres_up))
    redis_client = cast(RedisClient, _Stub(redis_up))

    application.dependency_overrides[deps.resources] = lambda: AppResources(
        settings=settings,
        database=database,
        redis=redis_client,
        llm=cast(OpenAIStructuredClient, _Stub(True)),
        taxonomy=load_taxonomy(),
        attributes=load_catalog_attributes(),
        dimensions=load_dimension_semantics(taxonomy=load_taxonomy()),
    )
    application.dependency_overrides[deps.health_service] = lambda: HealthService(
        database, redis_client
    )
    return application


async def _get_health(app: FastAPI) -> tuple[int, dict[str, object], dict[str, str]]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    return response.status_code, response.json(), dict(response.headers)


async def test_all_dependencies_up_reports_ok(settings: Settings) -> None:
    status, body, _ = await _get_health(_app(settings, postgres_up=True, redis_up=True))

    assert status == 200
    assert body["status"] == "ok"
    assert body["dependencies"] == {"postgres": "up", "redis": "up"}
    assert body["service"] == "zory-agent"
    assert body["environment"] == "test"


@pytest.mark.parametrize(
    ("postgres_up", "redis_up", "expected"),
    [
        (False, True, {"postgres": "down", "redis": "up"}),
        (True, False, {"postgres": "up", "redis": "down"}),
        (False, False, {"postgres": "down", "redis": "down"}),
    ],
)
async def test_a_failing_dependency_degrades_the_service(
    settings: Settings, postgres_up: bool, redis_up: bool, expected: dict[str, str]
) -> None:
    status, body, _ = await _get_health(
        _app(settings, postgres_up=postgres_up, redis_up=redis_up)
    )

    assert status == 503
    assert body["status"] == "degraded"
    assert body["dependencies"] == expected


async def test_health_describes_no_infrastructure(settings: Settings) -> None:
    """Status only: no DSN, host, port or driver detail."""
    _, body, _ = await _get_health(_app(settings, postgres_up=False, redis_up=False))

    rendered = str(body)
    assert "localhost" not in rendered
    assert "5432" not in rendered
    assert "asyncpg" not in rendered
    assert "zory:zory" not in rendered


async def test_health_carries_a_trace_header(settings: Settings) -> None:
    _, _, headers = await _get_health(_app(settings, postgres_up=True, redis_up=True))
    assert headers["x-request-id"]


async def test_docs_are_off_unless_enabled(settings: Settings) -> None:
    app = _app(settings, postgres_up=True, redis_up=True)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/openapi.json")).status_code == 404
