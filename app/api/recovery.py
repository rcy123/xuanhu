"""会话恢复 API 路由。

实现 P3-4 接口：
- POST /api/v1/consult/sessions/{session_id}/recover

本模块不实现 Agent、RAG、安全审核、医师确认或病历生成。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.request_context import (
    WriteRequestContext,
    execute_model_write,
    get_trace_id,
    write_request_context,
)
from app.core.exceptions import (
    LangGraphRecoveryNotImplementedError,
    RecoveryNotNeededError,
    SessionBusyError,
    SessionNotFoundError,
    StateRecoveryRequiredError,
    ValidationError,
)
from app.db.session import get_db
from app.schemas.common import success_response
from app.schemas.recovery import RecoveryRequest
from app.services.http_idempotency import session_http_scope
from app.services.recovery_dispatcher import RecoveryDispatcher

router = APIRouter(prefix="/api/v1/consult", tags=["recovery"])


def _get_trace_id(request: Request) -> str:
    """获取或生成 trace_id。"""
    return get_trace_id(request)


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
    context: WriteRequestContext = Depends(write_request_context),
) -> JSONResponse:
    """恢复中断的会话。

    支持四种 action：
    - resume_from_pg_snapshot: 从 PG state_snapshot 恢复
    - retry_current_stage: 保持 current_stage，重置 recovery_status
    - rollback_to_stage: 回退到指定阶段
    - terminate: 终止会话

    每次成功恢复或终止均写入 audit_events。
    """
    del request
    trace_id = context.trace_id
    service = RecoveryDispatcher(db)
    scope = session_http_scope(session_id)
    result = await execute_model_write(
        db,
        context,
        operation="session.recover.v1",
        scope_key=scope,
        concurrency_scope=scope,
        request_payload={
            "body": body.model_dump(mode="json"),
            "doctor_id": doctor_id,
        },
        success_status=200,
        success_message="ok",
        handler=lambda: service.recover(
            session_id,
            body,
            doctor_id=doctor_id,
            trace_id=trace_id,
        ),
    )
    return JSONResponse(
        status_code=result.status_code,
        content=success_response(
            data=result.data,
            trace_id=trace_id,
            message=result.message,
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


async def langgraph_recovery_not_implemented_handler(
    request: Request, exc: LangGraphRecoveryNotImplementedError
) -> JSONResponse:
    """LangGraph 恢复未实现时 fail closed，不允许回退到 Legacy。"""
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
    LangGraphRecoveryNotImplementedError: langgraph_recovery_not_implemented_handler,
    ValidationError: recovery_validation_error_handler,
    SessionBusyError: recovery_session_busy_handler,
}
