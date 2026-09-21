"""Error responses must be safe: a code we chose and a message we wrote."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.api.errors import register_exception_handlers
from app.api.middleware import TraceContextMiddleware
from app.core.exceptions import StoreNotFoundError
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

TRACE_HEADER = "X-Request-ID"
SECRET_DETAIL = "password=hunter2 at /opt/zory/app/repositories/products.py:42"


class _Body(BaseModel):
    store_id: int


def _build_app() -> FastAPI:
    router = APIRouter()

    @router.get("/boom-domain")
    async def boom_domain() -> None:
        raise StoreNotFoundError(requested_store_id=999, internal_note=SECRET_DETAIL)

    @router.get("/boom-unexpected")
    async def boom_unexpected() -> None:
        raise RuntimeError(SECRET_DETAIL)

    @router.post("/echo")
    async def echo(body: _Body) -> dict[str, int]:
        return {"store_id": body.store_id}

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(TraceContextMiddleware, header=TRACE_HEADER)
    app.include_router(router)
    return app


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=_build_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def test_domain_error_maps_to_its_code_and_status(client: AsyncClient) -> None:
    response = await client.get("/boom-domain")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "store_not_found"
    assert body["error"]["message"] == "That store is not available."
    assert body["error"]["trace_id"]


async def test_domain_error_hides_its_internal_context(client: AsyncClient) -> None:
    response = await client.get("/boom-domain")
    error = response.json()["error"]

    # Assert on the fields we author, not the whole body: `trace_id` is random
    # hex and can contain any short digit sequence by chance.
    carried = f"{error['code']} {error['message']}"
    assert "hunter2" not in carried
    assert "999" not in carried
    assert "repositories/products.py" not in carried
    assert set(error) == {"code", "message", "trace_id"}


async def test_unexpected_error_becomes_a_generic_500(client: AsyncClient) -> None:
    response = await client.get("/boom-unexpected")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


async def test_validation_failure_does_not_echo_the_payload(client: AsyncClient) -> None:
    response = await client.post("/echo", json={"store_id": "'; DROP TABLE core_product--"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "DROP TABLE" not in response.text


@pytest.mark.parametrize("path", ["/boom-domain", "/boom-unexpected"])
async def test_every_response_carries_a_trace_header(client: AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.headers[TRACE_HEADER]
    assert response.json()["error"]["trace_id"] == response.headers[TRACE_HEADER]


async def test_a_supplied_trace_id_is_reused(client: AsyncClient) -> None:
    response = await client.get("/boom-domain", headers={TRACE_HEADER: "caller-trace-1"})
    assert response.headers[TRACE_HEADER] == "caller-trace-1"


async def test_an_oversized_trace_id_is_truncated(client: AsyncClient) -> None:
    response = await client.get("/boom-domain", headers={TRACE_HEADER: "x" * 500})
    assert len(response.headers[TRACE_HEADER]) == 64


async def test_unknown_route_returns_the_safe_error_shape(client: AsyncClient) -> None:
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
