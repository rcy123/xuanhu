"""Human confirmation boundary for high-risk safety-fact candidates."""

from __future__ import annotations

import uuid
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
from app.core.exceptions import SessionNotFoundError, ValidationError, XuanhuError
from app.core.ratelimit import require_write_rate_limit
from app.db.session import get_db
from app.schemas.common import success_response
from app.schemas.safety_confirmation import SafetyAssertionDecisionRequest, SafetyAssertionStatus
from app.services.http_idempotency import session_http_scope
from app.services.safety_confirmation import SafetyAction, SafetyConfirmationService

router = APIRouter(prefix="/api/v1/consult", tags=["safety-confirmations"])


def _uuid(value: str, *, kind: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise SessionNotFoundError(detail=f"{kind} is not a valid UUID") from exc


def _require_actor(doctor: DoctorPrincipal) -> str:
    """取医师主体 ID；off/audit 回退态下缺失时维持原「必填」语义。"""
    actor = (doctor.doctor_id or "").strip()
    if not actor or len(actor) > 128:
        raise ValidationError(detail="X-Doctor-Id is required and must be at most 128 characters")
    return actor


@router.get("/sessions/{session_id}/safety-assertions")
async def list_safety_assertions(
    request: Request,
    session_id: str = Depends(validate_session_id),
    status: SafetyAssertionStatus | None = Query(default=None),
    _: None = Depends(require_session_reader),
    db: AsyncSession = Depends(get_db),
    doctor: DoctorPrincipal = Depends(get_current_doctor),
) -> JSONResponse:
    result = await SafetyConfirmationService(db).list_assertions(
        _uuid(session_id, kind="session_id"),
        status=status,
    )
    trace_id = get_trace_id(request)
    return JSONResponse(content=success_response(data=result.model_dump(mode="json"), trace_id=trace_id))


async def _decide(
    *,
    session_id: str,
    assertion_id: str,
    action: SafetyAction,
    body: SafetyAssertionDecisionRequest,
    context: WriteRequestContext,
    doctor_id: str,
    db: AsyncSession,
) -> JSONResponse:
    scope = session_http_scope(session_id)
    result = await execute_model_write(
        db,
        context,
        operation="safety_assertion.transition.v1",
        scope_key=scope,
        concurrency_scope=scope,
        request_payload={
            "assertion_id": assertion_id,
            "action": action,
            "body": body.model_dump(mode="json"),
            "doctor_id": doctor_id,
        },
        success_status=200,
        success_message="ok",
        handler=lambda: SafetyConfirmationService(db).transition(
            session_id=_uuid(session_id, kind="session_id"),
            assertion_id=_uuid(assertion_id, kind="assertion_id"),
            action=action,
            actor_id=doctor_id,
            context=context,
            reason_code=body.reason_code,
        ),
    )
    return JSONResponse(
        status_code=result.status_code,
        content=success_response(
            data=result.data,
            trace_id=context.trace_id,
            message=result.message,
        ),
    )


@router.post("/sessions/{session_id}/safety-assertions/{assertion_id}/confirm")
async def confirm_safety_assertion(
    assertion_id: str,
    body: SafetyAssertionDecisionRequest,
    session_id: str = Depends(validate_session_id),
    _rl: None = Depends(require_write_rate_limit),
    _: None = Depends(require_session_owner),
    context: WriteRequestContext = Depends(write_request_context),
    doctor: DoctorPrincipal = Depends(get_current_doctor),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _decide(
        session_id=session_id,
        assertion_id=assertion_id,
        action="confirm",
        body=body,
        context=context,
        doctor_id=_require_actor(doctor),
        db=db,
    )


@router.post("/sessions/{session_id}/safety-assertions/{assertion_id}/reject")
async def reject_safety_assertion(
    assertion_id: str,
    body: SafetyAssertionDecisionRequest,
    session_id: str = Depends(validate_session_id),
    _rl: None = Depends(require_write_rate_limit),
    _: None = Depends(require_session_owner),
    context: WriteRequestContext = Depends(write_request_context),
    doctor: DoctorPrincipal = Depends(get_current_doctor),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _decide(
        session_id=session_id,
        assertion_id=assertion_id,
        action="reject",
        body=body,
        context=context,
        doctor_id=_require_actor(doctor),
        db=db,
    )


@router.post("/sessions/{session_id}/safety-assertions/{assertion_id}/retract")
async def retract_safety_assertion(
    assertion_id: str,
    body: SafetyAssertionDecisionRequest,
    session_id: str = Depends(validate_session_id),
    _rl: None = Depends(require_write_rate_limit),
    _: None = Depends(require_session_owner),
    context: WriteRequestContext = Depends(write_request_context),
    doctor: DoctorPrincipal = Depends(get_current_doctor),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _decide(
        session_id=session_id,
        assertion_id=assertion_id,
        action="retract",
        body=body,
        context=context,
        doctor_id=_require_actor(doctor),
        db=db,
    )


async def safety_confirmation_error_handler(request: Request, exc: XuanhuError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": None,
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": get_trace_id(request),
        },
    )


safety_confirmation_exception_handlers: dict[Any, Any] = {
    XuanhuError: safety_confirmation_error_handler,
}


__all__ = ["router", "safety_confirmation_exception_handlers"]
