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

import asyncio
import hashlib
import json
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.checkpoint import postgres_checkpointer
from app.agent_runtime.commands import XuanhuCommand
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.state import default_state
from app.agents.supervisor import Supervisor, SupervisorResult
from app.api.request_context import WriteRequestContext, get_trace_id, write_request_context
from app.core.config import get_settings
from app.core.exceptions import (
    IdempotencyConflictError,
    InsufficientInquiryError,
    InvalidStageTransitionError,
    InvalidStateVersionError,
    ModelGatewayUnavailableError,
    PendingDoctorReviewError,
    SessionBusyError,
    SessionNotFoundError,
)
from app.db.session import get_db
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.models.domain import GateResult, GraphRun, IntakeCommandClaim, OutboxEvent
from app.schemas.advance import AdvanceRequest
from app.schemas.common import success_response
from app.schemas.completeness import COMPLETENESS_GATE_NAME, COMPLETENESS_POLICY_VERSION
from app.services.http_idempotency import HttpCommandExecutor, session_http_scope
from app.services.session_lock import SessionLock

router = APIRouter(prefix="/api/v1/consult", tags=["advance"])


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


def _advance_command_key(idempotency_key: str | None) -> str:
    """Derive a durable command key independently from the attempt trace."""

    logical_key = idempotency_key or uuid.uuid4().hex
    digest = hashlib.sha256(f"advance\0{logical_key}".encode()).hexdigest()
    return f"advance:{digest}"


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


async def _invoke_reasoning_graph(*, session_id: str, command_key: str, run_id: uuid.UUID) -> None:
    graph_state = default_state(
        session_id=session_id,
        command=XuanhuCommand.ADVANCE.value,
        command_id=command_key,
        graph_version=DEFAULT_GRAPH_VERSION,
        run_id=str(run_id),
    )
    config = make_run_config(session_id, graph_version=DEFAULT_GRAPH_VERSION)
    async with postgres_checkpointer(get_settings().database_url) as saver:
        graph = build_main_graph(checkpointer=saver)
        runner = GraphRunner(graph, timeout_seconds=120)
        await runner.ainvoke(dict(graph_state), config=config)


async def _completed_advance_response(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    command_key: str,
    payload_digest: str,
) -> dict[str, Any]:
    if db.in_transaction():
        await db.rollback()
    claim = await db.scalar(
        select(IntakeCommandClaim)
        .where(
            IntakeCommandClaim.session_id == session_id,
            IntakeCommandClaim.idempotency_key == command_key,
        )
        .execution_options(populate_existing=True)
    )
    if claim is None:
        raise SessionBusyError(detail=f"session_id={session_id} advance command did not create a claim")
    if claim.payload_digest != payload_digest:
        raise IdempotencyConflictError(
            message="相同幂等键不能复用不同 advance 命令",
            detail=f"session_id={session_id} command_id={command_key} payload_digest_mismatch",
            retryable=False,
        )
    if claim.status == "completed" and claim.response_payload is not None:
        return dict(claim.response_payload)
    if claim.status == "failed":
        raise ModelGatewayUnavailableError(
            f"session_id={session_id} advance reasoning graph failed: {claim.error_code or 'UNKNOWN'}",
            retryable=True,
        )
    raise SessionBusyError(detail=f"session_id={session_id} advance command is still running")


async def _wait_for_completed_advance_response(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    command_key: str,
    payload_digest: str,
) -> dict[str, Any]:
    """Wait for the owner of an in-flight idempotent command to finish."""

    for _ in range(480):
        await asyncio.sleep(0.25)
        if db.in_transaction():
            await db.rollback()
        claim = await db.scalar(
            select(IntakeCommandClaim)
            .where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == command_key,
            )
            .execution_options(populate_existing=True)
        )
        if claim is None:
            break
        if claim.payload_digest != payload_digest:
            raise IdempotencyConflictError(
                message="相同幂等键不能复用不同 advance 命令",
                detail=f"session_id={session_id} command_id={command_key} payload_digest_mismatch",
                retryable=False,
            )
        if claim.status == "completed" and claim.response_payload is not None:
            return dict(claim.response_payload)
        if claim.status == "failed":
            raise ModelGatewayUnavailableError(
                f"session_id={session_id} advance reasoning graph failed: {claim.error_code or 'UNKNOWN'}",
                retryable=True,
            )
    raise SessionBusyError(detail=f"session_id={session_id} advance command is still running")


async def _replay_advance_after_lock_conflict(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    command_key: str,
    payload_digest: str,
) -> dict[str, Any] | None:
    """Recognise a concurrently-created claim after losing the session lock."""

    for _ in range(20):
        await asyncio.sleep(0.05)
        if db.in_transaction():
            await db.rollback()
        claim = await db.scalar(
            select(IntakeCommandClaim).where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == command_key,
            )
        )
        if claim is None:
            continue
        if claim.payload_digest != payload_digest:
            raise IdempotencyConflictError(
                message="相同幂等键不能复用不同 advance 命令",
                detail=f"session_id={session_id} command_id={command_key} payload_digest_mismatch",
                retryable=False,
            )
        if claim.status == "completed" and claim.response_payload is not None:
            return dict(claim.response_payload)
        return await _wait_for_completed_advance_response(
            db,
            session_id=session_id,
            command_key=command_key,
            payload_digest=payload_digest,
        )
    return None


class _WaitForAdvanceReplay(Exception):
    """Internal control flow used to leave the claim transaction before polling."""


async def _mark_advance_failed(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    session_id: uuid.UUID,
    command_key: str,
    error_code: str,
) -> None:
    if db.in_transaction():
        await db.rollback()
    async with db.begin():
        claim = await db.scalar(
            select(IntakeCommandClaim)
            .where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == command_key,
            )
            .with_for_update()
        )
        if claim is not None and claim.status != "completed":
            claim.status = "failed"
            claim.error_code = error_code[:64]
            claim.updated_at = func.now()
        graph_run = await db.get(GraphRun, run_id, with_for_update=True)
        if graph_run is not None and graph_run.status != "completed":
            graph_run.status = "failed"
            graph_run.completed_at = func.now()


async def _run_langgraph_advance(
    db: AsyncSession,
    session: ConsultSession,
    *,
    session_id: str,
    state_version: int | None,
    trace_id: str,
    force: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    del session
    sid = uuid.UUID(session_id)
    command_key = _advance_command_key(idempotency_key)
    payload_digest = _advance_payload_digest(force)
    run_id: uuid.UUID | None = None
    lock = SessionLock(db, session_id, trace_id)
    try:
        await lock.acquire()
    except SessionBusyError:
        replay = await _replay_advance_after_lock_conflict(
            db,
            session_id=sid,
            command_key=command_key,
            payload_digest=payload_digest,
        )
        if replay is not None:
            return replay
        raise
    replay_running = False
    try:
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
                    raise IdempotencyConflictError(
                        message="相同幂等键不能复用不同 advance 命令",
                        detail=f"session_id={session_id} command_id={command_key} payload_digest_mismatch",
                        retryable=False,
                    )
                if existing.status == "completed" and existing.response_payload is not None:
                    return dict(existing.response_payload)
                if existing.status == "running" and not _advance_claim_is_stale(existing):
                    raise _WaitForAdvanceReplay
                if existing.status == "failed":
                    existing.status = "running"
                    existing.error_code = None

            in_flight = await db.scalar(
                select(IntakeCommandClaim.id).where(
                    IntakeCommandClaim.session_id == sid,
                    IntakeCommandClaim.status == "running",
                    IntakeCommandClaim.idempotency_key != command_key,
                )
            )
            if in_flight is not None:
                raise SessionBusyError(
                    detail=f"session_id={session_id} already has an in-flight command",
                    retryable=True,
                )

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

            input_version = locked.state_version
            graph_run = await db.get(GraphRun, run_id)
            if graph_run is None:
                db.add(
                    GraphRun(
                        id=run_id,
                        session_id=sid,
                        graph_version=DEFAULT_GRAPH_VERSION,
                        command_id=command_key,
                        input_state_version=input_version,
                        status="running",
                    )
                )
            else:
                graph_run.input_state_version = input_version
                graph_run.status = "running"
                graph_run.completed_at = None
            db.add(
                OutboxEvent(
                    id=uuid.uuid4(),
                    event_type="advance.command_started.v1",
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
                    event_type="advance.started",
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
            intermediate_payload = {
                "advance": {
                    "from_stage": from_stage,
                    "trace_id": trace_id,
                    "source_gate_id": str(gate_id) if gate_id else None,
                    "source_gate_state_version": gate_state_version,
                }
            }
            if existing is None:
                db.add(
                    IntakeCommandClaim(
                        id=uuid.uuid4(),
                        session_id=sid,
                        idempotency_key=command_key,
                        payload_digest=payload_digest,
                        input_state_version=input_version,
                        status="running",
                        run_id=run_id,
                        intermediate_payload=intermediate_payload,
                    )
                )
            else:
                existing.status = "running"
                existing.input_state_version = input_version
                existing.output_state_version = None
                existing.response_payload = None
                existing.error_code = None
                existing.intermediate_payload = intermediate_payload
                existing.updated_at = func.now()
    except _WaitForAdvanceReplay:
        replay_running = True
    finally:
        await lock.release()
    if replay_running:
        return await _wait_for_completed_advance_response(
            db,
            session_id=sid,
            command_key=command_key,
            payload_digest=payload_digest,
        )
    assert run_id is not None
    try:
        await _invoke_reasoning_graph(session_id=session_id, command_key=command_key, run_id=run_id)
    except Exception as exc:
        await _mark_advance_failed(
            db,
            run_id=run_id,
            session_id=sid,
            command_key=command_key,
            error_code="REASONING_GRAPH_FAILED",
        )
        raise ModelGatewayUnavailableError(
            f"session_id={session_id} advance reasoning graph failed",
            retryable=True,
        ) from exc
    return await _completed_advance_response(
        db,
        session_id=sid,
        command_key=command_key,
        payload_digest=payload_digest,
    )


@router.post("/sessions/{session_id}/advance")
async def advance_session(
    request: Request,
    session_id: str,
    body: AdvanceRequest,
    db: AsyncSession = Depends(get_db),
    doctor_id: str | None = Depends(_doctor_id),
    state_version: int | None = Depends(_state_version),
    context: WriteRequestContext = Depends(write_request_context),
) -> JSONResponse:
    """阶段推进（§4.3.1）。

    问诊完备性充分后，调用此接口依次执行辨证→开方→加减→安全审核。
    安全审核通过后挂起等待医师确认（不进病历生成）。
    """
    del request
    trace_id = context.trace_id

    async def run_advance() -> dict[str, Any]:
        session = await _load_session_for_advance(db, session_id)
        if getattr(session, "agent_runtime", "legacy") == "langgraph":
            return await _run_langgraph_advance(
                db,
                session,
                session_id=session_id,
                state_version=state_version,
                trace_id=trace_id,
                force=body.force,
                idempotency_key=context.idempotency_key,
            )

        _precheck_stage(session, body.force)
        supervisor = Supervisor(db)
        supervisor_result: SupervisorResult = await supervisor.advance(
            session_id,
            trace_id,
            expected_state_version=state_version,
            force=body.force,
        )
        return {
            "session_id": session_id,
            "current_stage": (
                supervisor_result.to_stage.value
                if hasattr(supervisor_result.to_stage, "value")
                else str(supervisor_result.to_stage)
            ),
            "from_stage": (
                supervisor_result.from_stage.value
                if hasattr(supervisor_result.from_stage, "value")
                else str(supervisor_result.from_stage)
            ),
            "state_version": supervisor_result.state.state_version,
            "blocked_reason": supervisor_result.blocked_reason,
            "agent_name": supervisor_result.agent_name,
            "trace_id": trace_id,
        }

    scope = session_http_scope(session_id)
    result = await HttpCommandExecutor(db).execute(
        operation="session.advance.v1",
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
        handler=run_advance,
    )
    return JSONResponse(
        status_code=result.status_code,
        content=success_response(data=result.data, trace_id=trace_id, message=result.message),
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


async def advance_idempotency_conflict_handler(
    request: Request, exc: IdempotencyConflictError
) -> JSONResponse:
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


advance_exception_handlers: dict[Any, Any] = {
    SessionNotFoundError: advance_session_not_found_handler,
    SessionBusyError: advance_busy_handler,
    InvalidStateVersionError: advance_invalid_state_version_handler,
    InvalidStageTransitionError: advance_invalid_stage_handler,
    InsufficientInquiryError: insufficient_inquiry_handler,
    PendingDoctorReviewError: pending_doctor_review_handler,
    ModelGatewayUnavailableError: advance_model_gateway_handler,
    IdempotencyConflictError: advance_idempotency_conflict_handler,
}
