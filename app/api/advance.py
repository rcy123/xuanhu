"""阶段推进 API 路由。

实现接口设计文档 §4.3.1：
- POST /api/v1/consult/sessions/{session_id}/advance

薄包装 Supervisor.advance()，并在调用前预校验：
- current_stage=inquiry 且 sufficiency_report.sufficient=false → INSUFFICIENT_INQUIRY
- current_stage=review → PENDING_DOCTOR_REVIEW
- current_stage=done/blocked → INVALID_STAGE_TRANSITION

review 阶段必须挂起等待医师确认，不得自动推进。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.supervisor import Supervisor, SupervisorResult
from app.core.exceptions import (
    InsufficientInquiryError,
    InvalidStageTransitionError,
    InvalidStateVersionError,
    ModelGatewayUnavailableError,
    PendingDoctorReviewError,
    SessionBusyError,
    SessionNotFoundError,
    ValidationError,
)
from app.db.session import get_db
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.models.domain import GateResult, GraphRun, IntakeCommandClaim, OutboxEvent
from app.schemas.advance import AdvanceRequest
from app.schemas.common import success_response
from app.schemas.completeness import COMPLETENESS_GATE_NAME, COMPLETENESS_POLICY_VERSION
from app.services.session_lock import session_lock

router = APIRouter(prefix="/api/v1/consult", tags=["advance"])


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
        from app.core.exceptions import ValidationError

        raise ValidationError(
            message=f"X-State-Version 必须为整数，收到: {x_state_version}",
            detail=f"X-State-Version header 值 '{x_state_version}' 无法解析为整数",
            retryable=False,
        ) from err


async def _load_session_for_advance(
    db: AsyncSession,
    session_id: str,
) -> ConsultSession:
    """加载会话用于 advance 预校验。"""
    try:
        sid = uuid.UUID(session_id)
    except ValueError as exc:
        raise SessionNotFoundError(
            detail=f"session_id={session_id} 格式非法",
            retryable=False,
        ) from exc
    result = await db.execute(select(ConsultSession).where(ConsultSession.id == sid))
    session = result.scalar_one_or_none()
    if session is None:
        raise SessionNotFoundError(
            detail=f"session_id={session_id} 在数据库中未找到",
            retryable=False,
        )
    return session


def _precheck_stage(session: ConsultSession, force: bool) -> None:
    """调用 Supervisor 前的阶段预校验。

    - review 阶段：必须挂起等待医师确认，不得 advance → PENDING_DOCTOR_REVIEW
    - done/blocked：终态不可推进 → INVALID_STAGE_TRANSITION
    - inquiry 阶段且未 force：要求 sufficiency_report.sufficient=true，
      否则 INSUFFICIENT_INQUIRY
    """
    stage = session.current_stage
    if stage == "review":
        raise PendingDoctorReviewError(
            detail=(
                f"session_id={session.id} current_stage=review "
                f"pending_review={session.pending_review}，需先提交医师确认"
            ),
        )
    if stage in ("done", "blocked"):
        raise InvalidStageTransitionError(
            message=f"当前阶段 {stage} 不可推进",
            detail=f"session_id={session.id} current_stage={stage}",
            retryable=False,
        )
    if stage == "inquiry" and not force:
        snapshot = session.state_snapshot or {}
        suff = snapshot.get("sufficiency_report")
        sufficient = bool(suff.get("sufficient")) if isinstance(suff, dict) else False
        if not sufficient:
            raise InsufficientInquiryError(
                detail=(
                    f"session_id={session.id} current_stage=inquiry "
                    f"sufficient={sufficient}，问诊信息不充分，不能推进"
                ),
            )


def _advance_command_key(trace_id: str) -> str:
    return _safe_ref("advance", trace_id)[:128]


def _advance_payload_digest(force: bool) -> str:
    payload = {"command": "advance", "force": force}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _advance_claim_is_stale(claim: IntakeCommandClaim) -> bool:
    updated_at = claim.updated_at
    if updated_at.tzinfo is None:
        from datetime import UTC

        updated_at = updated_at.replace(tzinfo=UTC)
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - updated_at > timedelta(seconds=60)


def _safe_ref(prefix: str, value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._:-]+", "_", value).strip("._:-")
    if safe and len(safe) <= 96:
        return f"{prefix}:{safe}"
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


async def _run_langgraph_advance(
    db: AsyncSession,
    session: ConsultSession,
    *,
    session_id: str,
    state_version: int | None,
    trace_id: str,
    force: bool = False,
) -> dict[str, Any]:
    del session
    sid = uuid.UUID(session_id)
    command_key = _advance_command_key(trace_id)
    payload_digest = _advance_payload_digest(force)
    async with session_lock(db, session_id, trace_id):
        if db.in_transaction():
            await db.rollback()
        async with db.begin():
            existing = await db.scalar(
                select(IntakeCommandClaim)
                .where(
                    IntakeCommandClaim.session_id == sid,
                    IntakeCommandClaim.idempotency_key == command_key,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.payload_digest != payload_digest:
                    raise ValidationError(
                        message="相同幂等键不能复用不同 advance 命令",
                        detail=f"session_id={session_id} command_id={command_key} payload_digest_mismatch",
                        retryable=False,
                    )
                if existing.status == "completed" and existing.response_payload is not None:
                    return dict(existing.response_payload)
                if existing.status == "running" and not _advance_claim_is_stale(existing):
                    raise SessionBusyError(detail=f"session_id={session_id} advance command is still running")

            run_id = (
                existing.run_id
                if existing is not None
                else uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:advance:{session_id}:{command_key}")
            )
            locked = await db.get(ConsultSession, sid, with_for_update=True)
            if locked is None:
                raise SessionNotFoundError(detail=f"session_id={session_id} not found", retryable=False)
            if state_version is not None and state_version != locked.state_version:
                raise InvalidStateVersionError(
                    detail=(
                        f"session_id={session_id} client version {state_version} "
                        f"!= server version {locked.state_version}"
                    ),
                    retryable=True,
                )
            if locked.current_stage == "review":
                raise PendingDoctorReviewError(
                    detail=f"session_id={locked.id} current_stage=review requires doctor confirmation",
                )
            if locked.current_stage in ("done", "blocked") or locked.status in ("done", "blocked", "terminated"):
                raise InvalidStageTransitionError(
                    message=f"当前阶段 {locked.current_stage} 不可推进",
                    detail=f"session_id={locked.id} current_stage={locked.current_stage} status={locked.status}",
                    retryable=False,
                )

            from_stage = locked.current_stage
            gate_id: uuid.UUID | None = None
            gate_state_version: int | None = None
            if locked.current_stage == "inquiry":
                result = await db.execute(
                    select(GateResult)
                    .where(
                        GateResult.session_id == locked.id,
                        GateResult.gate_name == COMPLETENESS_GATE_NAME,
                        GateResult.policy_version == COMPLETENESS_POLICY_VERSION,
                        GateResult.input_state_version == locked.state_version,
                    )
                    .order_by(GateResult.created_at.desc(), GateResult.id.desc())
                    .limit(1)
                )
                gate = result.scalar_one_or_none()
                gate_details = gate.details if gate is not None and isinstance(gate.details, dict) else {}
                if gate is None or gate.decision != "passed" or gate_details.get("disposition") != "ready":
                    raise InsufficientInquiryError(
                        detail=(
                            f"session_id={locked.id} current_stage={locked.current_stage} "
                            "LangGraph advance requires persisted completeness disposition=ready for current state_version"
                        ),
                    )
                gate_id = gate.id
                gate_state_version = gate.input_state_version
                locked.current_stage = "syndrome"
                locked.state_version += 1
                snapshot = dict(locked.state_snapshot or {})
                snapshot["agent_runtime"] = "langgraph"
                snapshot["current_stage"] = "syndrome"
                snapshot["state_version"] = locked.state_version
                snapshot["advance"] = {
                    "source_gate_id": str(gate.id),
                    "source_gate_state_version": gate.input_state_version,
                    "trace_id": trace_id,
                }
                locked.state_snapshot = snapshot
            elif locked.current_stage != "syndrome":
                raise InvalidStageTransitionError(
                    message=f"当前阶段 {locked.current_stage} 不可推进",
                    detail=f"session_id={locked.id} current_stage={locked.current_stage} status={locked.status}",
                    retryable=False,
                )

            input_version = max(gate_state_version or state_version or locked.state_version, 1)
            graph_run = await db.get(GraphRun, run_id)
            if graph_run is None:
                db.add(
                    GraphRun(
                        id=run_id,
                        session_id=sid,
                        graph_version="main-graph.v1",
                        command_id=command_key,
                        input_state_version=input_version,
                        status="completed",
                        completed_at=func.now(),
                    )
                )
            else:
                graph_run.input_state_version = input_version
                graph_run.status = "completed"
                graph_run.completed_at = func.now()

            response_payload = {
                "session_id": session_id,
                "current_stage": locked.current_stage,
                "from_stage": from_stage,
                "state_version": locked.state_version,
                "blocked_reason": locked.blocked_reason,
                "agent_name": None,
                "trace_id": trace_id,
            }
            db.add(
                OutboxEvent(
                    id=uuid.uuid4(),
                    event_type="advance.command_completed.v1",
                    session_id=sid,
                    graph_run_id=run_id,
                    state_version=locked.state_version,
                    trace_id=_safe_ref("trace", trace_id),
                    payload={
                        "session_id": session_id,
                        "command_id": command_key,
                        "from_stage": from_stage,
                        "to_stage": locked.current_stage,
                        "source_gate_id": str(gate_id) if gate_id else None,
                        "source_gate_state_version": gate_state_version,
                    },
                )
            )
            db.add(
                AuditEvent(
                    session_id=sid,
                    event_type="advance.completed",
                    actor_type="system",
                    actor_id=None,
                    payload={
                        "command_id": command_key,
                        "from_stage": from_stage,
                        "to_stage": locked.current_stage,
                        "state_version": locked.state_version,
                    },
                    trace_id=trace_id,
                )
            )
            if existing is None:
                db.add(
                    IntakeCommandClaim(
                        id=uuid.uuid4(),
                        session_id=sid,
                        idempotency_key=command_key,
                        payload_digest=payload_digest,
                        input_state_version=input_version,
                        status="completed",
                        run_id=run_id,
                        output_state_version=locked.state_version,
                        response_payload=response_payload,
                    )
                )
            else:
                existing.status = "completed"
                existing.output_state_version = locked.state_version
                existing.response_payload = response_payload
                existing.updated_at = func.now()
            return response_payload


@router.post("/sessions/{session_id}/advance")
async def advance_session(
    request: Request,
    session_id: str,
    body: AdvanceRequest,
    db: AsyncSession = Depends(get_db),
    doctor_id: str | None = Depends(_doctor_id),
    state_version: int | None = Depends(_state_version),
) -> JSONResponse:
    """阶段推进（§4.3.1）。

    问诊完备性充分后，调用此接口依次执行辨证→开方→加减→安全审核。
    安全审核通过后挂起等待医师确认（不进病历生成）。
    """
    trace_id = _get_trace_id(request)
    del doctor_id  # MVP 审计由 Supervisor 内部完成

    # 预校验阶段
    session = await _load_session_for_advance(db, session_id)
    if getattr(session, "agent_runtime", "legacy") == "langgraph":
        data = await _run_langgraph_advance(
            db,
            session,
            session_id=session_id,
            state_version=state_version,
            trace_id=trace_id,
            force=body.force,
        )
        return JSONResponse(
            status_code=200,
            content=success_response(data=data, trace_id=trace_id),
        )

    _precheck_stage(session, body.force)

    supervisor = Supervisor(db)
    result: SupervisorResult = await supervisor.advance(
        session_id,
        trace_id,
        expected_state_version=state_version,
        force=body.force,
    )

    data = {
        "session_id": session_id,
        "current_stage": (
            result.to_stage.value if hasattr(result.to_stage, "value") else str(result.to_stage)
        ),
        "from_stage": (
            result.from_stage.value if hasattr(result.from_stage, "value") else str(result.from_stage)
        ),
        "state_version": result.state.state_version,
        "blocked_reason": result.blocked_reason,
        "agent_name": result.agent_name,
        "trace_id": trace_id,
    }
    return JSONResponse(
        status_code=200,
        content=success_response(data=data, trace_id=trace_id),
    )


# ---------------------------------------------------------------------------
# 异常处理器
# ---------------------------------------------------------------------------


async def advance_session_not_found_handler(
    request: Request, exc: SessionNotFoundError
) -> JSONResponse:
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


async def advance_busy_handler(
    request: Request, exc: SessionBusyError
) -> JSONResponse:
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


async def advance_invalid_state_version_handler(
    request: Request, exc: InvalidStateVersionError
) -> JSONResponse:
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


async def advance_invalid_stage_handler(
    request: Request, exc: InvalidStageTransitionError
) -> JSONResponse:
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


async def insufficient_inquiry_handler(
    request: Request, exc: InsufficientInquiryError
) -> JSONResponse:
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


async def pending_doctor_review_handler(
    request: Request, exc: PendingDoctorReviewError
) -> JSONResponse:
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


async def advance_model_gateway_handler(
    request: Request, exc: ModelGatewayUnavailableError
) -> JSONResponse:
    trace_id = _get_trace_id(request)
    return JSONResponse(
        status_code=503,
        content={
            "code": "MODEL_GATEWAY_UNAVAILABLE",
            "message": "模型网关不可用，阶段推进暂不可用",
            "detail": str(exc),
            "retryable": exc.retryable,
            "stage": None,
            "trace_id": trace_id,
        },
    )


advance_exception_handlers: dict[Any, Any] = {
    SessionNotFoundError: advance_session_not_found_handler,
    SessionBusyError: advance_busy_handler,
    InvalidStateVersionError: advance_invalid_state_version_handler,
    InvalidStageTransitionError: advance_invalid_stage_handler,
    InsufficientInquiryError: insufficient_inquiry_handler,
    PendingDoctorReviewError: pending_doctor_review_handler,
    ModelGatewayUnavailableError: advance_model_gateway_handler,
}
