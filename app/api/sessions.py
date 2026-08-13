"""会话管理 API 路由。

实现 P3-1 四个接口：
- POST /api/v1/consult/sessions
- GET  /api/v1/consult/sessions
- GET  /api/v1/consult/sessions/{session_id}
- POST /api/v1/consult/sessions/{session_id}/terminate

LangGraph 创建会话时会在同一幂等事务内生成模板首问；本模块本身不
编排消息、锁、SSE、RAG、安全审核或病历生成。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.request_context import (
    WriteRequestContext,
    execute_model_write,
    get_trace_id,
    validate_session_id,
    write_request_context,
)
from app.core.access import require_session_owner, require_session_reader
from app.core.auth import DoctorPrincipal, get_current_doctor
from app.core.config import get_settings
from app.core.exceptions import (
    InvalidStageTransitionError,
    LangGraphPublicDisabledError,
    LegacyRuntimeCreationDisabledError,
    RuntimeRolloutNotReadyError,
    RuntimeSwitchAuditMismatchError,
    SessionNotFoundError,
)
from app.core.exceptions import (
    ValidationError as XuanhuValidationError,
)
from app.core.ratelimit import require_write_rate_limit
from app.db.session import get_db
from app.schemas.common import success_response
from app.schemas.session import (
    SessionCreateRequest,
    SessionTerminateRequest,
)
from app.services.http_idempotency import session_http_scope
from app.services.runtime_rollout import select_new_session_runtime
from app.services.session import SessionService

router = APIRouter(prefix="/api/v1/consult", tags=["sessions"])


def _get_trace_id(request: Request) -> str:
    """获取或生成 trace_id。

    优先复用请求头 X-Request-Id / X-Trace-Id；否则生成 UUID v4。
    """
    return get_trace_id(request)


@router.post("/sessions", status_code=201)
async def create_session(
    request: Request,
    body: SessionCreateRequest,
    db: AsyncSession = Depends(get_db),
    doctor: DoctorPrincipal = Depends(get_current_doctor),
    context: WriteRequestContext = Depends(write_request_context),
) -> JSONResponse:
    """创建问诊会话。"""
    del request
    trace_id = context.trace_id
    settings = get_settings()
    public_runtime = select_new_session_runtime(settings, body.agent_runtime)
    if public_runtime == "langgraph" and not settings.langgraph_public_enabled:
        raise LangGraphPublicDisabledError(
            detail=(
                "agent_runtime=langgraph 的公共会话创建未启用；"
                "请使用 legacy 或由运维启用 XUANHU_LANGGRAPH_PUBLIC_ENABLED"
            )
        )
    service = SessionService(db)
    result = await execute_model_write(
        db,
        context,
        operation="session.create.v1",
        scope_key="sessions",
        concurrency_scope=None,
        request_payload={
            "body": body.model_dump(mode="json"),
            "doctor_id": doctor.doctor_id,
        },
        success_status=201,
        success_message="ok",
        handler=lambda: service.create_session(
            body,
            doctor_id=doctor.doctor_id,
            trace_id=trace_id,
            require_runtime_audit=(settings.agent_runtime_rollout_phase in {"full", "rollback"}),
        ),
    )
    return JSONResponse(
        status_code=result.status_code,
        content=success_response(data=result.data, trace_id=trace_id, message=result.message),
    )


@router.get("/sessions")
async def list_sessions(
    request: Request,
    status: str | None = Query(
        default=None,
        pattern=r"^(active|pending_review|done|blocked|terminated)$",
    ),
    patient_ref: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort: str = Query(default="created_at:desc", pattern=r"^(created_at|updated_at):desc$"),
    db: AsyncSession = Depends(get_db),
    doctor: DoctorPrincipal = Depends(get_current_doctor),
) -> JSONResponse:
    """查询会话列表。"""
    trace_id = _get_trace_id(request)
    service = SessionService(db)
    data = await service.list_sessions(
        status=status,
        patient_ref=patient_ref,
        page=page,
        page_size=page_size,
        sort=sort,
        doctor_id=doctor.doctor_id,
    )
    return JSONResponse(
        status_code=200,
        content=success_response(data=data.model_dump(mode="json"), trace_id=trace_id),
    )


@router.get("/sessions/{session_id}")
async def get_session(
    request: Request,
    session_id: str = Depends(validate_session_id),
    _: None = Depends(require_session_reader),
    db: AsyncSession = Depends(get_db),
    doctor: DoctorPrincipal = Depends(get_current_doctor),
) -> JSONResponse:
    """获取会话详情。"""
    trace_id = _get_trace_id(request)
    service = SessionService(db)
    data = await service.get_session(session_id, trace_id=trace_id)
    return JSONResponse(
        status_code=200,
        content=success_response(data=data.model_dump(mode="json"), trace_id=trace_id),
    )


@router.post("/sessions/{session_id}/terminate")
async def terminate_session(
    request: Request,
    body: SessionTerminateRequest,
    session_id: str = Depends(validate_session_id),
    _rl: None = Depends(require_write_rate_limit),
    _: None = Depends(require_session_owner),
    db: AsyncSession = Depends(get_db),
    doctor: DoctorPrincipal = Depends(get_current_doctor),
    context: WriteRequestContext = Depends(write_request_context),
) -> JSONResponse:
    """终止会话。"""
    del request
    trace_id = context.trace_id
    service = SessionService(db)
    scope = session_http_scope(session_id)
    result = await execute_model_write(
        db,
        context,
        operation="session.terminate.v1",
        scope_key=scope,
        concurrency_scope=scope,
        request_payload={
            "body": body.model_dump(mode="json"),
            "doctor_id": doctor.doctor_id,
        },
        success_status=200,
        success_message="会话已终止",
        handler=lambda: service.terminate_session(
            session_id,
            body,
            doctor_id=doctor.doctor_id,
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
# 本地异常转换：将 service 层抛出的业务异常转换为标准 envelope
# ---------------------------------------------------------------------------


async def session_not_found_handler(request: Request, exc: SessionNotFoundError) -> JSONResponse:
    """SESSION_NOT_FOUND 异常处理。"""
    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": None,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


async def invalid_stage_transition_handler(request: Request, exc: InvalidStageTransitionError) -> JSONResponse:
    """INVALID_STAGE_TRANSITION 异常处理。"""
    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": None,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


async def validation_error_handler(request: Request, exc: XuanhuValidationError) -> JSONResponse:
    """VALIDATION_ERROR 异常处理。"""
    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": None,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


async def langgraph_public_disabled_handler(request: Request, exc: LangGraphPublicDisabledError) -> JSONResponse:
    """LANGGRAPH_PUBLIC_DISABLED 异常处理。"""
    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": None,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


async def runtime_switch_audit_mismatch_handler(request: Request, exc: RuntimeSwitchAuditMismatchError) -> JSONResponse:
    """Render a sanitized fail-closed deployment-audit mismatch."""

    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": None,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


async def runtime_rollout_not_ready_handler(request: Request, exc: RuntimeRolloutNotReadyError) -> JSONResponse:
    """Render a sanitized fail-closed rollout policy error."""

    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": None,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


async def legacy_runtime_creation_disabled_handler(
    request: Request, exc: LegacyRuntimeCreationDisabledError
) -> JSONResponse:
    """Reject new Legacy sessions after full cutover without touching old ones."""

    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": None,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


# 导出路由级异常处理映射，供 main.py 注册
session_exception_handlers: dict[Any, Any] = {
    SessionNotFoundError: session_not_found_handler,
    InvalidStageTransitionError: invalid_stage_transition_handler,
    LangGraphPublicDisabledError: langgraph_public_disabled_handler,
    RuntimeSwitchAuditMismatchError: runtime_switch_audit_mismatch_handler,
    RuntimeRolloutNotReadyError: runtime_rollout_not_ready_handler,
    LegacyRuntimeCreationDisabledError: legacy_runtime_creation_disabled_handler,
    XuanhuValidationError: validation_error_handler,
}
