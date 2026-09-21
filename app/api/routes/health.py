"""Liveness/readiness endpoint.

Transport only: it calls the health service and maps the report onto a status
code. It reports *which* dependency is down but never why — connection strings,
hostnames and driver errors stay in the logs (CLAUDE.md 20.5).
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict

from app.api.dependencies import HealthServiceDep, ResourcesDep
from app.services.health import DependencyStatus

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    service: str
    environment: str
    dependencies: dict[str, DependencyStatus]


@router.get("/health", response_model=HealthResponse)
async def health(
    service: HealthServiceDep, app_resources: ResourcesDep, response: Response
) -> HealthResponse:
    report = await service.check()
    if not report.healthy:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if report.healthy else "degraded",
        service=app_resources.settings.service_name,
        environment=str(app_resources.settings.environment),
        dependencies=report.dependencies,
    )
