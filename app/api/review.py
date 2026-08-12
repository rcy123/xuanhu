"""医师确认 API 路由。

实现 P7-1 接口：
- POST /api/v1/consult/sessions/{session_id}/review

支持 confirm / modify / reject / request_more_info 四条路径。
本模块不实现病历生成、病历编辑或导出。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.async_command_admission import (
    try_rollout_async_admission,
)
from app.agent_runtime.lifecycle import (
    SharedLangGraphRuntime,
    allow_request_local_runtime_fallback,
)
from app.api.request_context import (
    WriteRequestContext,
    execute_model_write,
    get_trace_id,
    write_request_context,
)
from app.core.auth import DoctorPrincipal, get_current_doctor
from app.core.exceptions import (
    FormulaOverrideRequiredError,
    IdempotencyConflictError,
    InvalidReviewActionError,
    InvalidStageTransitionError,
    InvalidStateVersionError,
    ModelGatewayUnavailableError,
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
from app.models.consult import ConsultSession
from app.schemas.common import success_response
from app.schemas.review import ReviewRequest
from app.services.http_idempotency import session_http_scope
from app.services.langgraph_review import LangGraphReviewService
from app.services.review import ReviewService

router = APIRouter(prefix="/api/v1/consult", tags=["review"])


def _get_trace_id(request: Request) -> str:
    """获取或生成 trace_id。"""
    return get_trace_id(request)


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
    doctor: DoctorPrincipal = Depends(get_current_doctor),
    state_version: int | None = Depends(_state_version),
    context: WriteRequestContext = Depends(write_request_context),
) -> JSONResponse:
    """医师确认、修改、否决处方或要求补充信息。

    支持四种 action：
    - confirm：确认安全审核通过的处方，推进到 record 阶段
    - modify：修改处方，系统执行二次安全审核后推进到 record
    - reject：否决处方，回退到 syndrome 阶段
    - request_more_info：补充辨证信息，回退到 syndrome 阶段重新辨证开方

    每次提交均写入 doctor_reviews 和 audit_events(doctor.reviewed)。
    """
    trace_id = context.trace_id
    try:
        parsed_session_id = uuid.UUID(session_id)
    except ValueError:
        raise SessionNotFoundError(detail=f"session_id={session_id} not found", retryable=False) from None
    preflight = await db.get(ConsultSession, parsed_session_id)
    if preflight is None:
        raise SessionNotFoundError(detail=f"session_id={session_id} not found", retryable=False)
    # R7 rollout: prefer the durable async 202 path when the R6 substrate is
    # enabled/ready/registered; otherwise fall through to the synchronous path
    # (exact R1-R5 semantics). Single centralized rollout decision; admission
    # does bounded session/enqueue only — no model or review execution inline.
    accepted = await try_rollout_async_admission(
        request,
        request.app.state,
        session_id=session_id,
        operation="prescription.review",
        idempotency_key=context.idempotency_key,
        request_payload={
            "body": body.model_dump(mode="json"),
            "doctor_id": doctor.doctor_id,
            "state_version": state_version,
        },
    )
    if accepted is not None:
        return accepted
    is_langgraph = preflight.agent_runtime == "langgraph"
    runtime_state = getattr(request.app.state, "langgraph_runtime_state", None)
    shared_runtime: SharedLangGraphRuntime | None = (
        runtime_state.runtime if runtime_state is not None and runtime_state.status == "ready" else None
    )
    test_runtime_fallback = allow_request_local_runtime_fallback(
        runtime_state,
        test_fallback_enabled=bool(getattr(request.app.state, "allow_request_local_langgraph_test_runtime", False)),
    )
    legacy_service = ReviewService(db)
    langgraph_service = LangGraphReviewService(db)

    async def run_review() -> Any:
        if is_langgraph:
            if shared_runtime is None and not test_runtime_fallback:
                raise ModelGatewayUnavailableError(
                    "shared LangGraph runtime is unavailable",
                    retryable=True,
                )
            return await langgraph_service.review(
                session_id,
                body,
                doctor_id=doctor.doctor_id,
                trace_id=trace_id,
                x_state_version=state_version,
                idempotency_key=context.idempotency_key,
                shared_runtime=shared_runtime,
                allow_request_local_runtime=test_runtime_fallback,
            )
        return await legacy_service.review(
            session_id,
            body,
            doctor_id=doctor.doctor_id,
            trace_id=trace_id,
            x_state_version=state_version,
        )

    async def resolve_durable_outcome() -> dict[str, Any] | None:
        if not is_langgraph:
            return None
        return await langgraph_service.resolve_durable_outcome(
            session_id,
            body,
            doctor_id=doctor.doctor_id,
            idempotency_key=context.idempotency_key,
            shared_runtime=shared_runtime,
            allow_request_local_runtime=test_runtime_fallback,
        )

    scope = session_http_scope(session_id)
    result = await execute_model_write(
        db,
        context,
        operation="session.review.v1",
        scope_key=scope,
        concurrency_scope=scope,
        request_payload={
            "body": body.model_dump(mode="json"),
            "doctor_id": doctor.doctor_id,
            "state_version": state_version,
        },
        success_status=200,
        success_message="ok",
        handler=run_review,
        durable_outcome_resolver=resolve_durable_outcome,
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
# 异常处理器
# ---------------------------------------------------------------------------


async def review_invalid_action_handler(request: Request, exc: InvalidReviewActionError) -> JSONResponse:
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


async def review_formula_override_required_handler(request: Request, exc: FormulaOverrideRequiredError) -> JSONResponse:
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


async def review_safety_blocked_handler(request: Request, exc: SafetyReviewBlockedError) -> JSONResponse:
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


async def review_safety_accept_risk_handler(request: Request, exc: SafetyAcceptRiskUnsupportedError) -> JSONResponse:
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


async def review_session_not_found_handler(request: Request, exc: SessionNotFoundError) -> JSONResponse:
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


async def review_idempotency_conflict_handler(request: Request, exc: IdempotencyConflictError) -> JSONResponse:
    """Return a stable conflict when one public key is reused for another payload."""
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


async def review_invalid_stage_handler(request: Request, exc: InvalidStageTransitionError) -> JSONResponse:
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


async def review_invalid_state_version_handler(request: Request, exc: InvalidStateVersionError) -> JSONResponse:
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


async def review_session_terminated_handler(request: Request, exc: SessionTerminatedError) -> JSONResponse:
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


async def review_session_busy_handler(request: Request, exc: SessionBusyError) -> JSONResponse:
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


async def review_validation_error_handler(request: Request, exc: XuanhuValidationError) -> JSONResponse:
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
    IdempotencyConflictError: review_idempotency_conflict_handler,
    InvalidStageTransitionError: review_invalid_stage_handler,
    InvalidStateVersionError: review_invalid_state_version_handler,
    SessionTerminatedError: review_session_terminated_handler,
    SessionBusyError: review_session_busy_handler,
    XuanhuValidationError: review_validation_error_handler,
}
