"""会话管理 API 路由。

实现 P3-1 四个接口：
- POST /api/v1/consult/sessions
- GET  /api/v1/consult/sessions
- GET  /api/v1/consult/sessions/{session_id}
- POST /api/v1/consult/sessions/{session_id}/terminate

本模块不实现消息、锁、SSE、Agent、RAG、安全审核或病历生成。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InvalidStageTransitionError,
    SessionNotFoundError,
)
from app.core.exceptions import (
    ValidationError as XuanhuValidationError,
)
from app.db.session import get_db
from app.schemas.common import success_response
from app.schemas.session import (
    SessionCreateRequest,
    SessionTerminateRequest,
)
from app.services.session import SessionService

router = APIRouter(prefix="/api/v1/consult", tags=["sessions"])


def _get_trace_id(request: Request) -> str:
    """获取或生成 trace_id。

    优先复用请求头 X-Request-Id / X-Trace-Id；否则生成 UUID v4。
    """
    header = request.headers.get("x-request-id") or request.headers.get("x-trace-id")
    if header:
        return header
    return str(uuid.uuid4())


def _doctor_id(x_doctor_id: str | None = Header(default=None, alias="X-Doctor-Id")) -> str | None:
    """读取医师标识请求头（MVP 可选）。"""
    return x_doctor_id or None


@router.post("/sessions", status_code=201)
async def create_session(
    request: Request,
    body: SessionCreateRequest,
    db: AsyncSession = Depends(get_db),
    doctor_id: str | None = Depends(_doctor_id),
) -> JSONResponse:
    """创建问诊会话。"""
    trace_id = _get_trace_id(request)
    service = SessionService(db)
    data = await service.create_session(body, doctor_id=doctor_id, trace_id=trace_id)
    return JSONResponse(
        status_code=201,
        content=success_response(data=data.model_dump(mode="json"), trace_id=trace_id),
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
    )
    return JSONResponse(
        status_code=200,
        content=success_response(data=data.model_dump(mode="json"), trace_id=trace_id),
    )


@router.get("/sessions/{session_id}")
async def get_session(
    request: Request,
    session_id: str,
    db: AsyncSession = Depends(get_db),
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
    session_id: str,
    body: SessionTerminateRequest,
    db: AsyncSession = Depends(get_db),
    doctor_id: str | None = Depends(_doctor_id),
) -> JSONResponse:
    """终止会话。"""
    trace_id = _get_trace_id(request)
    service = SessionService(db)
    data = await service.terminate_session(
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
            message="会话已终止",
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
            "detail": exc.detail,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


async def invalid_stage_transition_handler(
    request: Request, exc: InvalidStageTransitionError
) -> JSONResponse:
    """INVALID_STAGE_TRANSITION 异常处理。"""
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


async def validation_error_handler(
    request: Request, exc: XuanhuValidationError
) -> JSONResponse:
    """VALIDATION_ERROR 异常处理。"""
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
session_exception_handlers: dict[Any, Any] = {
    SessionNotFoundError: session_not_found_handler,
    InvalidStageTransitionError: invalid_stage_transition_handler,
    XuanhuValidationError: validation_error_handler,
}
