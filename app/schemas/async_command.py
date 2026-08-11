"""Public read-only async-command status schema (R6-A).

Every field is intentionally privacy-safe: private request payload, request
digest, idempotency digest, owner/lease internals and any PHI are excluded by
construction at the repository boundary and cannot reach this envelope.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"


class CommandResultInfo(BaseModel):
    """Safe terminal success metadata (present only when succeeded).

    Only the result HTTP status is public. The private result_payload DB field
    (which may carry R6-B domain output / PHI) is never exposed here.
    """

    http_status: int | None = Field(default=None, ge=100, le=599)


class CommandErrorInfo(BaseModel):
    """Safe sanitized terminal failure metadata (present only when failed).

    Only the fixed error code is public. The error_payload DB field is a
    sanitized empty object in R6-A and is never exposed here.
    """

    code: str | None = Field(default=None, max_length=64)


class CommandTimestamps(BaseModel):
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None


class CommandLinks(BaseModel):
    """Stable self/session/stream links for client navigation."""

    self: str
    session: str
    stream: str


class AsyncCommandStatus(BaseModel):
    """Public status-query body returned at HTTP 200."""

    command_id: uuid.UUID
    operation: str
    status: str
    attempt_count: int = Field(ge=0)
    result: CommandResultInfo | None = None
    error: CommandErrorInfo | None = None
    timestamps: CommandTimestamps
    links: CommandLinks


__all__ = [
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_SUCCEEDED",
    "STATUS_FAILED",
    "CommandResultInfo",
    "CommandErrorInfo",
    "CommandTimestamps",
    "CommandLinks",
    "AsyncCommandStatus",
]
