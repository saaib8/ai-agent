"""Wire format for error responses.

Deliberately minimal: a stable machine code, a message we authored, and the
trace id so a report can be tied to the logs that explain it. Nothing else
crosses the boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ErrorBody(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    trace_id: str | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    error: ErrorBody
