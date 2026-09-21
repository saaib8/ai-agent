"""ZORY AI Commerce Agent — application factory.

Run: ``uvicorn app.main:create_app --factory``

There is deliberately no module-level ``app``: building it would read settings
at import time, so merely importing this module would require a fully
configured environment.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.middleware import TraceContextMiddleware
from app.api.routes.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.lifespan import lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    # Interactive docs publish the whole API surface; off unless asked for.
    docs_on = settings.api.enable_docs
    app = FastAPI(
        title="ZORY AI Commerce Agent",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_on else None,
        redoc_url="/redoc" if docs_on else None,
        openapi_url="/openapi.json" if docs_on else None,
    )

    register_exception_handlers(app)

    # Added first so CORS (added last) wraps it, and so error responses still
    # carry the trace header.
    app.add_middleware(
        TraceContextMiddleware, header=settings.observability.trace_header
    )
    if settings.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.api.cors_origins),
            # Auth is not cookie-based, so credentialed CORS is never needed.
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
            expose_headers=[settings.observability.trace_header],
        )

    # Health sits outside the versioned prefix so probes are stable across
    # API versions (CLAUDE.md 25).
    app.include_router(health_router)
    return app
