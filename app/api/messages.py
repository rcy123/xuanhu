"""消息 API 路由。

实现 P3-2 两个接口：
- POST /api/v1/consult/sessions/{session_id}/messages
- GET  /api/v1/consult/sessions/{session_id}/messages

P8-6: POST /messages 在 inquiry 阶段保存医生消息后触发 InquiryAgent
+ SufficiencyAgent，返回 Agent 回复与完备性报告。

本模块不实现 Agent 调度、SSE、RAG 调用或病历生成。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.async_command_admission import (
    try_rollout_async_admission,
)
from app.agent_runtime.lifecycle import allow_request_local_runtime_fallback
from app.api.request_context import WriteRequestContext, get_trace_id, write_request_context
from app.core.exceptions import (
    AgentTriggerFailedError,
    IdempotencyConflictError,
    InvalidStageTransitionError,
    InvalidStateVersionError,
    ModelGatewayUnavailableError,
    SessionBusyError,
    SessionNotFoundError,
    SessionTerminatedError,
)
from app.core.exceptions import (
    ValidationError as XuanhuValidationError,
)
from app.db.session import get_db
from app.schemas.common import success_response
from app.schemas.message import MessageCreateRequest
from app.services.http_idempotency import HttpCommandExecutor, session_http_scope
from app.services.langgraph_intake import resolve_durable_intake_message_response
from app.services.message import MessageService

router = APIRouter(prefix="/api/v1/consult", tags=["messages"])


def _get_trace_id(request: Request) -> str:
    """获取或生成 trace_id。"""
    return get_trace_id(request)


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


@router.post("/sessions/{session_id}/messages")
async def create_message(
    request: Request,
    session_id: str,
    body: MessageCreateRequest,
    db: AsyncSession = Depends(get_db),
    doctor_id: str | None = Depends(_doctor_id),
    state_version: int | None = Depends(_state_version),
    context: WriteRequestContext = Depends(write_request_context),
) -> JSONResponse:
    """提交问诊消息（P8-6: 触发 Agent 回复）。"""
    runtime_state = getattr(request.app.state, "langgraph_runtime_state", None)
    test_runtime_fallback = allow_request_local_runtime_fallback(
        runtime_state,
        test_fallback_enabled=bool(
            getattr(
                request.app.state,
                "allow_request_local_langgraph_test_runtime",
                False,
            )
        ),
    )
    trace_id = context.trace_id
    # R7 rollout: prefer the durable async 202 path when the R6 substrate is
    # enabled/ready/registered; otherwise fall through to the synchronous path
    # (exact R1-R5 semantics). This is the single centralized rollout decision;
    # admission does bounded validation/session/enqueue only — no model or graph
    # execution in the request task.
    accepted = await try_rollout_async_admission(
        request,
        request.app.state,
        session_id=session_id,
        operation="intake.message",
        idempotency_key=context.idempotency_key,
        request_payload={
            "body": body.model_dump(mode="json"),
            "doctor_id": doctor_id,
            "state_version": state_version,
        },
    )
    if accepted is not None:
        return accepted
    service = MessageService(
        db,
        shared_langgraph_runtime=(runtime_state.runtime if runtime_state is not None else None),
        # ASGITransport does not run lifespan automatically.  The fallback is
        # restricted to explicit test configuration; every non-test process
        # fails closed when startup state is absent or degraded.
        allow_request_local_langgraph_runtime=test_runtime_fallback,
    )
    # Keep retryable infrastructure failures outside HttpCommandExecutor so
    # the idempotency key can execute after the process runtime recovers.
    is_langgraph = await service.ensure_submission_runtime_available(session_id)

    async def submit() -> dict[str, Any]:
        data = await service.submit_message(
            session_id,
            body,
            doctor_id=doctor_id,
            trace_id=trace_id,
            x_state_version=state_version,
            idempotency_key=context.idempotency_key,
        )
        return data.model_dump(mode="json", exclude_none=True)

    scope = session_http_scope(session_id)

    async def resolve_durable_outcome() -> dict[str, Any] | None:
        if not is_langgraph:
            return None
        return await resolve_durable_intake_message_response(
            session_id,
            body,
            idempotency_key=context.idempotency_key,
            retry_failed_command=submit,
        )

    result = await HttpCommandExecutor(db).execute(
        operation="session.message.create.v1",
        scope_key=scope,
        concurrency_scope=scope,
        idempotency_key=context.idempotency_key,
        is_idempotent=context.is_idempotent,
        request_payload={
            "body": body.model_dump(mode="json"),
            "doctor_id": doctor_id,
            "state_version": state_version,
        },
        success_status=200,
        success_message="ok",
        handler=submit,
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


@router.get("/sessions/{session_id}/messages")
async def get_messages(
    request: Request,
    session_id: str,
    before: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    stage: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """获取消息历史（游标分页）。"""
    trace_id = _get_trace_id(request)
    service = MessageService(db)
    data = await service.get_messages(
        session_id,
        before=before,
        limit=limit,
        stage=stage,
        trace_id=trace_id,
    )
    return JSONResponse(
        status_code=200,
        content=success_response(data=data.model_dump(mode="json"), trace_id=trace_id),
    )


# ---------------------------------------------------------------------------
# 异常处理器
# ---------------------------------------------------------------------------


async def session_busy_handler(request: Request, exc: SessionBusyError) -> JSONResponse:
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


async def invalid_state_version_handler(request: Request, exc: InvalidStateVersionError) -> JSONResponse:
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


async def session_terminated_handler(request: Request, exc: SessionTerminatedError) -> JSONResponse:
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


async def message_not_found_handler(request: Request, exc: SessionNotFoundError) -> JSONResponse:
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


async def message_invalid_stage_handler(request: Request, exc: InvalidStageTransitionError) -> JSONResponse:
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


async def agent_trigger_failed_handler(request: Request, exc: AgentTriggerFailedError) -> JSONResponse:
    """AGENT_TRIGGER_FAILED 异常处理（P8-6）。

    医生消息已落库，Agent 回复失败。返回 503，携带 agent_error_code。
    """
    trace_id = _get_trace_id(request)
    payload: dict[str, Any] = {
        "code": exc.code,
        "message": exc.message,
        "detail": exc.detail,
        "retryable": exc.retryable,
        "stage": None,
        "trace_id": trace_id,
    }
    if exc.agent_error_code:
        payload["agent_error_code"] = exc.agent_error_code
    return JSONResponse(
        status_code=exc.status_code,
        content=payload,
    )


async def model_gateway_unavailable_handler(request: Request, exc: ModelGatewayUnavailableError) -> JSONResponse:
    """MODEL_GATEWAY_UNAVAILABLE 异常处理（P8-6）。"""
    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=503,
        content={
            "code": "MODEL_GATEWAY_UNAVAILABLE",
            "message": "模型网关不可用，Agent 回复暂不可用",
            "detail": str(exc),
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


async def idempotency_conflict_handler(request: Request, exc: IdempotencyConflictError) -> JSONResponse:
    """Return a stable conflict when one public key is reused for another payload."""

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": exc.detail,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": _get_trace_id(request),
        },
    )


# 导出路由级异常处理映射
message_exception_handlers: dict[Any, Any] = {
    SessionBusyError: session_busy_handler,
    InvalidStateVersionError: invalid_state_version_handler,
    SessionTerminatedError: session_terminated_handler,
    SessionNotFoundError: message_not_found_handler,
    InvalidStageTransitionError: message_invalid_stage_handler,
    AgentTriggerFailedError: agent_trigger_failed_handler,
    ModelGatewayUnavailableError: model_gateway_unavailable_handler,
    IdempotencyConflictError: idempotency_conflict_handler,
}
