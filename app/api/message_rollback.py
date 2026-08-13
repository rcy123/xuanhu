"""问诊消息回退 API。

- POST /api/v1/consult/sessions/{session_id}/messages/{message_id}/rollback

同步执行（不触发 LangGraph / 模型调用）：删除目标消息及其之后的所有消息，
重建 observations / safety assertions / safety profile，推进 state_version，
插入回退提示消息并写审计。前端调用成功后重新拉取消息与读模型即可。

异常处理复用 messages 路由级 handler（SessionBusy / InvalidStateVersion /
SessionNotFound / InvalidStageTransition），另注册 ValidationError 本地 handler。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.request_context import get_trace_id, validate_session_id
from app.core.access import require_session_owner
from app.core.auth import DoctorPrincipal, get_current_doctor
from app.core.exceptions import (
    IdempotencyConflictError,
    SessionBusyError,
    SessionNotFoundError,
)
from app.core.exceptions import (
    ValidationError as XuanhuValidationError,
)
from app.core.ratelimit import require_write_rate_limit
from app.db.session import get_db
from app.models.consult import ConsultSession
from app.schemas.common import success_response
from app.schemas.message import MessageRollbackRequest
from app.services.message_rollback import rollback_messages_to
from app.services.session_lock import SessionLock

router = APIRouter(prefix="/api/v1/consult", tags=["messages"])


def _get_trace_id(request: Request) -> str:
    """获取或生成 trace_id。"""
    return get_trace_id(request)


def _state_version(
    x_state_version: str | None = Header(default=None, alias="X-State-Version"),
) -> int | None:
    """读取客户端 state_version（非整数直接拒绝）。"""
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


@router.post("/sessions/{session_id}/messages/{message_id}/rollback")
async def rollback_messages(
    request: Request,
    message_id: str,
    body: MessageRollbackRequest,
    session_id: str = Depends(validate_session_id),
    _rl: None = Depends(require_write_rate_limit),
    _: None = Depends(require_session_owner),
    db: AsyncSession = Depends(get_db),
    doctor: DoctorPrincipal = Depends(get_current_doctor),
    state_version: int | None = Depends(_state_version),
) -> JSONResponse:
    """回退到指定消息：删除该消息及其之后的所有问诊记录并重建事实状态。"""
    trace_id = _get_trace_id(request)
    sid = uuid.UUID(session_id)
    target_message_id = uuid.UUID(message_id)
    lock = SessionLock(db, session_id, trace_id)
    try:
        await lock.acquire()
    except SessionBusyError:
        raise SessionBusyError(
            detail=f"session_id={session_id} session is busy with another write",
            retryable=True,
        ) from None
    try:
        if db.in_transaction():
            await db.rollback()
        async with db.begin():
            session = await db.get(ConsultSession, sid, with_for_update=True)
            if session is None:
                raise SessionNotFoundError(detail=f"session_id={session_id} not found", retryable=False)
            if state_version is not None and state_version != session.state_version:
                from app.core.exceptions import InvalidStateVersionError

                raise InvalidStateVersionError(
                    detail=(
                        f"session_id={session_id} client version {state_version} "
                        f"!= server version {session.state_version}"
                    ),
                    retryable=True,
                )
            data = await rollback_messages_to(
                db,
                session=session,
                target_message_id=target_message_id,
                trace_id=trace_id,
                reason=body.reason,
            )
        return JSONResponse(
            status_code=200,
            content=success_response(
                data=data.model_dump(mode="json"),
                trace_id=trace_id,
                message="ok",
            ),
        )
    finally:
        await lock.release()


async def rollback_validation_handler(request: Request, exc: XuanhuValidationError) -> JSONResponse:
    """ValidationError 本地 handler（复用 common 响应结构）。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": None,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": _get_trace_id(request),
        },
    )


rollback_exception_handlers: dict[Any, Any] = {
    XuanhuValidationError: rollback_validation_handler,
    SessionNotFoundError: rollback_validation_handler,
    SessionBusyError: rollback_validation_handler,
    IdempotencyConflictError: rollback_validation_handler,
}
