"""会话恢复 API 路由。

实现 P3-4 接口：
- POST /api/v1/consult/sessions/{session_id}/recover

本模块不实现 Agent、RAG、安全审核、医师确认或病历生成。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    RecoveryNotNeededError,
    SessionBusyError,
    SessionNotFoundError,
    StateRecoveryRequiredError,
    ValidationError,
)
from app.db.session import get_db
from app.schemas.common import success_response
from app.schemas.recovery import RecoveryRequest
from app.services.recovery import RecoveryService

router = APIRouter(prefix="/api/v1/consult", tags=["recovery"])


def _get_trace_id(request: Request) -> str:
    """获取或生成 trace_id。"""
    header = request.headers.get("x-request-id") or request.headers.get("x-trace-id")
    if header:
        return header
    return str(uuid.uuid4())


def _doctor_id(x_doctor_id: str | None = Header(default=None, alias="X-Doctor-Id")) -> str | None:
    """读取医师标识请求头（MVP 可选）。"""
    return x_doctor_id or None


@router.post("/sessions/{session_id}/recover")
async def recover_session(
    request: Request,
    session_id: str,
    body: RecoveryRequest,
    db: AsyncSession = Depends(get_db),
    doctor_id: str | None = Depends(_doctor_id),
) -> JSONResponse:
    """恢复中断的会话。

    支持四种 action：
    - resume_from_pg_snapshot: 从 PG state_snapshot 恢复
    - retry_current_stage: 保持 current_stage，重置 recovery_status
    - rollback_to_stage: 回退到指定阶段
    - terminate: 终止会话

    每次成功恢复或终止均写入 audit_events。
    """
    trace_id = _get_trace_id(request)
    service = RecoveryService(db)
    data = await service.recover(
        session_id,
        body,
        doctor_id=doctor_id,
        trace_id=trace_id,
    )
    return JSONResponse(
        status_code=200,
        content=success_response(
            data=data.model_dump(mode="json"),
            trace_id=trace_id,
        ),
    )


# ---------------------------------------------------------------------------
# 本地异常转换
# ---------------------------------------------------------------------------


async def recovery_session_not_found_handler(
    request: Request, exc: SessionNotFoundError
) -> JSONResponse:
    """SESSION_NOT_FOUND 异常处理。"""
    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": exc.detail,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


async def recovery_not_needed_handler(
    request: Request, exc: RecoveryNotNeededError
) -> JSONResponse:
    """RECOVERY_NOT_NEEDED 异常处理。"""
    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": exc.detail,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


async def state_recovery_required_handler(
    request: Request, exc: StateRecoveryRequiredError
) -> JSONResponse:
    """STATE_RECOVERY_REQUIRED 异常处理。"""
    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": exc.detail,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


async def recovery_validation_error_handler(
    request: Request, exc: ValidationError
) -> JSONResponse:
    """VALIDATION_ERROR 异常处理（recovery 路由专用）。"""
    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": exc.detail,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


async def recovery_session_busy_handler(
    request: Request, exc: SessionBusyError
) -> JSONResponse:
    """SESSION_BUSY 异常处理（recover 会话锁冲突）。

    响应格式与 P3-2 消息接口保持一致。
    """
    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": exc.detail,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


# 导出路由级异常处理映射，供 main.py 注册
recovery_exception_handlers: dict[Any, Any] = {
    SessionNotFoundError: recovery_session_not_found_handler,
    RecoveryNotNeededError: recovery_not_needed_handler,
    StateRecoveryRequiredError: state_recovery_required_handler,
    ValidationError: recovery_validation_error_handler,
    SessionBusyError: recovery_session_busy_handler,
}
