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
from app.core.exceptions import (
    LangGraphRecoveryNotImplementedError,
    ModelGatewayUnavailableError,
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
    """使用持久化控制引用恢复中断会话。

    PostgreSQL Domain State 与 artifact revision 始终是唯一临床权威。
    ``resume_from_pg_snapshot`` 仅保留为兼容的公开 action 名称，并不会从
    ``ConsultSession.state_snapshot`` 恢复临床数据。系统会在任何 command
    claim 或 Domain 写入前，将 LangGraph checkpoint 按严格的纯引用控制
    schema 完整校验；成功恢复或终止后写入审计。
    """
    trace_id = context.trace_id
    service = RecoveryDispatcher(db)
    runtime = await service.get_runtime(session_id)
    # The runtime probe opens a read transaction.  A request-local
    # AsyncPostgresSaver may need CREATE INDEX CONCURRENTLY during setup,
    # which must not wait on this request's own virtual transaction ID.
    if db.in_transaction():
        await db.rollback()
    runtime_state = getattr(request.app.state, "langgraph_runtime_state", None)
    shared_runtime: SharedLangGraphRuntime | None = (
        runtime_state.runtime if runtime_state is not None and runtime_state.status == "ready" else None
    )
    test_runtime_fallback = allow_request_local_runtime_fallback(
        runtime_state,
        test_fallback_enabled=bool(getattr(request.app.state, "allow_request_local_langgraph_test_runtime", False)),
    )
    if runtime == "langgraph" and shared_runtime is None and not test_runtime_fallback:
        raise ModelGatewayUnavailableError(
            "shared LangGraph runtime is unavailable",
            retryable=True,
        )
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
            idempotency_key=context.idempotency_key,
            shared_runtime=shared_runtime,
            allow_request_local_runtime=test_runtime_fallback,
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


async def recovery_session_not_found_handler(request: Request, exc: SessionNotFoundError) -> JSONResponse:
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


async def recovery_not_needed_handler(request: Request, exc: RecoveryNotNeededError) -> JSONResponse:
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


async def state_recovery_required_handler(request: Request, exc: StateRecoveryRequiredError) -> JSONResponse:
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


async def recovery_validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
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


async def recovery_session_busy_handler(request: Request, exc: SessionBusyError) -> JSONResponse:
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


async def recovery_model_gateway_handler(request: Request, exc: ModelGatewayUnavailableError) -> JSONResponse:
    """Return the stable unavailable envelope before recovery mutation."""

    return JSONResponse(
        status_code=503,
        content={
            "code": "MODEL_GATEWAY_UNAVAILABLE",
            "message": "LangGraph 运行时不可用，暂不能恢复会话",
            "detail": str(exc),
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": _get_trace_id(request),
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
    ModelGatewayUnavailableError: recovery_model_gateway_handler,
}
