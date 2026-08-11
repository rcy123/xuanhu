"""Public read-only async-command status API (R6-A).

``GET /api/v1/consult/sessions/{session_id}/commands/{command_id}`` returns a
typed, privacy-safe status envelope. Lookups are session-scoped: a command that
does not belong to the session is indistinguishable from one that does not
exist, so no cross-session disclosure occurs. Queued/running are returned at
HTTP 200 (status queries, not 202 acceptances).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agent_runtime.async_command import (
    AsyncCommandStatus as RepoCommandStatus,
)
from app.agent_runtime.async_command import PostgresAsyncCommandRepository
from app.core.exceptions import SessionNotFoundError, XuanhuError
from app.db.session import get_session_factory
from app.schemas.async_command import (
    AsyncCommandStatus,
    CommandErrorInfo,
    CommandLinks,
    CommandResultInfo,
    CommandTimestamps,
)
from app.schemas.common import success_response

router = APIRouter(prefix="/api/v1/consult", tags=["commands"])


class CommandNotFoundError(XuanhuError):
    """Command not found in this session (or belongs to another session)."""

    code = "COMMAND_NOT_FOUND"
    message = "命令不存在"
    status_code = 404
    retryable = False


def _request_trace_id(request: Request) -> str:
    return (
        request.headers.get("x-request-id")
        or request.headers.get("x-trace-id")
        or str(uuid.uuid4())
    )


@router.get("/sessions/{session_id}/commands/{command_id}")
async def get_command_status(
    request: Request,
    session_id: str,
    command_id: str,
) -> JSONResponse:
    """Read one session-scoped command's public status."""
    trace_id = _request_trace_id(request)
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        # An invalid session identifier is a session-scoped lookup failure and
        # uses the existing SessionNotFoundError envelope.
        raise SessionNotFoundError(
            detail=f"session_id={session_id} 格式非法",
            retryable=False,
        ) from None
    try:
        cid = uuid.UUID(command_id)
    except ValueError:
        raise CommandNotFoundError(
            detail=f"command_id={command_id} 格式非法",
            retryable=False,
        ) from None

    repository = PostgresAsyncCommandRepository(get_session_factory())
    if not await repository.session_exists(sid):
        # A missing session is indistinguishable from an invalid one and uses
        # the same SessionNotFoundError envelope.
        raise SessionNotFoundError(
            detail=f"session_id={session_id} 在数据库中未找到",
            retryable=False,
        )
    status = await repository.get_status(sid, cid)
    if status is None:
        # Same 404 for missing command, malformed command identifier, and a
        # command that belongs to a different (existing) session.
        raise CommandNotFoundError(
            detail=f"session_id={session_id} command_id={command_id} 未找到",
            retryable=False,
        )

    body = _status_body(session_id, str(status.command_id), status)
    return JSONResponse(
        status_code=200,
        content=success_response(
            data=body.model_dump(mode="json"),
            trace_id=trace_id,
        ),
    )


def _status_body(
    session_id: str,
    command_id: str,
    status: RepoCommandStatus,
) -> AsyncCommandStatus:
    result = None
    if status.status == "succeeded":
        # Only the result HTTP status is public; the private result_payload DB
        # field is never projected.
        result = CommandResultInfo(http_status=status.result_http_status)
    error = None
    if status.status == "failed":
        # Only the fixed error code is public; the private error_payload DB
        # field is never projected.
        error = CommandErrorInfo(code=status.error_code)
    links = CommandLinks(
        self=f"/api/v1/consult/sessions/{session_id}/commands/{command_id}",
        session=f"/api/v1/consult/sessions/{session_id}",
        stream=f"/api/v1/consult/sessions/{session_id}/stream",
    )
    timestamps = CommandTimestamps(
        created_at=status.created_at,
        started_at=status.started_at,
        completed_at=status.completed_at,
        updated_at=status.updated_at,
    )
    return AsyncCommandStatus(
        command_id=status.command_id,
        operation=status.operation,
        status=status.status,
        attempt_count=status.attempt_count,
        result=result,
        error=error,
        timestamps=timestamps,
        links=links,
    )


async def command_not_found_handler(request: Request, exc: CommandNotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": exc.detail,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": _request_trace_id(request),
        },
    )


command_exception_handlers: dict[Any, Any] = {
    CommandNotFoundError: command_not_found_handler,
}


__all__ = ["router", "CommandNotFoundError", "command_exception_handlers"]
