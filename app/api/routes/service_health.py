"""The public health check: `GET /v1/health`.

The unversioned `/health` is the container's own readiness probe, and the
public host proxies only the API prefix, so it cannot be reached from outside.
This one can: it is for uptime monitors and the platform. It answers 503 only
when no chat can be answered, `degraded` with 200 when a feature is down.

Transport only. Like `/health` it names which dependency is down and never why
- no host, URL, index name or driver error leaves the service (CLAUDE.md 20.5).
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict

from app.api.dependencies import ResourcesDep, ServiceHealthServiceDep
from app.services.service_health import OverallStatus, ServiceStatus

router = APIRouter(tags=["health"])


class ServiceHealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: OverallStatus
    service: str
    environment: str
    dependencies: dict[str, ServiceStatus]


@router.get("/health", response_model=ServiceHealthResponse)
async def service_health(
    service: ServiceHealthServiceDep, app_resources: ResourcesDep, response: Response
) -> ServiceHealthResponse:
    report = await service.check()
    if report.status is OverallStatus.DOWN:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
    # A monitor must see this moment's answer, never a proxy's copy.
    response.headers["Cache-Control"] = "no-store"
    return ServiceHealthResponse(
        status=report.status,
        service=app_resources.settings.service_name,
        environment=str(app_resources.settings.environment),
        dependencies=report.dependencies,
    )
