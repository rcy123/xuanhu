"""病历 API 路由。

P7-3 实现：
- GET  /api/v1/consult/sessions/{session_id}/record?version=latest
- PUT  /api/v1/consult/sessions/{session_id}/record
- GET  /api/v1/consult/sessions/{session_id}/record/export?format=txt|json|md&version=latest
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InvalidStageTransitionError,
    InvalidStateVersionError,
    SessionBusyError,
    SessionNotFoundError,
    SessionTerminatedError,
)
from app.core.exceptions import (
    ValidationError as XuanhuValidationError,
)
from app.db.session import get_db
from app.schemas.common import success_response
from app.schemas.record import RecordUpdateRequest
from app.services.record_service import (
    ExportFormatUnsupportedError,
    RecordNotFoundError,
    RecordService,
)

router = APIRouter(prefix="/api/v1/consult", tags=["record"])


def _get_trace_id(request: Request) -> str:
    """获取或生成 trace_id。"""
    header = request.headers.get("x-request-id") or request.headers.get("x-trace-id")
    if header:
        return header
    return str(uuid.uuid4())


def _doctor_id(
    x_doctor_id: str | None = Header(default=None, alias="X-Doctor-Id"),
) -> str | None:
    """读取医师标识请求头（MVP 可选）。"""
    return x_doctor_id or None


def _state_version(
    x_state_version: str | None = Header(default=None, alias="X-State-Version"),
) -> int | None:
    """读取客户端 state_version。"""
    if x_state_version is None:
        return None
    try:
        return int(x_state_version)
    except ValueError as err:
        raise XuanhuValidationError(
            message=f"X-State-Version 必须为整数，收到: {x_state_version}",
            detail=f"X-State-Version header 值 '{x_state_version}' 无法解析为整数",
            retryable=False,
        ) from err


# ---------------------------------------------------------------------------
# GET /record
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/record")
async def get_record(
    request: Request,
    session_id: str,
    version: str | None = Query(
        default=None,
        description="版本号（正整数）或 'latest'，默认 latest",
    ),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """获取病历（latest 或指定 version）。

    Returns:
        标准 envelope，data 包含 RecordResponse 字段。
    """
    trace_id = _get_trace_id(request)
    service = RecordService(db)
    data = await service.get_record(
        session_id,
        version=version,
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
# PUT /record
# ---------------------------------------------------------------------------


@router.put("/sessions/{session_id}/record")
async def update_record(
    request: Request,
    session_id: str,
    body: RecordUpdateRequest,
    db: AsyncSession = Depends(get_db),
    doctor_id: str | None = Depends(_doctor_id),
    state_version: int | None = Depends(_state_version),
) -> JSONResponse:
    """医师编辑病历，新增版本不覆盖旧版本。

    仅允许 current_stage 为 record 或 done 的会话编辑。
    """
    trace_id = _get_trace_id(request)
    service = RecordService(db)
    data = await service.update_record(
        session_id,
        body,
        doctor_id=doctor_id,
        trace_id=trace_id,
        x_state_version=state_version,
    )
    return JSONResponse(
        status_code=200,
        content=success_response(
            data=data.model_dump(mode="json"),
            trace_id=trace_id,
        ),
    )


# ---------------------------------------------------------------------------
# GET /record/export
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/record/export")
async def export_record(
    request: Request,
    session_id: str,
    format: str = Query(
        ...,  # 必填
        description="导出格式: txt / json / md",
    ),
    version: str | None = Query(
        default=None,
        description="版本号（正整数）或 'latest'，默认 latest",
    ),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """导出病历为指定格式。

    不使用通用 envelope，直接返回文件响应。
    Content-Disposition 含 ASCII fallback 和 RFC 5987 filename* 编码。
    """
    trace_id = _get_trace_id(request)
    service = RecordService(db)
    content, content_type, filename = await service.export_record(
        session_id,
        format=format,
        version=version,
        trace_id=trace_id,
    )

    # RFC 5987: 同时提供 ASCII fallback 和 UTF-8 编码的文件名
    # ASCII fallback：使用 medical_record_ 前缀 + 扩展名
    ascii_fallback = f"medical_record.{format}"
    encoded_filename = quote(filename)

    content_disposition = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{encoded_filename}"
    )

    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": content_disposition,
            "X-Trace-Id": trace_id,
        },
    )


# ---------------------------------------------------------------------------
# 异常处理器
# ---------------------------------------------------------------------------


async def record_not_found_handler(
    request: Request, exc: RecordNotFoundError
) -> JSONResponse:
    """RECORD_NOT_FOUND 异常处理。"""
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


async def export_format_unsupported_handler(
    request: Request, exc: ExportFormatUnsupportedError
) -> JSONResponse:
    """EXPORT_FORMAT_UNSUPPORTED 异常处理。"""
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


async def record_session_not_found_handler(
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


async def record_invalid_stage_handler(
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


async def record_invalid_state_version_handler(
    request: Request, exc: InvalidStateVersionError
) -> JSONResponse:
    """INVALID_STATE_VERSION 异常处理。"""
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


async def record_session_busy_handler(
    request: Request, exc: SessionBusyError
) -> JSONResponse:
    """SESSION_BUSY 异常处理。"""
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


async def record_session_terminated_handler(
    request: Request, exc: SessionTerminatedError
) -> JSONResponse:
    """SESSION_TERMINATED 异常处理。"""
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


async def record_validation_error_handler(
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
record_exception_handlers: dict[Any, Any] = {
    RecordNotFoundError: record_not_found_handler,
    ExportFormatUnsupportedError: export_format_unsupported_handler,
    SessionNotFoundError: record_session_not_found_handler,
    InvalidStageTransitionError: record_invalid_stage_handler,
    InvalidStateVersionError: record_invalid_state_version_handler,
    SessionBusyError: record_session_busy_handler,
    SessionTerminatedError: record_session_terminated_handler,
    XuanhuValidationError: record_validation_error_handler,
}
