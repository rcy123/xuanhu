"""医师确认 API 路由。

实现 P7-1 接口：
- POST /api/v1/consult/sessions/{session_id}/review

支持 confirm / modify / reject 三条路径。
本模块不实现病历生成、病历编辑或导出。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    FormulaOverrideRequiredError,
    InvalidReviewActionError,
    InvalidStageTransitionError,
    InvalidStateVersionError,
    SafetyAcceptRiskUnsupportedError,
    SafetyReviewBlockedError,
    SessionBusyError,
    SessionNotFoundError,
    SessionTerminatedError,
)
from app.core.exceptions import (
    ValidationError as XuanhuValidationError,
)
from app.db.session import get_db
from app.schemas.common import success_response
from app.schemas.review import ReviewRequest
from app.services.review import ReviewService

router = APIRouter(prefix="/api/v1/consult", tags=["review"])


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
    """读取客户端 state_version。

    非整数时直接抛出 ValidationError，阻止静默绕过版本校验。
    """
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


@router.post("/sessions/{session_id}/review")
async def review_prescription(
    request: Request,
    session_id: str,
    body: ReviewRequest,
    db: AsyncSession = Depends(get_db),
    doctor_id: str | None = Depends(_doctor_id),
    state_version: int | None = Depends(_state_version),
) -> JSONResponse:
    """医师确认/修改/否决处方。

    支持三种 action：
    - confirm：确认安全审核通过的处方，推进到 record 阶段
    - modify：修改处方，系统执行二次安全审核后推进到 record
    - reject：否决处方，回退到 prescription 阶段

    每次确认均写入 doctor_reviews 和 audit_events(doctor.reviewed)。
    """
    trace_id = _get_trace_id(request)
    service = ReviewService(db)
    data = await service.review(
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
# 异常处理器
# ---------------------------------------------------------------------------


async def review_invalid_action_handler(
    request: Request, exc: InvalidReviewActionError
) -> JSONResponse:
    """INVALID_REVIEW_ACTION 异常处理。"""
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


async def review_formula_override_required_handler(
    request: Request, exc: FormulaOverrideRequiredError
) -> JSONResponse:
    """FORMULA_OVERRIDE_REQUIRED 异常处理。"""
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


async def review_safety_blocked_handler(
    request: Request, exc: SafetyReviewBlockedError
) -> JSONResponse:
    """SAFETY_REVIEW_BLOCKED 异常处理。

    附加 issues 字段，供前端展示安全审核未通过的问题列表。
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
            "issues": exc.issues,
        },
    )


async def review_safety_accept_risk_handler(
    request: Request, exc: SafetyAcceptRiskUnsupportedError
) -> JSONResponse:
    """SAFETY_ACCEPT_RISK_UNSUPPORTED 异常处理。"""
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


async def review_session_not_found_handler(
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


async def review_invalid_stage_handler(
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


async def review_invalid_state_version_handler(
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


async def review_session_terminated_handler(
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


async def review_session_busy_handler(
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


async def review_validation_error_handler(
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
review_exception_handlers: dict[Any, Any] = {
    InvalidReviewActionError: review_invalid_action_handler,
    FormulaOverrideRequiredError: review_formula_override_required_handler,
    SafetyReviewBlockedError: review_safety_blocked_handler,
    SafetyAcceptRiskUnsupportedError: review_safety_accept_risk_handler,
    SessionNotFoundError: review_session_not_found_handler,
    InvalidStageTransitionError: review_invalid_stage_handler,
    InvalidStateVersionError: review_invalid_state_version_handler,
    SessionTerminatedError: review_session_terminated_handler,
    SessionBusyError: review_session_busy_handler,
    XuanhuValidationError: review_validation_error_handler,
}
