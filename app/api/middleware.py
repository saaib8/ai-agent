"""Request-scoped trace context.

Every request gets a trace id — reused from the configured inbound header when
a caller supplies one, generated otherwise — bound to the logging context for
the life of the request and echoed back on the response, so a client report can
be matched to server logs (CLAUDE.md 22).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import bind_request_context, clear_request_context, new_trace_id

# Inbound trace ids are attacker-controlled, so they are length-capped and
# stripped before being bound to the logging context.
_MAX_TRACE_ID_CHARS = 64


class TraceContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Callable[..., Awaitable[None]], *, header: str) -> None:
        super().__init__(app)
        self._header = header

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied = (request.headers.get(self._header) or "").strip()
        trace_id = supplied[:_MAX_TRACE_ID_CHARS] if supplied else new_trace_id()

        clear_request_context()
        bind_request_context(trace_id=trace_id)
        request.state.trace_id = trace_id
        # An unhandled exception unwinds past this middleware, so the error
        # handlers stamp the header themselves; they read its name from here.
        request.state.trace_header = self._header
        try:
            response = await call_next(request)
        finally:
            clear_request_context()
        response.headers[self._header] = trace_id
        return response
