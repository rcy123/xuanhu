"""阶段推进 API 路由。

实现接口设计文档 §4.3.1：
- POST /api/v1/consult/sessions/{session_id}/advance

LangGraph 统一后端的 advance 入口(3d 后 legacy Supervisor 已下线)。
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
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy import null as sql_null
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.checkpoint import postgres_checkpointer
from app.agent_runtime.commands import XuanhuCommand
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.lifecycle import (
    LangGraphRuntimeUnavailableError,
    SharedLangGraphRuntime,
    allow_request_local_runtime_fallback,
)
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.state import default_state
from app.api.request_context import WriteRequestContext, get_trace_id, write_request_context
from app.core.config import get_settings
from app.core.exceptions import (
    AgentTriggerFailedError,
    IdempotencyConflictError,
    InsufficientInquiryError,
    InvalidStageTransitionError,
    InvalidStateVersionError,
    ModelGatewayUnavailableError,
    PendingDoctorReviewError,
    SessionBusyError,
    SessionNotFoundError,
    StateRecoveryRequiredError,
)
from app.db.session import get_db, get_session_factory
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.models.domain import (
    GateResult,
    GraphRun,
    IntakeCommandClaim,
    OutboxEvent,
    SafetyFactAssertion,
)
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


def _require_normal_recovery(session: ConsultSession) -> None:
    recovery_status = getattr(session, "recovery_status", "normal")
    if recovery_status != "normal":
        raise StateRecoveryRequiredError(
            detail=(
                f"session_id={session.id} recovery_status={recovery_status} "
                "must be recovered before advance"
            ),
            retryable=False,
        )


def _advance_command_key(idempotency_key: str | None) -> str:
    """Derive a durable command key independently from the attempt trace."""

    logical_key = idempotency_key or uuid.uuid4().hex
    digest = hashlib.sha256(f"advance\0{logical_key}".encode()).hexdigest()
    return f"advance:{digest}"


def _advance_payload_digest(force: bool) -> str:
    payload = {"command": "advance", "force": force}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


async def _read_durable_advance_response(
    *,
    session_id: uuid.UUID,
    command_key: str,
    payload_digest: str,
) -> dict[str, Any] | None:
    """Resolve an already-persisted command outcome without mutating claims."""

    factory = get_session_factory()
    async with factory() as db:
        claim = await db.scalar(
            select(IntakeCommandClaim).where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == command_key,
            )
        )
        if claim is None:
            return None
        if claim.payload_digest != payload_digest:
            raise IdempotencyConflictError(
                message="相同幂等键不能复用不同 advance 命令",
                detail=(
                    f"session_id={session_id} command_id={command_key} "
                    "payload_digest_mismatch"
                ),
                retryable=False,
            )
        if claim.status == "completed" and isinstance(claim.response_payload, dict):
            return dict(claim.response_payload)

    from app.agent_runtime.repository import RepositoryError, RepositoryErrorCode
    from app.services.langgraph_record import resolve_committed_record_advance

    try:
        return await resolve_committed_record_advance(
            session_id=session_id,
            command_id=command_key,
            payload_digest=payload_digest,
        )
    except RepositoryError as exc:
        if exc.code is RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED:
            raise IdempotencyConflictError(
                message="相同幂等键不能复用不同 advance 命令",
                detail=(
                    f"session_id={session_id} command_id={command_key} "
                    "payload_digest_mismatch"
                ),
                retryable=False,
            ) from exc
        raise


async def _repair_durable_advance_claim(
    *,
    session_id: uuid.UUID,
    command_key: str,
    payload_digest: str,
) -> dict[str, Any] | None:
    """Repair the internal claim only after a complete durable outcome exists."""

    response = await _read_durable_advance_response(
        session_id=session_id,
        command_key=command_key,
        payload_digest=payload_digest,
    )
    if response is None:
        return None
    from app.services.langgraph_review import _complete_advance_claim

    await _complete_advance_claim(
        session_id=session_id,
        command_id=command_key,
        response=response,
        state_version=int(response["state_version"]),
    )
    return response


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


async def _invoke_reasoning_graph(
    *,
    session_id: str,
    command_key: str,
    run_id: uuid.UUID,
    command: XuanhuCommand = XuanhuCommand.ADVANCE,
) -> None:
    graph_state = default_state(
        session_id=session_id,
        command=command.value,
        command_id=command_key,
        graph_version=DEFAULT_GRAPH_VERSION,
        run_id=str(run_id),
    )
    config = make_run_config(session_id, graph_version=DEFAULT_GRAPH_VERSION)
    async with postgres_checkpointer(get_settings().database_url) as saver:
        graph = build_main_graph(checkpointer=saver)
        runner = GraphRunner(graph, timeout_seconds=120)
        await runner.ainvoke(dict(graph_state), config=config)


async def _invoke_shared_reasoning_graph(
    runtime: SharedLangGraphRuntime,
    *,
    session_id: str,
    command_key: str,
    run_id: uuid.UUID,
    command: XuanhuCommand = XuanhuCommand.ADVANCE,
) -> None:
    """Invoke the lifespan-owned compiled graph without setup/recompile."""

    graph_state = default_state(
        session_id=session_id,
        command=command.value,
        command_id=command_key,
        graph_version=DEFAULT_GRAPH_VERSION,
        run_id=str(run_id),
    )
    config = make_run_config(session_id, graph_version=DEFAULT_GRAPH_VERSION)
    await runtime.runner(timeout_seconds=120).ainvoke(dict(graph_state), config=config)


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
        # 0d-2：失败可见可恢复——置 session.recovery_status=manual_required，
        # 使 /recover 能接管（仅放行 manual_required/recovering）。
        session = await db.get(ConsultSession, session_id, with_for_update=True)
        if session is not None and session.status == "active" and session.recovery_status == "normal":
            session.recovery_status = "manual_required"
            session.updated_at = func.now()


async def _run_langgraph_advance(
    db: AsyncSession,
    session: ConsultSession,
    *,
    session_id: str,
    state_version: int | None,
    trace_id: str,
    force: bool = False,
    idempotency_key: str | None = None,
    shared_runtime: SharedLangGraphRuntime | None = None,
    allow_request_local_runtime: bool = False,
) -> dict[str, Any]:
    _require_langgraph_runtime(shared_runtime, allow_request_local_runtime)
    sid = uuid.UUID(session_id)
    command_key = _advance_command_key(idempotency_key)
    payload_digest = _advance_payload_digest(force)
    _require_normal_recovery(session)
    durable = await _repair_durable_advance_claim(
        session_id=sid,
        command_key=command_key,
        payload_digest=payload_digest,
    )
    if durable is not None:
        return durable
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
            _require_normal_recovery(locked)
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
            graph_command = XuanhuCommand.ADVANCE
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
                pending_safety_assertion_id = await db.scalar(
                    select(SafetyFactAssertion.id)
                    .where(
                        SafetyFactAssertion.session_id == locked.id,
                        SafetyFactAssertion.status == "proposed",
                        # Red flags are owned by the triage/recovery boundary,
                        # not the generic safety-fact confirmation workflow.
                        SafetyFactAssertion.field_name != "red_flag",
                    )
                    .limit(1)
                )
                if pending_safety_assertion_id is not None:
                    raise InsufficientInquiryError(
                        detail=(
                            f"session_id={locked.id} LangGraph advance requires all "
                            "proposed safety facts to be explicitly resolved by a doctor"
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
            elif locked.current_stage in {"safety", "record"}:
                graph_command = XuanhuCommand.REVIEW
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
                existing.response_payload = cast(Any, sql_null())
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
        if shared_runtime is not None:
            await _invoke_shared_reasoning_graph(
                shared_runtime,
                session_id=session_id,
                command_key=command_key,
                run_id=run_id,
                command=graph_command,
            )
        elif allow_request_local_runtime:
            await _invoke_reasoning_graph(
                session_id=session_id,
                command_key=command_key,
                run_id=run_id,
                command=graph_command,
            )
        else:
            raise LangGraphRuntimeUnavailableError
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


def _require_langgraph_runtime(
    shared_runtime: SharedLangGraphRuntime | None,
    allow_request_local_runtime: bool,
) -> None:
    """Fail before any domain or HTTP-idempotency mutation."""

    if shared_runtime is None and not allow_request_local_runtime:
        raise ModelGatewayUnavailableError(
            "shared LangGraph runtime is unavailable",
            retryable=True,
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
    shared_runtime = runtime_state.runtime if runtime_state is not None else None
    trace_id = context.trace_id

    preflight_session = await _load_session_for_advance(db, session_id)
    preflight_session_id = uuid.UUID(session_id)
    durable_preflight: dict[str, Any] | None = None
    if getattr(preflight_session, "agent_runtime", "legacy") == "langgraph":
        _require_normal_recovery(preflight_session)
        preflight_command_key = _advance_command_key(context.idempotency_key)
        preflight_payload_digest = _advance_payload_digest(body.force)
        durable_preflight = await _repair_durable_advance_claim(
            session_id=preflight_session_id,
            command_key=preflight_command_key,
            payload_digest=preflight_payload_digest,
        )
        # Do not persist a retryable runtime-startup failure as the terminal
        # outcome for this HTTP idempotency key.
        if durable_preflight is None:
            _require_langgraph_runtime(shared_runtime, test_runtime_fallback)

    async def run_advance() -> dict[str, Any]:
        session = await _load_session_for_advance(db, session_id)
        if getattr(session, "agent_runtime", "langgraph") == "langgraph":
            return await _run_langgraph_advance(
                db,
                session,
                session_id=session_id,
                state_version=state_version,
                trace_id=trace_id,
                force=body.force,
                idempotency_key=context.idempotency_key,
                shared_runtime=shared_runtime,
                allow_request_local_runtime=test_runtime_fallback,
            )

        # 3d: legacy 路径已下线——历史 legacy session 仅兼容读,不再推进。
        raise AgentTriggerFailedError(
            detail=f"session_id={session_id} legacy runtime has been decommissioned; session is read-only",
            agent_error_code="LEGACY_RUNTIME_DECOMMISSIONED",
            retryable=False,
        )

    scope = session_http_scope(session_id)

    async def resolve_durable_outcome() -> dict[str, Any] | None:
        if durable_preflight is not None:
            return durable_preflight
        if getattr(preflight_session, "agent_runtime", "legacy") != "langgraph":
            return None
        return await _read_durable_advance_response(
            session_id=preflight_session_id,
            command_key=_advance_command_key(context.idempotency_key),
            payload_digest=_advance_payload_digest(body.force),
        )

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
        durable_outcome_resolver=resolve_durable_outcome,
    )
    return JSONResponse(
        status_code=result.status_code,
        content=success_response(data=result.data, trace_id=trace_id, message=result.message),
    )


# ---------------------------------------------------------------------------
# 异常处理器
# ---------------------------------------------------------------------------


async def advance_session_not_found_handler(request: Request, exc: SessionNotFoundError) -> JSONResponse:
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


async def advance_busy_handler(request: Request, exc: SessionBusyError) -> JSONResponse:
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


async def advance_invalid_state_version_handler(request: Request, exc: InvalidStateVersionError) -> JSONResponse:
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


async def advance_invalid_stage_handler(request: Request, exc: InvalidStageTransitionError) -> JSONResponse:
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


async def insufficient_inquiry_handler(request: Request, exc: InsufficientInquiryError) -> JSONResponse:
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


async def pending_doctor_review_handler(request: Request, exc: PendingDoctorReviewError) -> JSONResponse:
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


async def advance_model_gateway_handler(request: Request, exc: ModelGatewayUnavailableError) -> JSONResponse:
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


async def advance_idempotency_conflict_handler(request: Request, exc: IdempotencyConflictError) -> JSONResponse:
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
