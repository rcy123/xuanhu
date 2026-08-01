"""LangGraph-backed intake message flow for L3-5.

This service owns request/API orchestration only.  Clinical facts are committed
through the L2 verifier/reducer/repository path, and model calls happen outside
database transactions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy import null as sql_null
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.checkpoint import postgres_checkpointer
from app.agent_runtime.commands import NODE_INTAKE_SUBGRAPH_V1, XuanhuCommand
from app.agent_runtime.completeness_policy import completeness_to_gate_result_schema, evaluate_completeness_policy
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.context import project_model_input_identity_sequences
from app.agent_runtime.ephemeral_cache import BoundedTTLCache
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.intake_fact_key_legality import (
    NormalizedObservation,
    RejectedObservation,
    filter_legal_observations,
    normalized_observations_to_payload,
    rejected_observations_to_payload,
)
from app.agent_runtime.intake_verifier import (
    INTAKE_AGENT_NAME,
    INTAKE_AGENT_VERSION,
    INTAKE_POLICY_VERSION,
    INTAKE_PROMPT_VERSION,
)
from app.agent_runtime.lifecycle import SharedLangGraphRuntime
from app.agent_runtime.reducer import DomainDelta, DomainReducerError, DomainState, reduce_domain_state
from app.agent_runtime.repository import (
    ConsultMessageSpec,
    GraphStepSpec,
    PostgresDomainRepository,
    RepositoryError,
    RepositoryErrorCode,
    SafetyFactAssertionSpec,
)
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.runtime import AgentRuntime
from app.agent_runtime.specs import AgentSpec, Capability, FailurePolicy, ModelPolicy, RunArtifact, RunSpec
from app.agent_runtime.state import ArtifactRef, XuanhuGraphState, default_state
from app.agent_runtime.triage_policy import evaluate_triage_policy, to_gate_result_schema
from app.agent_runtime.triage_precheck import (
    TRIAGE_PRECHECK_VERSION,
    TriagePrecheckResult,
    evaluate_raw_text_triage_precheck,
    merge_red_flag_candidates,
)
from app.agent_runtime.verifiers import DEFAULT_VERIFIER_CHAIN, VerificationContext
from app.agents.intake_extraction import (
    IntakeExecutionResult,
    IntakeExecutionStatus,
    execute_intake_extraction,
)
from app.agents.question_composer import (
    QUESTION_COMPOSER_POLICY_VERSION,
    compose_question,
    slot_followup_text,
)
from app.core.config import get_settings
from app.core.exceptions import (
    AgentTriggerFailedError,
    IdempotencyConflictError,
    InvalidStageTransitionError,
    InvalidStateVersionError,
    SessionBusyError,
    SessionNotFoundError,
    SessionTerminatedError,
    ValidationError,
)
from app.db.session import get_session_factory
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import (
    DomainCommandCommit,
    GraphRun,
    IntakeCommandClaim,
    OutboxEvent,
    SafetyFactAssertion,
)
from app.schemas.completeness import (
    CompletenessDisposition,
    CompletenessDomainSnapshot,
    CompletenessObservationFact,
    CompletenessPolicyInput,
    CompletenessProgress,
    CompletenessSafetyProfile,
    InquiryDimension,
)
from app.schemas.domain import (
    ArtifactRevisionSchema,
    ArtifactStatus,
    CollectionStatus,
    GateResultSchema,
    ObservationSchema,
    ObservationStatus,
)
from app.schemas.intake import (
    ActiveObservationContext,
    EvidenceSpan,
    IntakeExtractionDecision,
    IntakeExtractionInput,
    IntakeExtractionOutput,
    IntakeMessage,
    IntakeMessageRole,
    IntakeReplyContext,
    LactationDelta,
    ObservationOperation,
    PatientSafetyDelta,
    PregnancyDelta,
    SafetyListDelta,
)
from app.schemas.message import AgentMessageItem, MessageCreateRequest, MessageCreateResponse, SufficiencyReportData
from app.schemas.question import (
    QUESTION_COMPOSER_AGENT_VERSION,
    QUESTION_COMPOSER_PROMPT_VERSION,
    QuestionComposerClinicalFact,
    QuestionComposerTurn,
    QuestionCompositionOutcome,
    QuestionCompositionStatus,
)
from app.schemas.triage import TriageDisposition, TriagePolicyInput
from app.services.events import EventService
from app.services.safety_confirmation import build_intake_safety_assertion_specs
from app.services.session_lock import SessionLock

logger = logging.getLogger("xuanhu.langgraph_intake")

INTAKE_MESSAGE_CREATED = "intake.message_created.v1"
INTAKE_COMMAND_COMPLETED = "intake.command_completed.v1"
STALE_CLAIM_AFTER_SECONDS = 60
RETRYABLE_INTAKE_FAILURE_CODES = frozenset(
    {
        "MODEL_GATEWAY_TIMEOUT",
        "MODEL_GATEWAY_UNAVAILABLE",
        # guard 改软遮罩后应几乎不出现；留作兜底，降级走模板 follow-up 而非整条 failed。
        "MODEL_INPUT_PRIVACY_VIOLATION",
        # 网关超时的伴生情况（外层 RunSpec 先放弃）：降级而不硬停整条问诊。
        "RUN_DEADLINE_EXCEEDED",
        # 0d-2：模型输出坏 JSON / 截断属于「同输入重放可能成功」的随机失败，
        # 加入可重试集，使同一幂等键重放自动重开 claim（_reset_retryable_failed_claim），
        # 不再「previous intake command failed」永久拒绝。模型持续失败时仍会走到
        # recovery_status=manual_required → recover 转人工兜底，不会无限重试。
        "STRUCTURED_OUTPUT_INVALID",
        "MODEL_OUTPUT_TRUNCATED",
        "INTAKE_GROUNDING_SPAN_INVALID",
        "INTAKE_GROUNDING_VALUE_MISMATCH",
        # 模型 decision=extracted 但候选为空(重复提取被拒/合键遗漏)属输出质量问题。
        "INTAKE_DECISION_CONTENT_MISMATCH",
        # 模型偶发把身份类键(如 patient.name)当观察提取——重试大概率正常。
        "INTAKE_IDENTITY_FACT_FORBIDDEN",
        # 0d-2：composer 模型失败（自由措辞生成失败）同属随机失败，
        # 可重试重放；模板兜底仍由 compose_question 内部先承担（degraded 留痕）。
        "QUESTION_MODEL_OUTPUT_INVALID",
        "QUESTION_MODEL_UNAVAILABLE",
        "QUESTION_SINGLE_QUESTION_INVALID",
        "QUESTION_COMPOSER_FAILED",
        # 图级兜底错误码：底层多为节点级模型随机失败（last_failure_code 保留在
        # failure 上下文），同键重放大概率成功；manual_required 兜底防无限重试。
        "RUNNER_EXECUTION_FAILED",
        # 图级总超时(60s 预算内两段模型调用未完成)——重放(120s 预算)大概率成功。
        "RUNNER_TIMEOUT",
    }
)

# 0d-2 + 真实后端复盘(2026-08): 静默降级=「整轮不 503、退回 ABSTAINED/模板追问」。
# 瞬时网关/守卫类失败与模型输出质量失败(坏 JSON/截断/grounding span/decision 空)同属
# 单轮可重试或可退让的软失败——硬 503 会让安全项采集被模型随机性卡死。降级前先重试一次
# (见 _execute_intake_extraction_with_retry)，失败现场经 fallback_error_code 留痕，
# 不丢对话连续性；确定性硬风险阻断路径不受影响。
_INTAKE_SILENT_DEGRADE_CODES = frozenset(
    {
        "MODEL_GATEWAY_TIMEOUT",
        "MODEL_GATEWAY_UNAVAILABLE",
        "MODEL_INPUT_PRIVACY_VIOLATION",
        "RUN_DEADLINE_EXCEEDED",
        "STRUCTURED_OUTPUT_INVALID",
        "MODEL_OUTPUT_TRUNCATED",
        "INTAKE_GROUNDING_SPAN_INVALID",
        "INTAKE_GROUNDING_VALUE_MISMATCH",
        "INTAKE_GROUNDING_CONTEXT_UNSAFE",
        "INTAKE_DECISION_CONTENT_MISMATCH",
        "INTAKE_SAFETY_SEMANTICS_INVALID",
    }
)
# 模型质量类失败：同输入重放一次大概率成功(输出随机)，两次仍失败再降级。
_INTAKE_RETRYABLE_MODEL_CODES = frozenset(
    {
        "STRUCTURED_OUTPUT_INVALID",
        "MODEL_OUTPUT_TRUNCATED",
        "INTAKE_GROUNDING_SPAN_INVALID",
        "INTAKE_GROUNDING_VALUE_MISMATCH",
        "INTAKE_GROUNDING_CONTEXT_UNSAFE",
        "INTAKE_DECISION_CONTENT_MISMATCH",
        "INTAKE_SAFETY_SEMANTICS_INVALID",
    }
)
INTAKE_ROUTE_READY = "ready"
INTAKE_ROUTE_INCOMPLETE = "incomplete"
INTAKE_ROUTE_CONFLICT = "conflict"
INTAKE_ROUTE_MANUAL = "manual"
INTAKE_REPLY_BINDING_VERSION = "intake-reply-binding.v1"

_BOUND_EXPLICIT_NONE_PATTERN = re.compile(
    r"^\s*(?:无|没有|否|不是|未有|none|no)\s*[。.!！]?\s*$",
    re.IGNORECASE,
)
_SOCIAL_ACKNOWLEDGEMENT_PATTERN = re.compile(
    r"^\s*(?:你好|您好|嗨|哈喽|hello|hi|谢谢|感谢)(?:呀|啊|哦|哈)?\s*[。.!！?？]*\s*$",
    re.IGNORECASE,
)

_BOUND_REQUIRED_OBSERVATION_DIMENSIONS = frozenset(
    {
        InquiryDimension.CHIEF_COMPLAINT_SYMPTOM,
        InquiryDimension.BASIC_COURSE,
        InquiryDimension.PRESENT_ILLNESS_CHANGE,
        InquiryDimension.TEN_COLD_HEAT,
        InquiryDimension.TEN_SWEAT,
        InquiryDimension.TEN_HEAD_BODY,
        InquiryDimension.TEN_STOOL_URINE,
        InquiryDimension.TEN_DIET,
        InquiryDimension.TEN_CHEST_ABDOMEN,
        InquiryDimension.TEN_THIRST,
        InquiryDimension.TEN_SLEEP,
        InquiryDimension.TEN_MENSES_LEUKORRHEA,
        InquiryDimension.TEN_PAIN,
        InquiryDimension.TEN_RESPIRATORY,
    }
)


class _EmptyOutput(BaseModel):
    ok: bool = True


def _deadline(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _audit_event(
    session_id: uuid.UUID,
    event_type: str,
    actor_type: str,
    actor_id: str | None,
    payload: dict[str, Any],
    trace_id: str,
) -> AuditEvent:
    return AuditEvent(
        session_id=session_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        payload=payload,
        trace_id=trace_id,
    )


@dataclass(frozen=True)
class _ClaimResult:
    claim: IntakeCommandClaim
    message: ConsultMessage | None
    replay_response: MessageCreateResponse | None = None


_INTAKE_OUTPUT_CACHE: BoundedTTLCache[uuid.UUID, IntakeExtractionOutput] = BoundedTTLCache(
    max_size=256,
    ttl_seconds=300,
)

# 1a 主诉大类归集——并入终端单 commit。``_compute_intake_from_claim`` 在一个 claim
# 上会被 reduce / gates / terminal 多次调用，每次都要靠 `_classify_and_merge_category`
# 做一次决策。为避免重复消耗 complaint_classifier 模型调用（gate 一致性也要求每次复
# 判得到同一 category），首次决策的 (trace, category_observation) 进程内缓存以
# claim.id 为 key，TTL 与 _INTAKE_OUTPUT_CACHE 对齐（300s 覆盖 claim 生命周期）。
_CLASSIFY_TRACE_CACHE: BoundedTTLCache[uuid.UUID, tuple[Any, ObservationSchema]] = BoundedTTLCache(
    max_size=256,
    ttl_seconds=300,
)


@dataclass(frozen=True)
class _IntakeComputation:
    repository: PostgresDomainRepository
    domain_state: DomainState
    output: IntakeExtractionOutput
    delta: DomainDelta
    context: VerificationContext
    next_state: DomainState
    new_fact_count: int
    pending_safety_dimensions: tuple[InquiryDimension, ...]
    triage_result: Any
    progress: CompletenessProgress
    completeness_result: Any
    triage_gate: GateResultSchema
    completeness_gate: GateResultSchema
    classify_trace_payload: dict[str, Any] | None


class LangGraphIntakeMessageRunner:
    """Runs the versioned LangGraph intake flow for sessions fixed to langgraph."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        event_service: EventService | None = None,
        shared_runtime: SharedLangGraphRuntime | None = None,
        allow_request_local_runtime: bool = False,
    ) -> None:
        self._db = db
        self._event_service = event_service
        self._shared_runtime = shared_runtime
        self._allow_request_local_runtime = allow_request_local_runtime

    async def submit_message(
        self,
        session_id: str,
        body: MessageCreateRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
        x_state_version: int | None,
        idempotency_key: str | None = None,
    ) -> MessageCreateResponse:
        if self._shared_runtime is None and not self._allow_request_local_runtime:
            raise AgentTriggerFailedError(
                detail="shared LangGraph runtime is unavailable",
                agent_error_code="LANGGRAPH_RUNTIME_UNAVAILABLE",
                retryable=True,
            )
        sid = _parse_session_id(session_id)
        command_key = _command_key(idempotency_key)
        payload_digest = _payload_digest(body)
        claim = await self._claim_or_replay(
            sid,
            body,
            command_key=command_key,
            payload_digest=payload_digest,
            doctor_id=doctor_id,
            trace_id=trace_id,
            x_state_version=x_state_version,
        )
        if claim.replay_response is not None:
            return claim.replay_response
        if claim.message is None:
            return await self._wait_for_completed_claim(sid, command_key, payload_digest)

        graph_state = default_state(
            session_id=session_id,
            command=XuanhuCommand.MESSAGE.value,
            command_id=command_key,
            graph_version=DEFAULT_GRAPH_VERSION,
            run_id=str(claim.claim.run_id),
        )
        config = make_run_config(session_id, graph_version=DEFAULT_GRAPH_VERSION)
        # 3a/2.5: 单轮问诊串行 intake_extraction(35-55s)+ question_composer(5-35s),
        # 图级总超时需覆盖两段模型调用(60s 会 RUNNER_TIMEOUT);120s 留余量。
        INTAKE_GRAPH_TIMEOUT_SECONDS = 120
        try:
            if self._shared_runtime is not None:
                runner = self._shared_runtime.runner(timeout_seconds=INTAKE_GRAPH_TIMEOUT_SECONDS)
                await runner.ainvoke(dict(graph_state), config=config)
            elif self._allow_request_local_runtime:
                # Explicit fallback for direct service/integration-test invocation.
                # Production HTTP requests always receive the lifespan-owned state
                # and therefore never enter this branch.
                async with postgres_checkpointer(get_settings().database_url) as saver:
                    graph = build_main_graph(checkpointer=saver)
                    runner = GraphRunner(graph, timeout_seconds=INTAKE_GRAPH_TIMEOUT_SECONDS)
                    await runner.ainvoke(dict(graph_state), config=config)
            else:
                await self._mark_claim_failed(
                    claim.claim.id,
                    "LANGGRAPH_RUNTIME_UNAVAILABLE",
                )
                raise AgentTriggerFailedError(
                    detail="shared LangGraph runtime is unavailable",
                    agent_error_code="LANGGRAPH_RUNTIME_UNAVAILABLE",
                    retryable=True,
                )
        except AgentTriggerFailedError:
            raise
        except Exception as exc:
            # 0d-2：图级异常（GraphRunnerError 等）也必须落 claim=failed，
            # 否则 claim 永远 running → 会话永久 SESSION_BUSY 且 recover 无入口。
            # 错误码优先取节点层已写的 last_failure_code（如 INTAKE_GROUNDING_*，
            # 可重试），图级 code（RUNNER_EXECUTION_FAILED）仅作兜底上下文。
            payload = claim.claim.intermediate_payload if isinstance(claim.claim.intermediate_payload, dict) else {}
            node_failure_code = None
            if isinstance(payload.get("failure"), dict):
                node_failure_code = payload["failure"].get("last_failure_code")
            graph_code = getattr(exc, "code", None) or type(exc).__name__.upper()[:64]
            code = node_failure_code or graph_code
            await self._mark_claim_failed(
                claim.claim.id,
                code,
                failure_context={
                    "last_step": "graph_ainvoke",
                    "error_code": code,
                    "failed_node": "graph",
                    "exception_type": getattr(exc, "exception_type", None) or type(exc).__name__,
                    "graph_error_code": graph_code,
                },
            )
            raise
        return await self._wait_for_completed_claim(sid, command_key, payload_digest)

    async def _claim_or_replay(
        self,
        session_id: uuid.UUID,
        body: MessageCreateRequest,
        *,
        command_key: str,
        payload_digest: str,
        doctor_id: str | None,
        trace_id: str,
        x_state_version: int | None,
    ) -> _ClaimResult:
        lock = SessionLock(self._db, str(session_id), trace_id)
        try:
            await lock.acquire()
        except SessionBusyError:
            replay = await self._claim_from_busy_session(session_id, command_key, payload_digest)
            if replay is not None:
                return replay
            raise
        try:
            if self._db.in_transaction():
                await self._db.rollback()
            async with self._db.begin():
                existing = await self._db.scalar(
                    select(IntakeCommandClaim)
                    .where(
                        IntakeCommandClaim.session_id == session_id,
                        IntakeCommandClaim.idempotency_key == command_key,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    if existing.payload_digest != payload_digest:
                        raise IdempotencyConflictError(
                            message="相同幂等键不能复用不同消息",
                            detail=f"session_id={session_id} command_id={command_key} payload_digest_mismatch",
                            retryable=False,
                        )
                    if existing.status == "completed" and existing.response_payload is not None:
                        return _ClaimResult(existing, None, _response_from_payload(existing.response_payload))
                    if existing.status == "failed" and existing.error_code in RETRYABLE_INTAKE_FAILURE_CODES:
                        retry_message = await self._reset_retryable_failed_claim(existing)
                        if retry_message is not None:
                            return _ClaimResult(existing, retry_message)
                    if existing.status == "running" and _claim_is_stale(existing):
                        patient_message = None
                        if existing.patient_message_id is not None:
                            patient_message = await self._db.get(ConsultMessage, existing.patient_message_id)
                        if patient_message is not None:
                            existing.updated_at = func.now()
                            return _ClaimResult(existing, patient_message)
                    return _ClaimResult(existing, None)

                in_flight = await self._db.scalar(
                    select(IntakeCommandClaim.id).where(
                        IntakeCommandClaim.session_id == session_id,
                        IntakeCommandClaim.status == "running",
                        IntakeCommandClaim.idempotency_key != command_key,
                    )
                )
                if in_flight is not None:
                    raise SessionBusyError(
                        detail=f"session_id={session_id} already has an in-flight command",
                        retryable=True,
                    )

                session = await self._db.get(ConsultSession, session_id, with_for_update=True)
                if session is None:
                    raise SessionNotFoundError(detail=f"session_id={session_id} not found", retryable=False)
                if getattr(session, "agent_runtime", "legacy") != "langgraph":
                    raise InvalidStageTransitionError(
                        detail=f"session_id={session_id} is not a langgraph session",
                        retryable=False,
                    )
                if session.status == "terminated":
                    raise SessionTerminatedError(detail=f"session_id={session_id} has been terminated", retryable=False)
                if session.current_stage != "inquiry":
                    raise InvalidStageTransitionError(
                        message=f"当前阶段 {session.current_stage} 不允许提交消息",
                        detail=f"session_id={session_id} current_stage={session.current_stage}",
                        retryable=False,
                    )
                if x_state_version is not None and x_state_version != session.state_version:
                    raise InvalidStateVersionError(
                        detail=(
                            f"session_id={session_id} client version {x_state_version} "
                            f"!= server version {session.state_version}"
                        ),
                        retryable=True,
                    )

                reply_binding = await _resolve_reply_binding(
                    self._db,
                    session,
                    body.reply_to_message_id,
                )
                run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:intake:{session_id}:{command_key}")
                message = ConsultMessage(
                    session_id=session_id,
                    role=body.role,
                    stage=session.current_stage,
                    content=body.content,
                    structured_delta=(
                        {
                            "reply_context": reply_binding.model_dump(mode="json"),
                            "binding_version": INTAKE_REPLY_BINDING_VERSION,
                        }
                        if reply_binding is not None
                        else None
                    ),
                    trace_id=trace_id,
                )
                self._db.add(message)
                await self._db.flush()

                session.state_version += 1
                snapshot = dict(session.state_snapshot or {})
                snapshot["agent_runtime"] = "langgraph"
                snapshot["last_message"] = {
                    "message_id": str(message.id),
                    "role": body.role,
                    "stage": message.stage,
                    "preview": body.content[:200],
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                }
                session.state_snapshot = snapshot

                self._db.add(
                    GraphRun(
                        id=run_id,
                        session_id=session_id,
                        graph_version=DEFAULT_GRAPH_VERSION,
                        command_id=command_key,
                        input_state_version=session.state_version,
                        status="running",
                    )
                )
                claim = IntakeCommandClaim(
                    id=uuid.uuid4(),
                    session_id=session_id,
                    idempotency_key=command_key,
                    payload_digest=payload_digest,
                    input_state_version=session.state_version,
                    status="running",
                    run_id=run_id,
                    patient_message_id=message.id,
                )
                self._db.add(claim)
                self._db.add(
                    _audit_event(
                        session_id=session_id,
                        event_type="message.created",
                        actor_type="doctor" if doctor_id else "system",
                        actor_id=doctor_id,
                        payload={
                            "message_id": str(message.id),
                            "role": body.role,
                            "stage": message.stage,
                            "content_length": len(body.content),
                            "agent_runtime": "langgraph",
                        },
                        trace_id=trace_id,
                    )
                )
                self._db.add(
                    OutboxEvent(
                        id=uuid.uuid4(),
                        event_type=INTAKE_MESSAGE_CREATED,
                        session_id=session_id,
                        graph_run_id=run_id,
                        state_version=session.state_version,
                        trace_id=_stable_ref("trace", trace_id),
                        payload={
                            "session_id": str(session_id),
                            "message_id": str(message.id),
                            "role": body.role,
                            "stage": message.stage,
                            "content_length": len(body.content),
                        },
                    )
                )
            await self._db.refresh(message)
            await self._db.refresh(claim)
            await self._db.commit()
            return _ClaimResult(claim, message)
        except IntegrityError as exc:
            await self._db.rollback()
            raise SessionBusyError(detail=f"session_id={session_id} command claim conflict", retryable=True) from exc
        finally:
            await lock.release()

    async def _reset_retryable_failed_claim(
        self,
        claim: IntakeCommandClaim,
    ) -> ConsultMessage | None:
        """Atomically reopen a gateway-failed claim without accepting new input."""

        if claim.error_code not in RETRYABLE_INTAKE_FAILURE_CODES or claim.patient_message_id is None:
            return None
        patient_message = await self._db.get(ConsultMessage, claim.patient_message_id)
        session = await self._db.get(ConsultSession, claim.session_id, with_for_update=True)
        graph_run = await self._db.get(GraphRun, claim.run_id, with_for_update=True)
        committed = await self._db.scalar(
            select(DomainCommandCommit.id).where(DomainCommandCommit.graph_run_id == claim.run_id)
        )
        if (
            patient_message is None
            or patient_message.session_id != claim.session_id
            or session is None
            or session.status != "active"
            or session.current_stage != "inquiry"
            or session.state_version != claim.input_state_version
            or graph_run is None
            or graph_run.session_id != claim.session_id
            or graph_run.command_id != claim.idempotency_key
            or graph_run.input_state_version != claim.input_state_version
            or graph_run.status not in {"failed", "running"}
            or committed is not None
        ):
            return None
        other_running = await self._db.scalar(
            select(IntakeCommandClaim.id).where(
                IntakeCommandClaim.session_id == claim.session_id,
                IntakeCommandClaim.status == "running",
                IntakeCommandClaim.id != claim.id,
            )
        )
        if other_running is not None:
            raise SessionBusyError(
                detail=f"session_id={claim.session_id} already has an in-flight command",
                retryable=True,
            )

        claim.status = "running"
        claim.error_code = None
        claim.question_message_id = None
        claim.output_state_version = None
        claim.response_payload = cast(Any, sql_null())
        claim.updated_at = func.now()
        graph_run.status = "running"
        graph_run.completed_at = None
        _INTAKE_OUTPUT_CACHE.pop(claim.id, None)
        await self._db.flush()
        return patient_message

    async def _claim_from_busy_session(
        self,
        session_id: uuid.UUID,
        command_key: str,
        payload_digest: str,
    ) -> _ClaimResult | None:
        for _ in range(20):
            await asyncio.sleep(0.05)
            await self._db.rollback()
            existing = await self._db.scalar(
                select(IntakeCommandClaim).where(
                    IntakeCommandClaim.session_id == session_id,
                    IntakeCommandClaim.idempotency_key == command_key,
                )
            )
            if existing is None:
                continue
            if existing.payload_digest != payload_digest:
                raise IdempotencyConflictError(
                    message="相同幂等键不能复用不同消息",
                    detail=f"session_id={session_id} command_id={command_key} payload_digest_mismatch",
                    retryable=False,
                )
            if existing.status == "completed" and existing.response_payload is not None:
                return _ClaimResult(existing, None, _response_from_payload(existing.response_payload))
            return _ClaimResult(existing, None)
        return None

    async def _wait_for_completed_claim(
        self,
        session_id: uuid.UUID,
        command_key: str,
        payload_digest: str,
    ) -> MessageCreateResponse:
        for attempt in range(120):
            # A synchronous graph invocation normally completes the durable
            # claim before returning here.  Read once immediately; only an
            # actually in-flight replay needs the polling delay.
            if attempt > 0:
                await asyncio.sleep(0.25)
            await self._db.rollback()
            existing = await self._db.scalar(
                select(IntakeCommandClaim).where(
                    IntakeCommandClaim.session_id == session_id,
                    IntakeCommandClaim.idempotency_key == command_key,
                )
            )
            if existing is None:
                break
            if existing.payload_digest != payload_digest:
                raise IdempotencyConflictError(
                    detail=f"session_id={session_id} command_id={command_key} payload_digest_mismatch",
                    retryable=False,
                )
            if existing.status == "completed" and existing.response_payload is not None:
                return _response_from_payload(existing.response_payload)
            recovered = await self._recover_completed_claim(existing)
            if recovered is not None:
                return recovered
            if existing.status == "failed":
                retryable = existing.error_code in RETRYABLE_INTAKE_FAILURE_CODES
                raise AgentTriggerFailedError(
                    detail=f"session_id={session_id} previous intake command failed",
                    agent_error_code=existing.error_code or "LANGGRAPH_INTAKE_FAILED",
                    retryable=retryable,
                )
        raise SessionBusyError(detail=f"session_id={session_id} intake command is still running", retryable=True)

    async def _execute_after_claim(
        self,
        *,
        claim: IntakeCommandClaim,
        patient_message: ConsultMessage,
        trace_id: str,
        state: XuanhuGraphState,
    ) -> tuple[dict[str, Any], MessageCreateResponse]:
        try:
            computation = await _compute_intake_from_claim(
                claim,
                patient_message,
                trace_id,
                runner=self,
            )
            repository = computation.repository
            delta = computation.delta
            context = computation.context
            next_state = computation.next_state
            triage_result = computation.triage_result
            progress = computation.progress
            completeness_result = computation.completeness_result
            triage_gate = computation.triage_gate
            completeness_gate = computation.completeness_gate

            question_message_id: uuid.UUID | None = None
            question_spec: ConsultMessageSpec | None = None
            agent_item: AgentMessageItem | None = None
            if completeness_result.disposition in {
                CompletenessDisposition.INCOMPLETE,
                CompletenessDisposition.CONFLICT,
            }:
                question = await self._compose_question(
                    claim.session_id,
                    completeness_result,
                    computation.pending_safety_dimensions,
                    computation.next_state,
                    trace_id,
                    claim.idempotency_key,
                    claim.id,
                )
                if question is not None:
                    question_message_id = uuid.uuid4()
                    question_spec = ConsultMessageSpec(
                        message_id=question_message_id,
                        role="agent",
                        agent_name="question_composer",
                        stage="inquiry",
                        content=question["question"],
                        structured_delta=question,
                        trace_id=trace_id[:64],
                    )
                    agent_item = AgentMessageItem(
                        message_id=str(question_message_id),
                        role="agent",
                        agent_name="question_composer",
                        stage="inquiry",
                        content=question["question"],
                        created_at=None,
                    )
                    progress = progress.model_copy(update={"followup_rounds": progress.followup_rounds + 1})

            session_updates = _session_updates(
                completeness_result.disposition,
                triage_result.disposition,
                trace_id=trace_id,
                run_id=claim.run_id,
                patient_message=patient_message,
                agent_item=agent_item,
                triage_gate=triage_gate,
                completeness_gate=completeness_gate,
                progress=progress,
                pending_safety_dimensions=computation.pending_safety_dimensions,
                output_state_version=next_state.state_version,
            )
            await repository.commit(
                delta,
                context,
                graph_version=DEFAULT_GRAPH_VERSION,
                gate_results=(triage_gate, completeness_gate),
                graph_steps=_graph_steps(completeness_result.disposition),
                consult_messages=() if question_spec is None else (question_spec,),
                safety_fact_assertions=_safety_assertion_specs(
                    computation,
                    claim,
                    patient_message,
                    trace_id,
                ),
                session_updates=session_updates,
                outbox_event_type=INTAKE_COMMAND_COMPLETED,
                outbox_payload={
                    "session_id": str(claim.session_id),
                    "command_id": claim.idempotency_key,
                    "input_state_version": claim.input_state_version,
                    "output_state_version": next_state.state_version,
                    "triage_decision": triage_gate.decision.value,
                    "completeness_decision": completeness_gate.decision.value,
                    "completeness_disposition": (completeness_gate.details or {}).get("disposition"),
                    "patient_message_id": str(patient_message.id),
                    "question_message_id": str(question_message_id) if question_message_id else None,
                },
            )
            if question_message_id is not None:
                persisted_agent_item = await _load_agent_item(self._db, question_message_id)
                if persisted_agent_item is not None:
                    agent_item = persisted_agent_item
            patient_message_id = patient_message.id
            patient_role = patient_message.role
            patient_stage = patient_message.stage
            patient_content = patient_message.content
            patient_created_at = patient_message.created_at
            response = MessageCreateResponse(
                message_id=str(patient_message_id),
                session_id=str(claim.session_id),
                role=patient_role,
                stage=patient_stage,
                content=patient_content,
                current_stage=str(session_updates["current_stage"]),
                state_version=next_state.state_version,
                created_at=patient_created_at,
                agent_message=agent_item,
                sufficiency_report=_sufficiency_report(completeness_gate),
            )
            await self._complete_claim(claim.id, response, question_message_id, next_state.state_version)
            update = _graph_update(
                patient_message_id=patient_message_id,
                agent_item=agent_item,
                output_state_version=next_state.state_version,
                triage_gate=triage_gate,
                completeness_gate=completeness_gate,
            )
            return update, response
        except RepositoryError as exc:
            await self._mark_claim_failed(claim.id, exc.code.value)
            if exc.code is RepositoryErrorCode.STATE_VERSION_CONFLICT:
                raise InvalidStateVersionError(
                    detail=f"session_id={claim.session_id} stale intake command",
                    retryable=True,
                ) from exc
            raise
        except Exception as exc:
            # 0d-2：compose 等软失败 raise AgentTriggerFailedError 时也必须落 claim=failed，
            # 否则 claim 永远 running → 会话永久 SESSION_BUSY 且 recover 无入口。
            if not isinstance(exc, InvalidStateVersionError):
                await self._mark_claim_failed(
                    claim.id,
                    (
                        exc.agent_error_code
                        if isinstance(exc, AgentTriggerFailedError)
                        else type(exc).__name__.upper()[:64]
                    ),
                )
            raise

    async def _next_progress(
        self,
        session_id: uuid.UUID,
        *,
        new_fact_count: int,
        count_no_new_turn: bool = True,
    ) -> CompletenessProgress:
        session = await self._db.get(ConsultSession, session_id)
        raw: dict[str, Any] = {}
        if session is not None and isinstance(session.state_snapshot, dict):
            intake = session.state_snapshot.get("langgraph_intake")
            if isinstance(intake, dict) and isinstance(intake.get("progress"), dict):
                raw = cast(dict[str, Any], intake["progress"])
        previous = CompletenessProgress.model_validate(raw)
        no_new_facts_rounds = (
            0
            if new_fact_count
            else previous.no_new_facts_rounds + 1
            if count_no_new_turn
            else previous.no_new_facts_rounds
        )
        return CompletenessProgress(
            no_new_facts_rounds=no_new_facts_rounds,
            followup_rounds=previous.followup_rounds,
        )

    async def _compose_question(
        self,
        session_id: uuid.UUID,
        completeness_result: Any,
        pending_safety_dimensions: tuple[InquiryDimension, ...],
        domain_state: DomainState,
        trace_id: str,
        command_key: str,
        claim_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        run_spec = RunSpec(
            run_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:intake-question:{session_id}:{command_key}"),
            session_id=session_id,
            state_version=completeness_result.input_state_version,
            stage="intake_question",
            agent_spec_version=QUESTION_COMPOSER_AGENT_VERSION,
            prompt_version=QUESTION_COMPOSER_PROMPT_VERSION,
            policy_version=QUESTION_COMPOSER_POLICY_VERSION,
            # intake 节点级 RunSpec deadline 需 > 对应 AgentSpec ModelPolicy.timeout(75s)
            # 且 > MODEL_GATEWAY_TIMEOUT_SECONDS(60s)；保留余量给 recorder/console。
            deadline_at=_deadline(90),
            total_attempt_budget=1,
            idempotency_key=f"{command_key}:question",
            trace_id=trace_id,
        )
        recent_turns = await _recent_question_turns(self._db, session_id)
        outcome = await compose_question(
            completeness_result=completeness_result,
            pending_safety_dimensions=pending_safety_dimensions,
            clinical_context=_question_clinical_context(domain_state),
            runtime=AgentRuntime(),
            run_spec=run_spec,
            # 1b: 对话历史/主诉/激活维度集/缺口提示——writer 承接前文、贴合主诉、自由措辞
            recent_turns=recent_turns,
            chief_complaint=_chief_complaint_text(domain_state),
            activated_dimensions=_activated_dimension_values(completeness_result),
            missing_slot=_missing_slot_text(completeness_result, recent_turns),
        )
        if outcome.status is QuestionCompositionStatus.NO_QUESTION:
            return None
        if outcome.status is not QuestionCompositionStatus.SUCCEEDED or outcome.result is None:
            # 1b: compose 硬失败(模板缺失/契约不匹配)不再 raise 崩图——
            # 留痕 degraded 后本轮不追问(claim 正常 completed,下一轮再问)。
            code = str(outcome.failure_code or "QUESTION_COMPOSER_FAILED")
            await _save_intermediate(
                claim_id,
                {
                    "question_composer": {
                        "source": "template",
                        "degraded": True,
                        "source_kind": "question_composer",
                        "agent_run_id": str(run_spec.run_id),
                        "prompt_version": None,
                        "selection_kind": str(getattr(outcome.result, "selection_kind", "") or ""),
                        "template_version": None,
                        "selected_dimension": str(getattr(outcome.result, "selected_dimension", "") or ""),
                        "last_failure_code": code,
                    }
                },
                step="question_compose",
            )
            return None
        # 0a 模板兜底留痕：对称记录模型成功 / 模板退化，便于事后查"为什么这一轮是模板句"。
        await _save_intermediate(
            claim_id,
            {"question_composer": _question_composer_metadata(outcome, str(run_spec.run_id))},
            step="question_compose",
        )
        return outcome.result.model_dump(mode="json")

    async def _complete_claim(
        self,
        claim_id: uuid.UUID,
        response: MessageCreateResponse,
        question_message_id: uuid.UUID | None,
        output_state_version: int,
    ) -> None:
        payload = response.model_dump(mode="json")
        if self._db.in_transaction():
            await self._db.rollback()
        async with self._db.begin():
            claim = await self._db.get(IntakeCommandClaim, claim_id, with_for_update=True)
            if claim is None:
                raise SessionNotFoundError(detail=f"intake claim {claim_id} not found", retryable=False)
            claim.status = "completed"
            claim.question_message_id = question_message_id
            claim.output_state_version = output_state_version
            claim.response_payload = payload
            claim.updated_at = func.now()
        _INTAKE_OUTPUT_CACHE.pop(claim_id, None)

    async def _recover_completed_claim(self, claim: IntakeCommandClaim) -> MessageCreateResponse | None:
        if claim.status != "running":
            return None
        claim_id = claim.id
        if self._db.in_transaction():
            await self._db.rollback()
        async with self._db.begin():
            locked = await self._db.get(IntakeCommandClaim, claim_id, with_for_update=True)
            if locked is None:
                return None
            if locked.status == "completed" and locked.response_payload is not None:
                _INTAKE_OUTPUT_CACHE.pop(claim_id, None)
                return _response_from_payload(locked.response_payload)
            commit = await self._db.scalar(
                select(DomainCommandCommit).where(DomainCommandCommit.graph_run_id == locked.run_id)
            )
            if commit is None or locked.patient_message_id is None:
                return None
            patient_message = await self._db.get(ConsultMessage, locked.patient_message_id)
            session = await self._db.get(ConsultSession, locked.session_id)
            if patient_message is None or session is None:
                return None
            agent_item, question_message_id = await _load_last_agent_item(self._db, session)
            sufficiency_report = None
            snapshot = session.state_snapshot if isinstance(session.state_snapshot, dict) else {}
            raw_suff = snapshot.get("sufficiency_report")
            if isinstance(raw_suff, dict):
                sufficiency_report = SufficiencyReportData.model_validate(raw_suff)
            response = MessageCreateResponse(
                message_id=str(patient_message.id),
                session_id=str(locked.session_id),
                role=patient_message.role,
                stage=patient_message.stage,
                content=patient_message.content,
                current_stage=session.current_stage,
                state_version=commit.output_state_version,
                created_at=patient_message.created_at,
                agent_message=agent_item,
                sufficiency_report=sufficiency_report,
            )
            locked.status = "completed"
            locked.question_message_id = question_message_id
            locked.output_state_version = commit.output_state_version
            locked.response_payload = response.model_dump(mode="json")
            locked.updated_at = func.now()
            _INTAKE_OUTPUT_CACHE.pop(claim_id, None)
            return response

    async def _mark_claim_failed(
        self,
        claim_id: uuid.UUID,
        error_code: str,
        *,
        failure_context: dict[str, Any] | None = None,
    ) -> None:
        """标记 claim 失败并固化失败现场快照。

        ``failure_context`` 携带排障所需的非 PII 元数据（失败的图节点名、
        最后完成的步骤、模型调用尝试数、模型请求 run_id、error_code），
        合并进 ``intermediate_payload["failure"]`` 供事后定位，不污染成功路径。
        """
        if self._db.in_transaction():
            await self._db.rollback()
        async with self._db.begin():
            claim = await self._db.get(IntakeCommandClaim, claim_id, with_for_update=True)
            if claim is not None and claim.status != "completed":
                claim.status = "failed"
                # 0d-2: 节点层已写过具体错误码(如 INTAKE_GROUNDING_SPAN_INVALID)时
                # 不覆盖——图级兜底(如 RUNNER_EXECUTION_FAILED)只补上下文,
                # 保留可重试判定需要的精确错误码。
                if claim.error_code is None:
                    claim.error_code = error_code[:64]
                if failure_context:
                    payload = dict(claim.intermediate_payload or {})
                    failure = dict(payload.get("failure") or {})
                    failure.update(failure_context)
                    if claim.error_code is None:
                        failure["error_code"] = error_code[:64]
                    payload["failure"] = failure
                    claim.intermediate_payload = payload
                claim.updated_at = func.now()
                graph_run = await self._db.get(GraphRun, claim.run_id, with_for_update=True)
                if graph_run is not None and graph_run.status == "running":
                    graph_run.status = "failed"
                    graph_run.completed_at = func.now()
                # 0d-2：intake 失败 → session 进入可恢复状态（manual_required），
                # /recover 才能接管（recover 仅放行 manual_required/recovering）；
                # 重放成功路径 _complete_claim 会把它清回 normal。
                session = await self._db.get(ConsultSession, claim.session_id, with_for_update=True)
                if session is not None and session.status == "active" and session.recovery_status == "normal":
                    session.recovery_status = "manual_required"
                    session.updated_at = func.now()
        _INTAKE_OUTPUT_CACHE.pop(claim_id, None)


async def run_intake_persist_message_node(state: XuanhuGraphState) -> dict[str, Any]:
    try:
        session_id = uuid.UUID(state.get("session_id", ""))
    except (TypeError, ValueError):
        return _sanitized_graph_error(state, "INTAKE_COMMAND_REF_INVALID", "intake command session ref is invalid")
    command_id = state.get("command_id")
    if not command_id:
        return _sanitized_graph_error(state, "INTAKE_COMMAND_REF_INVALID", "intake command id is missing")

    factory = get_session_factory()
    async with factory() as db:
        runner = LangGraphIntakeMessageRunner(db)
        claim = await _load_intake_claim(db, session_id, command_id)
        if claim is None:
            return _sanitized_graph_error(state, "INTAKE_COMMAND_NOT_FOUND", "intake command claim was not found")
        completed = await _completed_graph_update(runner, claim)
        if completed is not None:
            return completed
        if claim.patient_message_id is None:
            return _sanitized_graph_error(state, "INTAKE_MESSAGE_NOT_FOUND", "intake patient message ref is missing")
        patient_message = await db.get(ConsultMessage, claim.patient_message_id)
        if patient_message is None:
            return _sanitized_graph_error(state, "INTAKE_MESSAGE_NOT_FOUND", "intake patient message was not found")
        await _save_intermediate_step(claim.id, "persist_message")
        return {
            "route": NODE_INTAKE_SUBGRAPH_V1,
            "artifact_refs": [{"kind": "message", "artifact_id": str(patient_message.id), "revision": 1}],
            "last_error": None,
        }


async def run_intake_triage_precheck_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_running_intake_context(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim, patient_message, runner = loaded
    try:
        completed = await _completed_graph_update(runner, claim)
        if completed is not None:
            return completed
        precheck = evaluate_raw_text_triage_precheck(patient_message.id, patient_message.content)
        await _save_intermediate(
            claim.id,
            {"triage_precheck": _triage_precheck_metadata(precheck)},
            step="triage_precheck",
        )
        return {"route": NODE_INTAKE_SUBGRAPH_V1, "last_error": None}
    finally:
        await db.close()


async def run_intake_build_context_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_running_intake_context(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim, patient_message, runner = loaded
    try:
        completed = await _completed_graph_update(runner, claim)
        if completed is not None:
            return completed
        repository = PostgresDomainRepository(get_session_factory())
        domain_state = await repository.get_state(claim.session_id)
        intake_input = _build_intake_input(domain_state, patient_message)
        await _save_intermediate(
            claim.id,
            {
                "build_context": {
                    "current_message_ids": [str(item.message_id) for item in intake_input.current_messages],
                    "historical_active_fact_count": len(intake_input.historical_active_facts),
                    "reply_question_message_id": (
                        str(intake_input.reply_context.question_message_id)
                        if intake_input.reply_context is not None
                        else None
                    ),
                    "reply_dimension": (
                        intake_input.reply_context.selected_dimension
                        if intake_input.reply_context is not None
                        else None
                    ),
                    "input_state_version": domain_state.state_version,
                },
            },
            step="build_intake_context",
        )
        return {
            "route": NODE_INTAKE_SUBGRAPH_V1,
            "domain_state_version": domain_state.state_version,
            "last_error": None,
        }
    finally:
        await db.close()


async def run_intake_extract_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_running_intake_context(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim, patient_message, runner = loaded
    try:
        completed = await _completed_graph_update(runner, claim)
        if completed is not None:
            return completed
        if claim.id in _INTAKE_OUTPUT_CACHE:
            await _save_intermediate_step(claim.id, "extract_intake")
            return {"route": NODE_INTAKE_SUBGRAPH_V1, "last_error": None}

        precheck = evaluate_raw_text_triage_precheck(patient_message.id, patient_message.content)
        if precheck.candidates:
            output = _precheck_blocking_output(precheck)
            _INTAKE_OUTPUT_CACHE[claim.id] = output
            await _save_intermediate(
                claim.id,
                {"extraction": _precheck_extraction_metadata(output, claim.input_state_version)},
                step="extract_intake",
            )
            return {"route": NODE_INTAKE_SUBGRAPH_V1, "last_error": None}

        repository = PostgresDomainRepository(get_session_factory())
        domain_state = await repository.get_state(claim.session_id)
        intake_input = _build_intake_input(domain_state, patient_message)
        bound_output = _bound_explicit_none_output(intake_input) or _bound_social_reply_output(intake_input)
        if bound_output is not None:
            _INTAKE_OUTPUT_CACHE[claim.id] = bound_output
            await _save_intermediate(
                claim.id,
                {
                    "extraction": _reply_binding_extraction_metadata(
                        bound_output,
                        claim.input_state_version,
                        intake_input,
                    )
                },
                step="extract_intake",
            )
            return {"route": NODE_INTAKE_SUBGRAPH_V1, "last_error": None}
        run_id = _stable_intake_extraction_run_id(claim)
        intake_result, success_run_id = await _execute_intake_extraction_with_retry(
            claim=claim,
            intake_input=intake_input,
            run_id=run_id,
            trace_id=_node_trace_id(state),
        )
        if intake_result.status is not IntakeExecutionStatus.SUCCEEDED or intake_result.output is None:
            code = str(intake_result.failure_code or "INTAKE_FAILED")
            fallback_output = _gateway_bound_reply_fallback_output(intake_input, code)
            if fallback_output is not None:
                _INTAKE_OUTPUT_CACHE[claim.id] = fallback_output
                await _save_intermediate(
                    claim.id,
                    {
                        "extraction": _reply_binding_extraction_metadata(
                            fallback_output,
                            claim.input_state_version,
                            intake_input,
                            fallback_error_code=code,
                        )
                    },
                    step="extract_intake",
                )
                return {"route": NODE_INTAKE_SUBGRAPH_V1, "last_error": None}
            await runner._mark_claim_failed(  # noqa: SLF001
                claim.id,
                code,
                failure_context={
                    "failed_node": "extract_intake",
                    "last_step": "extract_intake",
                    "model_run_id": str(success_run_id),
                    "model_agent_name": INTAKE_AGENT_NAME,
                    "degraded": True,
                    "last_failure_code": code,
                },
            )
            return _sanitized_graph_error(state, code, "intake extraction failed")
        _INTAKE_OUTPUT_CACHE[claim.id] = intake_result.output
        await _save_intermediate(
            claim.id,
            {"extraction": _extraction_metadata(success_run_id, intake_result.output, claim.input_state_version)},
            step="extract_intake",
        )
        return {"route": NODE_INTAKE_SUBGRAPH_V1, "last_error": None}
    finally:
        await db.close()


async def run_intake_verify_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_running_intake_context(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim, patient_message, runner = loaded
    try:
        completed = await _completed_graph_update(runner, claim)
        if completed is not None:
            return completed
        try:
            computation = await _compute_intake_from_claim(claim, patient_message, _node_trace_id(state))
        except KeyError:
            return _sanitized_graph_error(state, "INTAKE_EXTRACTION_MISSING", "intake extraction output is missing")
        except AgentTriggerFailedError as exc:
            # 0d-2/1b: verify 校验失败(如 INTAKE_GROUNDING_VALUE_MISMATCH)不能 raise 崩图——
            # 标记 claim=failed(具体错误码,可重试)并走 sanitized 路由,同 extract 失败路径。
            code = exc.agent_error_code or "INTAKE_VERIFICATION_FAILED"
            await runner._mark_claim_failed(  # noqa: SLF001
                claim.id,
                code,
                failure_context={
                    "failed_node": "verify_intake",
                    "last_step": "verify_intake",
                    "degraded": True,
                    "last_failure_code": code,
                },
            )
            return _sanitized_graph_error(state, code, "intake verification failed")
        except RepositoryError as exc:
            await runner._mark_claim_failed(claim.id, exc.code.value)  # noqa: SLF001
            raise
        await _save_intermediate(
            claim.id,
            {
                "verified": {
                    "delta_id": str(computation.delta.delta_id),
                    "input_state_version": claim.input_state_version,
                }
            },
            step="verify_intake",
        )
        return {"route": NODE_INTAKE_SUBGRAPH_V1, "last_error": None}
    finally:
        await db.close()


async def run_intake_reduce_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_running_intake_context(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim, patient_message, runner = loaded
    try:
        completed = await _completed_graph_update(runner, claim)
        if completed is not None:
            return completed
        try:
            computation = await _compute_intake_from_claim(claim, patient_message, _node_trace_id(state))
        except KeyError:
            return _sanitized_graph_error(state, "INTAKE_EXTRACTION_MISSING", "intake extraction output is missing")
        except AgentTriggerFailedError as exc:
            # 0d-2/1b: verify 校验失败不能 raise 崩图——标记 claim=failed(具体码可重试)。
            code = exc.agent_error_code or "INTAKE_VERIFICATION_FAILED"
            await runner._mark_claim_failed(  # noqa: SLF001
                claim.id,
                code,
                failure_context={
                    "failed_node": "verify_intake",
                    "last_step": "verify_intake",
                    "degraded": True,
                    "last_failure_code": code,
                },
            )
            return _sanitized_graph_error(state, code, "intake verification failed")
        await _save_intermediate(
            claim.id,
            {
                "reduced": {
                    "output_state_version": computation.next_state.state_version,
                    "new_fact_count": computation.new_fact_count,
                },
            },
            step="reduce_observations",
        )
        return {
            "route": NODE_INTAKE_SUBGRAPH_V1,
            "domain_state_version": computation.next_state.state_version,
            "last_error": None,
        }
    finally:
        await db.close()


async def run_intake_gates_node(state: XuanhuGraphState) -> dict[str, Any]:
    loaded = await _load_running_intake_context(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim, patient_message, runner = loaded
    try:
        completed = await _completed_graph_update(runner, claim)
        if completed is not None:
            return completed | {"intake_route": INTAKE_ROUTE_READY}
        try:
            computation = await _compute_intake_from_claim(claim, patient_message, _node_trace_id(state), runner=runner)
        except KeyError:
            return _sanitized_graph_error(state, "INTAKE_EXTRACTION_MISSING", "intake extraction output is missing")
        except AgentTriggerFailedError as exc:
            # 0d-2/1b: verify 校验失败不能 raise 崩图——标记 claim=failed(具体码可重试)。
            code = exc.agent_error_code or "INTAKE_VERIFICATION_FAILED"
            await runner._mark_claim_failed(  # noqa: SLF001
                claim.id,
                code,
                failure_context={
                    "failed_node": "verify_intake",
                    "last_step": "verify_intake",
                    "degraded": True,
                    "last_failure_code": code,
                },
            )
            return _sanitized_graph_error(state, code, "intake verification failed")
        intake_route = _route_for_disposition(computation.completeness_result.disposition)
        await _save_intermediate(
            claim.id,
            {
                "gates": {
                    "gate_refs": _gate_refs(computation.triage_gate, computation.completeness_gate),
                    "triage_decision": computation.triage_gate.decision.value,
                    "completeness_decision": computation.completeness_gate.decision.value,
                    "triage_disposition": computation.triage_result.disposition.value,
                    "completeness_disposition": computation.completeness_result.disposition.value,
                    "progress": computation.progress.model_dump(mode="json"),
                    "route": intake_route,
                    "output_state_version": computation.next_state.state_version,
                },
            },
            step="gates_and_route",
        )
        update = _graph_update(
            patient_message_id=patient_message.id,
            agent_item=None,
            output_state_version=computation.next_state.state_version,
            triage_gate=computation.triage_gate,
            completeness_gate=computation.completeness_gate,
        )
        return update | {"intake_route": intake_route}
    finally:
        await db.close()


async def run_intake_route_ready_node(state: XuanhuGraphState) -> dict[str, Any]:
    return await _finalize_intake_route(state, expected_route=INTAKE_ROUTE_READY)


async def run_intake_route_incomplete_node(state: XuanhuGraphState) -> dict[str, Any]:
    return await _finalize_intake_route(state, expected_route=INTAKE_ROUTE_INCOMPLETE)


async def run_intake_route_conflict_node(state: XuanhuGraphState) -> dict[str, Any]:
    return await _finalize_intake_route(state, expected_route=INTAKE_ROUTE_CONFLICT)


async def run_intake_route_manual_node(state: XuanhuGraphState) -> dict[str, Any]:
    return await _finalize_intake_route(state, expected_route=INTAKE_ROUTE_MANUAL)


async def run_recoverable_intake_node(state: XuanhuGraphState) -> dict[str, Any]:
    try:
        session_id = uuid.UUID(state.get("session_id", ""))
    except (TypeError, ValueError):
        return _sanitized_graph_error(state, "INTAKE_COMMAND_REF_INVALID", "intake command session ref is invalid")
    command_id = state.get("command_id")
    if not command_id:
        return _sanitized_graph_error(state, "INTAKE_COMMAND_REF_INVALID", "intake command id is missing")

    factory = get_session_factory()
    async with factory() as db:
        runner = LangGraphIntakeMessageRunner(db)
        claim = await db.scalar(
            select(IntakeCommandClaim).where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == command_id,
            )
        )
        if claim is None:
            return _sanitized_graph_error(state, "INTAKE_COMMAND_NOT_FOUND", "intake command claim was not found")
        if claim.status == "completed" and claim.response_payload is not None:
            return _graph_update_from_response(_response_from_payload(claim.response_payload))
        recovered = await runner._recover_completed_claim(claim)  # noqa: SLF001
        if recovered is not None:
            return _graph_update_from_response(recovered)
        if claim.patient_message_id is None:
            return _sanitized_graph_error(state, "INTAKE_MESSAGE_NOT_FOUND", "intake patient message ref is missing")
        patient_message = await db.get(ConsultMessage, claim.patient_message_id)
        if patient_message is None:
            return _sanitized_graph_error(state, "INTAKE_MESSAGE_NOT_FOUND", "intake patient message was not found")
        try:
            update, _ = await runner._execute_after_claim(  # noqa: SLF001
                claim=claim,
                patient_message=patient_message,
                trace_id=state.get("run_id") or command_id,
                state=state,
            )
        except AgentTriggerFailedError as exc:
            # 0d-2/1b: _execute_after_claim 内 verify/reduce 失败(AgentTriggerFailedError)
            # 不能 raise 崩图——标记 claim=failed(具体码可重试)并 sanitized 收尾。
            code = exc.agent_error_code or "INTAKE_VERIFICATION_FAILED"
            await runner._mark_claim_failed(  # noqa: SLF001
                claim.id,
                code,
                failure_context={
                    "failed_node": "finalize_intake",
                    "last_step": "finalize_intake",
                    "degraded": True,
                    "last_failure_code": code,
                },
            )
            return _sanitized_graph_error(state, code, "intake finalize failed")
        return update


def _node_trace_id(state: XuanhuGraphState) -> str:
    return state.get("run_id") or state.get("command_id") or ""


async def _load_intake_claim(
    db: AsyncSession,
    session_id: uuid.UUID,
    command_id: str,
) -> IntakeCommandClaim | None:
    return cast(
        IntakeCommandClaim | None,
        await db.scalar(
            select(IntakeCommandClaim).where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == command_id,
            )
        ),
    )


async def _load_running_intake_context(
    state: XuanhuGraphState,
) -> tuple[AsyncSession, IntakeCommandClaim, ConsultMessage, LangGraphIntakeMessageRunner] | dict[str, Any]:
    if state.get("last_error") is not None:
        return {"route": NODE_INTAKE_SUBGRAPH_V1, "last_error": state.get("last_error")}
    try:
        session_id = uuid.UUID(state.get("session_id", ""))
    except (TypeError, ValueError):
        return _sanitized_graph_error(state, "INTAKE_COMMAND_REF_INVALID", "intake command session ref is invalid")
    command_id = state.get("command_id")
    if not command_id:
        return _sanitized_graph_error(state, "INTAKE_COMMAND_REF_INVALID", "intake command id is missing")

    factory = get_session_factory()
    db = factory()
    await db.__aenter__()
    try:
        runner = LangGraphIntakeMessageRunner(db)
        claim = await _load_intake_claim(db, session_id, command_id)
        if claim is None:
            await db.__aexit__(None, None, None)
            return _sanitized_graph_error(state, "INTAKE_COMMAND_NOT_FOUND", "intake command claim was not found")
        if claim.patient_message_id is None:
            await db.__aexit__(None, None, None)
            return _sanitized_graph_error(state, "INTAKE_MESSAGE_NOT_FOUND", "intake patient message ref is missing")
        patient_message = await db.get(ConsultMessage, claim.patient_message_id)
        if patient_message is None:
            await db.__aexit__(None, None, None)
            return _sanitized_graph_error(state, "INTAKE_MESSAGE_NOT_FOUND", "intake patient message was not found")
        return db, claim, patient_message, runner
    except Exception:
        await db.__aexit__(None, None, None)
        raise


async def _completed_graph_update(
    runner: LangGraphIntakeMessageRunner,
    claim: IntakeCommandClaim,
) -> dict[str, Any] | None:
    del runner
    if claim.status == "completed" and claim.response_payload is not None:
        _INTAKE_OUTPUT_CACHE.pop(claim.id, None)
        return _graph_update_from_response(_response_from_payload(claim.response_payload))
    factory = get_session_factory()
    async with factory() as db:
        fresh = await db.get(IntakeCommandClaim, claim.id)
        if fresh is None:
            return None
        recovered = await LangGraphIntakeMessageRunner(db)._recover_completed_claim(fresh)  # noqa: SLF001
        if recovered is not None:
            return _graph_update_from_response(recovered)
    return None


async def _save_intermediate_step(claim_id: uuid.UUID, step: str) -> None:
    await _save_intermediate(claim_id, {}, step=step)


async def _save_intermediate(
    claim_id: uuid.UUID,
    patch: dict[str, Any],
    *,
    step: str,
) -> None:
    factory = get_session_factory()
    async with factory() as db:
        if db.in_transaction():
            await db.rollback()
        async with db.begin():
            claim = await db.get(IntakeCommandClaim, claim_id, with_for_update=True)
            if claim is None or claim.status == "completed":
                return
            payload = dict(claim.intermediate_payload or {})
            steps = dict(payload.get("steps") or {})
            steps[step] = "completed"
            payload["steps"] = steps
            payload.update(patch)
            claim.intermediate_payload = payload
            claim.updated_at = func.now()


async def _classify_and_merge_category(
    *,
    claim: IntakeCommandClaim,
    domain_state: DomainState,
    delta: DomainDelta,
    next_state: DomainState,
    context: VerificationContext,
    trace_id: str,
) -> tuple[Any, DomainDelta, DomainState, VerificationContext]:
    """1a 主诉大类归集：读 next_state 里的 active symptom → 调 complaint_classifier
    归大类 → 把 category 作为一条 ADD observation 并入同一个 delta，重算
    reduce_domain_state 得到含 category 的最终 next_state。

    返回 (trace, new_delta, new_next_state, new_context)。不落库、不 commit——
    交给路由终端 _finalize_intake_route 一次 commit 同时落 symptom + category。

    幂等（三档，按优先级）：
      1. 进程内 ``_CLASSIFY_TRACE_CACHE`` 命中：复用首决策的 (trace, category_obs)，
         仍把 category_obs 并入 delta（本轮 ``_compute_intake_from_claim`` 被多次调用，
         每次都要让 next_state 含 category 才能 completeness 一致）。source="cache"。
      2. next_state.observations 已有 active ``chief_complaint.category``（极少见，
         仅当 seed domain_state 已含 category）：skip 模型，不并入 delta。source="cache"
         + skipped=True。
      3. next_state.observations 无 active ``chief_complaint.symptom``（red_flag 短路
         或纯社交寒暄）：skip_no_symptom，不并入 delta，不调模型。

    模型失败一律 fail-safe 降级 ComplaintCategory.GENERAL + degraded=True 留痕，
    category obs 仍并入 delta（让下游十问退回 general 档而不是拿不到 category）。
    """
    from app.agents.complaint_classifier import (
        COMPLAINT_CLASSIFIER_AGENT_VERSION,
        COMPLAINT_CLASSIFIER_PROMPT_VERSION,
        ComplaintClassificationStatus,
        ComplaintClassifierFailureCode,
        execute_complaint_classification,
    )
    from app.schemas.completeness import ComplaintCategory
    from app.services.intake_classify_pipeline import (
        COMPLAINT_CLASSIFY_DELTA_RUN_SPEC_STAGE,
        COMPLAINT_CLASSIFY_POLICY_VERSION,
        _build_category_observation,
        _build_classification_input,
        _chief_complaint_symptom_fact,
        _classification_run_id,
        _ClassificationTrace,
        _existing_category_observations,
    )

    cached = _CLASSIFY_TRACE_CACHE.get(claim.id)
    if cached is not None:
        trace, category_obs = cached
        return (trace, *_merge_category_into_delta(delta, domain_state, category_obs, trace_id, claim.idempotency_key))

    if _existing_category_observations(next_state):
        trace = _ClassificationTrace(
            category=str(_existing_category_observations(next_state)[0].normalized_value or ""),
            source="cache",
            degraded=False,
            last_failure_code=None,
            agent_run_id=None,
            confidence=_existing_category_observations(next_state)[0].confidence,
            skipped=True,
        )
        return trace, delta, next_state, context

    symptom_facts = _chief_complaint_symptom_fact(next_state)
    if not symptom_facts:
        trace = _ClassificationTrace(
            category=ComplaintCategory.GENERAL.value,
            source="skip_no_symptom",
            degraded=False,
            last_failure_code=None,
            agent_run_id=None,
            confidence=None,
            skipped=True,
        )
        return trace, delta, next_state, context

    classification_input = _build_classification_input(symptom_facts[0], next_state)
    if classification_input is None:
        trace = _ClassificationTrace(
            category=ComplaintCategory.GENERAL.value,
            source="skip_no_symptom",
            degraded=False,
            last_failure_code=None,
            agent_run_id=None,
            confidence=None,
            skipped=True,
        )
        return trace, delta, next_state, context

    run_id = _classification_run_id(claim.session_id, claim.idempotency_key)
    run_spec = RunSpec(
        run_id=run_id,
        session_id=claim.session_id,
        state_version=claim.input_state_version,
        stage=COMPLAINT_CLASSIFY_DELTA_RUN_SPEC_STAGE,
        agent_spec_version=COMPLAINT_CLASSIFIER_AGENT_VERSION,
        prompt_version=COMPLAINT_CLASSIFIER_PROMPT_VERSION,
        policy_version=COMPLAINT_CLASSIFY_POLICY_VERSION,
        # 节点级 RunSpec deadline 需 > 对应 AgentSpec ModelPolicy.timeout(75s)
        # 且 > MODEL_GATEWAY_TIMEOUT_SECONDS(60s)；与 question_composer 惯例对齐（90s），
        # 保留余量给 recorder/console，避免网关配置变化时 deadline 先触发误归因降级。
        deadline_at=_deadline(90),
        total_attempt_budget=1,
        idempotency_key=f"{claim.idempotency_key}:classify",
        trace_id=trace_id,
    )
    result = await execute_complaint_classification(
        runtime=AgentRuntime(),
        run_spec=run_spec,
        input_payload=classification_input,
    )
    if result.status is ComplaintClassificationStatus.SUCCEEDED and result.output is not None:
        category = result.output.category
        confidence = float(result.output.confidence)
        source = "model"
        degraded = False
        failure_code: str | None = None
    else:
        # fail-safe：模型不可用/输出非法/grounding 失败一律降级 general，仍并入 delta。
        category = ComplaintCategory.GENERAL
        confidence = 0.0
        source = "model_degraded"
        degraded = True
        failure_code = (
            result.failure_code.value
            if result.failure_code is not None
            else ComplaintClassifierFailureCode.MODEL_OUTPUT_INVALID.value
        )

    category_obs = _build_category_observation(
        run_id=run_id,
        session_id=claim.session_id,
        category=category,
        source_message_id=symptom_facts[0].source_message_id,
        confidence=confidence,
    )
    trace = _ClassificationTrace(
        category=category.value,
        source=source,
        degraded=degraded,
        last_failure_code=failure_code,
        agent_run_id=str(run_spec.run_id),
        confidence=confidence,
        skipped=False,
    )
    _CLASSIFY_TRACE_CACHE[claim.id] = (trace, category_obs)
    return (trace, *_merge_category_into_delta(delta, domain_state, category_obs, trace_id, claim.idempotency_key))


def _merge_category_into_delta(
    delta: DomainDelta,
    domain_state: DomainState,
    category_obs: ObservationSchema,
    trace_id: str,
    idempotency_key: str,
) -> tuple[DomainDelta, DomainState, VerificationContext]:
    """把 category observation 追加进 delta.observations，扩 source_message_ids，
    重建 verification context，重算 reduce_domain_state 得到含 category 的 next_state。

    reducer 第 187-189 行要求每条 observation 的 source_message_id 必须在
    ``delta.source_message_ids`` 集合内。category 的 source 复用 symptom 的
    source_message_id（与主诉同源 seed 消息）；若该 seed 不在本轮 delta.source_message_ids
    内（复诊场景），需先并进去才能通过 OBSERVATION_SOURCE_UNDECLARED 校验。
    """
    merged_sources = delta.source_message_ids
    if category_obs.source_message_id not in merged_sources:
        merged_sources = merged_sources + (category_obs.source_message_id,)
    # 2c 修复: 分类合并给空 delta 补上 observations 后,必须清掉 intake_noop artifact
    # (reducer 禁止事实与工件混合变更 MIXED_FACT_AND_ARTIFACT_CHANGE——
    # 空提取时 _intake_output_to_delta 产 noop artifact,合并 category 后 facts 非空)。
    new_delta = delta.model_copy(
        update={
            "observations": delta.observations + (category_obs,),
            "source_message_ids": merged_sources,
            "artifact_revisions": () if not delta.observations else delta.artifact_revisions,
        }
    )
    new_context = _verification_context(
        delta=new_delta,
        state=domain_state,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
    )
    try:
        new_next_state = reduce_domain_state(domain_state, new_delta, new_context)
    except DomainReducerError as exc:
        # 0d-2: 分类合并后的 reduce 冲突同属模型输出问题,转 AgentTriggerFailedError。
        raise AgentTriggerFailedError(
            detail=f"intake category merge reduce failed code={exc.code.value}",
            agent_error_code=exc.code.value,
            retryable=False,
        ) from None
    return new_delta, new_next_state, new_context


async def _compute_intake_from_claim(
    claim: IntakeCommandClaim,
    patient_message: ConsultMessage,
    trace_id: str,
    *,
    runner: LangGraphIntakeMessageRunner | None = None,
) -> _IntakeComputation:
    repository = PostgresDomainRepository(get_session_factory())
    domain_state = await repository.get_state(claim.session_id)
    output = await _load_or_retry_intake_output(claim, patient_message, domain_state, trace_id)
    precheck = evaluate_raw_text_triage_precheck(patient_message.id, patient_message.content)
    rejected_observations: list[RejectedObservation] = []
    normalized_observations: list[NormalizedObservation] = []
    delta = _intake_output_to_delta(
        run_id=claim.run_id,
        session_id=claim.session_id,
        expected_state_version=claim.input_state_version,
        source_message_id=patient_message.id,
        state=domain_state,
        observations=output.observations,
        safety_delta=output.patient_safety_delta,
        rejected_observations=rejected_observations,
        normalized_observations=normalized_observations,
    )
    if rejected_observations or normalized_observations:
        extraction_trace: dict[str, Any] = {}
        if rejected_observations:
            extraction_trace["rejected_observations"] = rejected_observations_to_payload(rejected_observations)
        if normalized_observations:
            extraction_trace["normalized_observations"] = normalized_observations_to_payload(normalized_observations)
        await _save_intermediate(
            claim.id,
            {"extraction": extraction_trace},
            step="fact_key_legality_e1",
        )
    context = _verification_context(
        delta=delta,
        state=domain_state,
        trace_id=trace_id,
        idempotency_key=claim.idempotency_key,
    )
    try:
        next_state = reduce_domain_state(domain_state, delta, context)
    except DomainReducerError as exc:
        # 0d-2: reducer 冲突(如模型重复提取同键不同值 OBSERVATION_SOURCE_CONFLICT)
        # 是模型输出质量问题——统一转 AgentTriggerFailedError(带具体码),由各节点
        # catch 标记 claim=failed + sanitized,不再崩图/污染 checkpoint。
        raise AgentTriggerFailedError(
            detail=f"intake domain reduce failed code={exc.code.value}",
            agent_error_code=exc.code.value,
            retryable=False,
        ) from None
    # 1a 主诉大类归集并入终端单 commit：在 evaluate_completeness_policy 之前把
    # chief_complaint.category 作为一条 ADD observation 追加进 delta，重算一次
    # reduce_domain_state 得到含 category 的最终 next_state。这样下游
    # `_complaint_category()` 能读到真实 category（respiratory 等），动态十问维度
    # 才会按真实大类激活而非一律退回 general 档 4 维；终端 repository.commit 一次
    # 同时落 symptom + category，避免子图内部多一次 commit 带来的状态版本协调成本。
    classify_trace, delta, next_state, context = await _classify_and_merge_category(
        claim=claim,
        domain_state=domain_state,
        delta=delta,
        next_state=next_state,
        context=context,
        trace_id=trace_id,
    )
    # An unconfirmed candidate is conversational progress, but it is not an
    # authoritative SafetyProfile fact and therefore is absent from ``delta``.
    new_fact_count = len(delta.observations) + (1 if output.patient_safety_delta.has_candidate() else 0)
    triage_result = evaluate_triage_policy(
        TriagePolicyInput(
            input_state_version=next_state.state_version,
            red_flag_candidates=merge_red_flag_candidates(precheck.candidates, output.red_flag_candidates),
        )
    )
    if runner is None:
        factory = get_session_factory()
        async with factory() as db:
            progress = await LangGraphIntakeMessageRunner(db)._next_progress(  # noqa: SLF001
                claim.session_id,
                new_fact_count=new_fact_count,
                count_no_new_turn=not _is_social_acknowledgement(patient_message.content),
            )
    else:
        progress = await runner._next_progress(  # noqa: SLF001
            claim.session_id,
            new_fact_count=new_fact_count,
            count_no_new_turn=not _is_social_acknowledgement(patient_message.content),
        )
    if runner is None:
        factory = get_session_factory()
        async with factory() as db:
            pending_safety_dimensions = await _pending_safety_dimensions(
                db,
                claim.session_id,
                output.patient_safety_delta,
            )
    else:
        pending_safety_dimensions = await _pending_safety_dimensions(
            runner._db,  # noqa: SLF001
            claim.session_id,
            output.patient_safety_delta,
        )
    completeness_result = evaluate_completeness_policy(
        CompletenessPolicyInput(
            input_state_version=next_state.state_version,
            domain_snapshot=_completeness_snapshot(next_state),
            triage_gate=triage_result.gate_result,
            progress=progress,
            # 2c 灰度: 槽位口径 covered 判定由 settings 开关驱动(默认关闭=现状认键)。
            slot_based=get_settings().intake_slot_path_enabled,
        )
    )
    return _IntakeComputation(
        repository=repository,
        domain_state=domain_state,
        output=output,
        delta=delta,
        context=context,
        next_state=next_state,
        new_fact_count=new_fact_count,
        pending_safety_dimensions=pending_safety_dimensions,
        triage_result=triage_result,
        progress=progress,
        completeness_result=completeness_result,
        triage_gate=to_gate_result_schema(triage_result),
        completeness_gate=completeness_to_gate_result_schema(completeness_result),
        classify_trace_payload=classify_trace.to_payload(),
    )


async def _pending_safety_dimensions(
    db: AsyncSession,
    session_id: uuid.UUID,
    current_delta: PatientSafetyDelta,
) -> tuple[InquiryDimension, ...]:
    field_to_dimension = {
        "allergy": InquiryDimension.ALLERGY_STATUS,
        "pregnancy": InquiryDimension.PREGNANCY_STATUS,
        "lactation": InquiryDimension.LACTATION_STATUS,
        "medications": InquiryDimension.MEDICATION_STATUS,
        "major_conditions": InquiryDimension.MAJOR_CONDITION_STATUS,
    }
    pending_fields = set(
        await db.scalars(
            select(SafetyFactAssertion.field_name).where(
                SafetyFactAssertion.session_id == session_id,
                SafetyFactAssertion.status == "proposed",
            )
        )
    )
    for field_name, item in (
        ("allergy", current_delta.allergy),
        ("pregnancy", current_delta.pregnancy),
        ("lactation", current_delta.lactation),
        ("medications", current_delta.medications),
        ("major_conditions", current_delta.major_conditions),
    ):
        if item.status is not CollectionStatus.UNKNOWN:
            pending_fields.add(field_name)
    return tuple(
        sorted(
            {field_to_dimension[field_name] for field_name in pending_fields if field_name in field_to_dimension},
            key=lambda item: item.value,
        )
    )


async def _load_or_retry_intake_output(
    claim: IntakeCommandClaim,
    patient_message: ConsultMessage,
    domain_state: DomainState,
    trace_id: str,
) -> IntakeExtractionOutput:
    cached = _INTAKE_OUTPUT_CACHE.get(claim.id)
    if cached is not None:
        return cached
    precheck = evaluate_raw_text_triage_precheck(patient_message.id, patient_message.content)
    if precheck.candidates:
        output = _precheck_blocking_output(precheck)
        _INTAKE_OUTPUT_CACHE[claim.id] = output
        await _save_intermediate(
            claim.id,
            {"extraction": _precheck_extraction_metadata(output, claim.input_state_version)},
            step="extract_intake",
        )
        return output
    run_id = _stable_intake_extraction_run_id(claim)
    intake_input = _build_intake_input(domain_state, patient_message)
    bound_output = _bound_explicit_none_output(intake_input) or _bound_social_reply_output(intake_input)
    if bound_output is not None:
        _INTAKE_OUTPUT_CACHE[claim.id] = bound_output
        await _save_intermediate(
            claim.id,
            {
                "extraction": _reply_binding_extraction_metadata(
                    bound_output,
                    claim.input_state_version,
                    intake_input,
                )
            },
            step="extract_intake",
        )
        return bound_output
    intake_result, success_run_id = await _execute_intake_extraction_with_retry(
        claim=claim,
        intake_input=intake_input,
        run_id=run_id,
        trace_id=trace_id,
    )
    if intake_result.status is not IntakeExecutionStatus.SUCCEEDED or intake_result.output is None:
        failure_code = str(intake_result.failure_code or "INTAKE_FAILED")
        fallback_output = _gateway_bound_reply_fallback_output(intake_input, failure_code)
        if fallback_output is not None:
            _INTAKE_OUTPUT_CACHE[claim.id] = fallback_output
            await _save_intermediate(
                claim.id,
                {
                    "extraction": _reply_binding_extraction_metadata(
                        fallback_output,
                        claim.input_state_version,
                        intake_input,
                        fallback_error_code=failure_code,
                    )
                },
                step="extract_intake",
            )
            return fallback_output
        raise KeyError("extraction_output")
    _INTAKE_OUTPUT_CACHE[claim.id] = intake_result.output
    await _save_intermediate(
        claim.id,
        {
            "extraction": _extraction_metadata(
                success_run_id, intake_result.output, claim.input_state_version
            )
        },
        step="extract_intake",
    )
    return intake_result.output


def _stable_intake_extraction_run_id(claim: IntakeCommandClaim) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:intake-extraction:{claim.run_id}:{claim.idempotency_key}")


async def _execute_intake_extraction_with_retry(
    *,
    claim: IntakeCommandClaim,
    intake_input: IntakeExtractionInput,
    run_id: uuid.UUID,
    trace_id: str,
) -> tuple[IntakeExecutionResult, uuid.UUID]:
    """Run intake extraction once, then retry once on model-quality failures.

    Returns ``(result, success_run_id)``; ``success_run_id`` points at the call
    that actually succeeded (4bc10ac review should-fix), so the audited run id
    never references a failed attempt when a later retry succeeded.
    """

    def _run_spec(attempt_run_id: uuid.UUID, *, deadline_seconds: int, retry: bool) -> RunSpec:
        return RunSpec(
            run_id=attempt_run_id,
            session_id=claim.session_id,
            state_version=claim.input_state_version,
            stage="inquiry",
            agent_spec_version=INTAKE_AGENT_VERSION,
            prompt_version=INTAKE_PROMPT_VERSION,
            policy_version=INTAKE_POLICY_VERSION,
            deadline_at=_deadline(deadline_seconds),
            total_attempt_budget=1,
            idempotency_key=(
                f"{claim.idempotency_key}:intake:retry" if retry else f"{claim.idempotency_key}:intake"
            ),
            trace_id=trace_id,
        )

    first = await execute_intake_extraction(
        runtime=AgentRuntime(),
        run_spec=_run_spec(run_id, deadline_seconds=90, retry=False),
        input_payload=intake_input,
    )
    if first.status is IntakeExecutionStatus.SUCCEEDED:
        return first, run_id
    if str(first.failure_code or "") not in _INTAKE_RETRYABLE_MODEL_CODES:
        return first, run_id
    retry_run_id = uuid.uuid4()
    retried = await execute_intake_extraction(
        runtime=AgentRuntime(),
        run_spec=_run_spec(retry_run_id, deadline_seconds=150, retry=True),
        input_payload=intake_input,
    )
    if retried.status is IntakeExecutionStatus.SUCCEEDED:
        return retried, retry_run_id
    return retried, run_id


def _safety_assertion_specs(
    computation: _IntakeComputation,
    claim: IntakeCommandClaim,
    patient_message: ConsultMessage,
    trace_id: str,
) -> tuple[SafetyFactAssertionSpec, ...]:
    if not (computation.output.patient_safety_delta.has_candidate() or computation.output.red_flag_candidates):
        return ()
    precheck = evaluate_raw_text_triage_precheck(patient_message.id, patient_message.content)
    deterministic_precheck = bool(precheck.candidates)
    deterministic_reply = (
        _reply_context_from_message(patient_message) is not None
        and _BOUND_EXPLICIT_NONE_PATTERN.fullmatch(patient_message.content) is not None
        and computation.output.patient_safety_delta.has_candidate()
    )
    source_kind: Literal[
        "model_extraction",
        "deterministic_precheck",
        "deterministic_reply_binding",
    ]
    if deterministic_precheck:
        template_version = TRIAGE_PRECHECK_VERSION
        source_kind = "deterministic_precheck"
    elif deterministic_reply:
        template_version = INTAKE_REPLY_BINDING_VERSION
        source_kind = "deterministic_reply_binding"
    else:
        template_version = INTAKE_PROMPT_VERSION
        source_kind = "model_extraction"
    return build_intake_safety_assertion_specs(
        session_id=claim.session_id,
        source_message=patient_message,
        output=computation.output,
        extraction_run_id=_stable_intake_extraction_run_id(claim),
        template_version=template_version,
        source_kind=source_kind,
        trace_id=trace_id,
    )


def _extraction_metadata(
    run_id: uuid.UUID,
    output: IntakeExtractionOutput,
    input_state_version: int,
) -> dict[str, Any]:
    return {
        "agent_run_id": str(run_id),
        "idempotency_key_ref": "intake",
        "input_state_version": input_state_version,
        "output_digest": _fingerprint(output.model_dump(mode="json")),
        "decision": output.decision.value,
        "observation_count": len(output.observations),
        "red_flag_candidate_count": len(output.red_flag_candidates),
        "ambiguity_count": len(output.ambiguities),
        "safety_delta_present": output.patient_safety_delta.has_candidate(),
    }


def _triage_precheck_metadata(result: TriagePrecheckResult) -> dict[str, Any]:
    return {
        "policy_version": TRIAGE_PRECHECK_VERSION,
        "disposition": result.disposition.value,
        "candidate_count": len(result.candidates),
        "matched_rule_ids": list(result.matched_rule_ids),
        "candidate_digest": _fingerprint([item.model_dump(mode="json") for item in result.candidates]),
    }


def _precheck_blocking_output(result: TriagePrecheckResult) -> IntakeExtractionOutput:
    return IntakeExtractionOutput(
        decision=IntakeExtractionDecision.EXTRACTED,
        red_flag_candidates=result.candidates,
    )


def _precheck_extraction_metadata(output: IntakeExtractionOutput, input_state_version: int) -> dict[str, Any]:
    return {
        "source": "deterministic_triage_precheck",
        "policy_version": TRIAGE_PRECHECK_VERSION,
        "input_state_version": input_state_version,
        "output_digest": _fingerprint(output.model_dump(mode="json")),
        "decision": output.decision.value,
        "observation_count": 0,
        "red_flag_candidate_count": len(output.red_flag_candidates),
        "ambiguity_count": 0,
        "safety_delta_present": False,
    }


def _reply_binding_extraction_metadata(
    output: IntakeExtractionOutput,
    input_state_version: int,
    input_payload: IntakeExtractionInput,
    *,
    fallback_error_code: str | None = None,
) -> dict[str, Any]:
    """Trace metadata for deterministic/fallback extraction outputs.

    Reply-bound fallbacks keep the reply binding details; unbound degraded
    fallbacks (ABSTAINED after a model-quality failure) record the failure
    without pretending a reply binding exists.
    """
    context = input_payload.reply_context
    metadata = {
        "source": "deterministic_reply_binding" if context is not None else "degraded_fallback",
        "policy_version": INTAKE_REPLY_BINDING_VERSION if context is not None else "intake-degraded.v1",
        "input_state_version": input_state_version,
        "output_digest": _fingerprint(output.model_dump(mode="json")),
        "decision": output.decision.value,
        "observation_count": len(output.observations),
        "red_flag_candidate_count": len(output.red_flag_candidates),
        "ambiguity_count": len(output.ambiguities),
        "safety_delta_present": output.patient_safety_delta.has_candidate(),
    }
    if context is not None:
        metadata["reply_question_message_id"] = str(context.question_message_id)
        metadata["reply_dimension"] = context.selected_dimension
    if fallback_error_code is not None:
        metadata["fallback_error_code"] = fallback_error_code
        metadata["degraded"] = True
        metadata["last_failure_code"] = fallback_error_code
    return metadata


def _question_composer_metadata(outcome: QuestionCompositionOutcome, agent_run_id: str | None) -> dict[str, Any]:
    """0a 模板兜底留痕：把 composer outcome 的源 / 退化信号投影成可查询的 intermediate_payload 片段。

    - source: model/template —— omniscient 落库后事后可查"这一轮是模型直接的还是退到模板"
    - degraded + last_failure_code: 模板退化时记录模型那次失败的 failure code
      （归因 bug 修复后真实区分 UNAVAILABLE/OUTPUT_INVALID/SINGLE_QUESTION_INVALID/RUNTIME_CONTRACT 等）
    """
    result = outcome.result
    assert result is not None  # noqa: S101 - SUCCEEDED path only
    return {
        "source": result.source.value,
        "source_kind": "question_composer",
        "degraded": outcome.degraded,
        "last_failure_code": outcome.last_failure_code.value if outcome.last_failure_code is not None else None,
        "selected_dimension": result.selected_dimension.value,
        "selection_kind": result.selection_kind.value,
        "template_version": result.template_version,
        "prompt_version": result.prompt_version,
        "agent_run_id": agent_run_id,
    }


def _gate_refs(*gates: GateResultSchema) -> list[dict[str, Any]]:
    return [
        {
            "gate_name": gate.gate_name,
            "policy_version": gate.policy_version,
            "input_state_version": gate.input_state_version,
            "decision": gate.decision.value,
        }
        for gate in gates
    ]


def _route_for_disposition(disposition: CompletenessDisposition) -> str:
    if disposition is CompletenessDisposition.READY:
        return INTAKE_ROUTE_READY
    # 2d(决策 11 A 为主): PARTIAL 与 READY 同路由推进(带 partial 标记随 dossier 走,
    # 下游辨证降置信不跳过);安全项 cap 到仍走 STAGNATED → MANUAL。
    if disposition is CompletenessDisposition.PARTIAL:
        return INTAKE_ROUTE_READY
    if disposition is CompletenessDisposition.INCOMPLETE:
        return INTAKE_ROUTE_INCOMPLETE
    if disposition is CompletenessDisposition.CONFLICT:
        return INTAKE_ROUTE_CONFLICT
    return INTAKE_ROUTE_MANUAL


async def _finalize_intake_route(state: XuanhuGraphState, *, expected_route: str) -> dict[str, Any]:
    loaded = await _load_running_intake_context(state)
    if isinstance(loaded, dict):
        return loaded
    db, claim, patient_message, runner = loaded
    try:
        completed = await _completed_graph_update(runner, claim)
        if completed is not None:
            return completed
        gate_route = None
        if isinstance(claim.intermediate_payload, dict):
            gates = claim.intermediate_payload.get("gates")
            if isinstance(gates, dict):
                gate_route = gates.get("route")
        state_route = state.get("intake_route")
        if gate_route is not None and gate_route != expected_route:
            return _sanitized_graph_error(state, "INTAKE_ROUTE_MISMATCH", "intake route does not match gate result")
        if state_route is not None and state_route != expected_route:
            return _sanitized_graph_error(state, "INTAKE_ROUTE_MISMATCH", "intake route does not match graph state")

        try:
            computation = await _compute_intake_from_claim(
                claim,
                patient_message,
                _node_trace_id(state),
                runner=runner,
            )
        except KeyError:
            return _sanitized_graph_error(state, "INTAKE_EXTRACTION_MISSING", "intake extraction output is missing")
        except AgentTriggerFailedError as exc:
            # 0d-2/1b: verify 校验失败不能 raise 崩图——标记 claim=failed(具体码可重试)。
            code = exc.agent_error_code or "INTAKE_VERIFICATION_FAILED"
            await runner._mark_claim_failed(  # noqa: SLF001
                claim.id,
                code,
                failure_context={
                    "failed_node": "verify_intake",
                    "last_step": "verify_intake",
                    "degraded": True,
                    "last_failure_code": code,
                },
            )
            return _sanitized_graph_error(state, code, "intake verification failed")
        disposition = computation.completeness_result.disposition
        if _route_for_disposition(disposition) != expected_route:
            return _sanitized_graph_error(state, "INTAKE_ROUTE_MISMATCH", "intake route does not match recomputed gate")
        # 1a 主诉大类归集留痕：归集决策已并入 _compute_intake_from_claim（终端单 commit 内联），
        # 在此消费 computation.classify_trace_payload 把 category/source/degraded/agent_run_id
        # 写进 intermediate_payload["classify_complaint"]（steps["classify_complaint"]="completed"），
        # 与 0a 的 question_composer 留痕同构，事后可查"本轮主诉归到了哪类/是否退化"。
        if computation.classify_trace_payload is not None:
            await _save_intermediate(
                claim.id,
                {"classify_complaint": computation.classify_trace_payload},
                step="classify_complaint",
            )

        question_message_id: uuid.UUID | None = None
        question_spec: ConsultMessageSpec | None = None
        agent_item: AgentMessageItem | None = None
        progress = computation.progress
        if disposition in {CompletenessDisposition.INCOMPLETE, CompletenessDisposition.CONFLICT}:
            question = await runner._compose_question(  # noqa: SLF001
                claim.session_id,
                computation.completeness_result,
                computation.pending_safety_dimensions,
                computation.next_state,
                _node_trace_id(state),
                claim.idempotency_key,
                claim.id,
            )
            if question is not None:
                question_message_id = uuid.uuid4()
                question_spec = ConsultMessageSpec(
                    message_id=question_message_id,
                    role="agent",
                    agent_name="question_composer",
                    stage="inquiry",
                    content=question["question"],
                    structured_delta=question,
                    trace_id=_node_trace_id(state)[:64],
                )
                agent_item = AgentMessageItem(
                    message_id=str(question_message_id),
                    role="agent",
                    agent_name="question_composer",
                    stage="inquiry",
                    content=question["question"],
                    created_at=None,
                )
                progress = progress.model_copy(update={"followup_rounds": progress.followup_rounds + 1})

        session_updates = _session_updates(
            disposition,
            computation.triage_result.disposition,
            trace_id=_node_trace_id(state),
            run_id=claim.run_id,
            patient_message=patient_message,
            agent_item=agent_item,
            triage_gate=computation.triage_gate,
            completeness_gate=computation.completeness_gate,
            progress=progress,
            pending_safety_dimensions=computation.pending_safety_dimensions,
            output_state_version=computation.next_state.state_version,
        )
        try:
            await computation.repository.commit(
                computation.delta,
                computation.context,
                graph_version=DEFAULT_GRAPH_VERSION,
                gate_results=(computation.triage_gate, computation.completeness_gate),
                graph_steps=_graph_steps(disposition),
                consult_messages=() if question_spec is None else (question_spec,),
                safety_fact_assertions=_safety_assertion_specs(
                    computation,
                    claim,
                    patient_message,
                    _node_trace_id(state),
                ),
                session_updates=session_updates,
                outbox_event_type=INTAKE_COMMAND_COMPLETED,
                outbox_payload={
                    "session_id": str(claim.session_id),
                    "command_id": claim.idempotency_key,
                    "input_state_version": claim.input_state_version,
                    "output_state_version": computation.next_state.state_version,
                    "triage_decision": computation.triage_gate.decision.value,
                    "completeness_decision": computation.completeness_gate.decision.value,
                    "completeness_disposition": (computation.completeness_gate.details or {}).get("disposition"),
                    "patient_message_id": str(patient_message.id),
                    "question_message_id": str(question_message_id) if question_message_id else None,
                },
            )
        except RepositoryError as exc:
            await runner._mark_claim_failed(claim.id, exc.code.value)  # noqa: SLF001
            if exc.code is RepositoryErrorCode.STATE_VERSION_CONFLICT:
                raise InvalidStateVersionError(
                    detail=f"session_id={claim.session_id} stale intake command",
                    retryable=True,
                ) from exc
            raise

        if question_message_id is not None:
            persisted_agent_item = await _load_agent_item(db, question_message_id)
            if persisted_agent_item is not None:
                agent_item = persisted_agent_item
        patient_message_id = patient_message.id
        patient_role = patient_message.role
        patient_stage = patient_message.stage
        patient_content = patient_message.content
        patient_created_at = patient_message.created_at
        response = MessageCreateResponse(
            message_id=str(patient_message_id),
            session_id=str(claim.session_id),
            role=patient_role,
            stage=patient_stage,
            content=patient_content,
            current_stage=str(session_updates["current_stage"]),
            state_version=computation.next_state.state_version,
            created_at=patient_created_at,
            agent_message=agent_item,
            sufficiency_report=_sufficiency_report(computation.completeness_gate),
        )
        await runner._complete_claim(claim.id, response, question_message_id, computation.next_state.state_version)  # noqa: SLF001
        return _graph_update(
            patient_message_id=patient_message_id,
            agent_item=agent_item,
            output_state_version=computation.next_state.state_version,
            triage_gate=computation.triage_gate,
            completeness_gate=computation.completeness_gate,
        )
    finally:
        await db.close()


def _claim_is_stale(claim: IntakeCommandClaim) -> bool:
    updated_at = claim.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - updated_at > timedelta(seconds=STALE_CLAIM_AFTER_SECONDS)


async def _load_last_agent_item(
    db: AsyncSession,
    session: ConsultSession,
) -> tuple[AgentMessageItem | None, uuid.UUID | None]:
    snapshot = session.state_snapshot if isinstance(session.state_snapshot, dict) else {}
    intake = snapshot.get("langgraph_intake")
    question_id_raw = intake.get("last_question_message_id") if isinstance(intake, dict) else None
    if not question_id_raw:
        return None, None
    try:
        question_id = uuid.UUID(str(question_id_raw))
    except ValueError:
        return None, None
    message = await db.get(ConsultMessage, question_id)
    if message is None:
        return None, None
    return (
        AgentMessageItem(
            message_id=str(message.id),
            role="agent",
            agent_name=message.agent_name,
            stage=message.stage,
            content=message.content,
            created_at=message.created_at,
        ),
        message.id,
    )


async def _load_agent_item(db: AsyncSession, message_id: uuid.UUID) -> AgentMessageItem | None:
    """Load the canonical persisted message, including its database timestamp."""
    message = await db.get(ConsultMessage, message_id)
    if message is None:
        return None
    return AgentMessageItem(
        message_id=str(message.id),
        role="agent",
        agent_name=message.agent_name,
        stage=message.stage,
        content=message.content,
        created_at=message.created_at,
    )


def _graph_update_from_response(response: MessageCreateResponse) -> dict[str, Any]:
    artifact_refs: list[ArtifactRef] = [{"kind": "message", "artifact_id": response.message_id, "revision": 1}]
    if response.agent_message is not None:
        artifact_refs.append({"kind": "message", "artifact_id": response.agent_message.message_id, "revision": 1})
    return {
        "route": NODE_INTAKE_SUBGRAPH_V1,
        "domain_state_version": response.state_version,
        "artifact_refs": artifact_refs,
        "last_error": None,
    }


def _sanitized_graph_error(state: XuanhuGraphState, code: str, detail: str) -> dict[str, Any]:
    return {
        "route": NODE_INTAKE_SUBGRAPH_V1,
        "last_error": {
            "code": code,
            "trace_id": state.get("run_id", ""),
            "detail": detail,
        },
    }


def _parse_session_id(session_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(session_id)
    except ValueError as exc:
        raise SessionNotFoundError(detail=f"session_id={session_id} format is invalid", retryable=False) from exc


def _command_key(idempotency_key: str | None) -> str:
    """Derive a durable command key independently from the attempt trace."""

    logical_key = idempotency_key or uuid.uuid4().hex
    digest = hashlib.sha256(f"message\0{logical_key}".encode()).hexdigest()
    return f"command:{digest}"


def _payload_digest(body: MessageCreateRequest) -> str:
    payload = body.model_dump(mode="json")
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


async def resolve_durable_intake_message_response(
    session_id: str,
    body: MessageCreateRequest,
    *,
    idempotency_key: str,
    retry_failed_command: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Recover a result, or safely resume a gateway-failed internal claim."""

    sid = _parse_session_id(session_id)
    command_key = _command_key(idempotency_key)
    payload_digest = _payload_digest(body)
    factory = get_session_factory()
    retry_callback: Callable[[], Awaitable[dict[str, Any]]] | None = None
    response: MessageCreateResponse | None = None
    async with factory() as db:
        claim = await db.scalar(
            select(IntakeCommandClaim).where(
                IntakeCommandClaim.session_id == sid,
                IntakeCommandClaim.idempotency_key == command_key,
            )
        )
        if claim is None:
            return None
        if claim.payload_digest != payload_digest:
            raise IdempotencyConflictError(
                message="相同幂等键不能复用不同消息",
                detail=(f"session_id={session_id} command_id={command_key} payload_digest_mismatch"),
                retryable=False,
            )
        if claim.status == "completed" and isinstance(claim.response_payload, dict):
            response = _response_from_payload(claim.response_payload)
        elif (
            claim.status == "failed"
            and claim.error_code in RETRYABLE_INTAKE_FAILURE_CODES
            and retry_failed_command is not None
        ):
            retry_callback = retry_failed_command
        else:
            response = await LangGraphIntakeMessageRunner(db)._recover_completed_claim(claim)  # noqa: SLF001
    if retry_callback is not None:
        return await retry_callback()
    return None if response is None else response.model_dump(mode="json", exclude_none=True)


async def _resolve_reply_binding(
    db: AsyncSession,
    session: ConsultSession,
    requested_question_id: uuid.UUID | None,
) -> IntakeReplyContext | None:
    """Bind an answer only to the current canonical structured question."""

    snapshot = session.state_snapshot if isinstance(session.state_snapshot, dict) else {}
    intake = snapshot.get("langgraph_intake")
    raw_current_id = intake.get("last_question_message_id") if isinstance(intake, dict) else None
    current_question_id: uuid.UUID | None = None
    if raw_current_id:
        try:
            current_question_id = uuid.UUID(str(raw_current_id))
        except (TypeError, ValueError):
            current_question_id = None

    if requested_question_id is not None and requested_question_id != current_question_id:
        raise ValidationError(
            detail="reply_to_message_id does not reference the current intake question",
            retryable=False,
        )
    question_id = requested_question_id or current_question_id
    if question_id is None:
        return None

    question = await db.get(ConsultMessage, question_id)
    structured = question.structured_delta if question is not None else None
    if (
        question is None
        or question.session_id != session.id
        or question.role != "agent"
        or question.agent_name != "question_composer"
        or question.stage != "inquiry"
        or not isinstance(structured, dict)
    ):
        if requested_question_id is not None:
            raise ValidationError(detail="reply_to_message_id is not a valid intake question")
        return None

    try:
        return IntakeReplyContext.model_validate(
            {
                "question_message_id": question.id,
                "selected_dimension": InquiryDimension(str(structured["selected_dimension"])),
                "selection_kind": str(structured["selection_kind"]),
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        if requested_question_id is not None:
            raise ValidationError(detail="reply_to_message_id has invalid question metadata") from exc
        return None


def _stable_ref(prefix: str, value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._:-]+", "_", value).strip("._:-")
    if safe and len(safe) <= 96:
        return f"{prefix}:{safe}"
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _response_from_payload(payload: dict[str, Any]) -> MessageCreateResponse:
    return MessageCreateResponse.model_validate(payload)


def _build_intake_input(state: DomainState, message: ConsultMessage) -> IntakeExtractionInput:
    facts = tuple(
        ActiveObservationContext(
            observation_id=item.observation_id,
            fact_key=item.fact_key,
            value=item.value,
            normalized_value=item.normalized_value,
        )
        for item in _current_observations(state.observations)
        if item.value is not None or item.normalized_value is not None
    )
    return IntakeExtractionInput(
        current_messages=(
            IntakeMessage(message_id=message.id, role=IntakeMessageRole.PATIENT, content=message.content),
        ),
        historical_active_facts=facts[:128],
        reply_context=_reply_context_from_message(message),
    )


def _reply_context_from_message(message: ConsultMessage) -> IntakeReplyContext | None:
    structured = message.structured_delta if isinstance(message.structured_delta, dict) else {}
    raw = structured.get("reply_context")
    if not isinstance(raw, dict):
        return None
    try:
        return IntakeReplyContext.model_validate(raw)
    except (TypeError, ValueError):
        return None


def _bound_explicit_none_output(
    input_payload: IntakeExtractionInput,
) -> IntakeExtractionOutput | None:
    """Parse only an exact negative answer bound to one safety question."""

    context = input_payload.reply_context
    if context is None or len(input_payload.current_messages) != 1:
        return None
    message = input_payload.current_messages[0]
    if _BOUND_EXPLICIT_NONE_PATTERN.fullmatch(message.content) is None:
        return None
    span = EvidenceSpan(
        source_message_id=message.message_id,
        start_char=0,
        end_char=len(message.content),
        quote=message.content,
    )
    list_delta = SafetyListDelta(
        status=CollectionStatus.EXPLICITLY_NONE,
        source_message_id=message.message_id,
        negation_span=span,
    )
    safety_by_dimension: dict[InquiryDimension, PatientSafetyDelta] = {
        InquiryDimension.ALLERGY_STATUS: PatientSafetyDelta(allergy=list_delta),
        InquiryDimension.MEDICATION_STATUS: PatientSafetyDelta(medications=list_delta),
        InquiryDimension.MAJOR_CONDITION_STATUS: PatientSafetyDelta(major_conditions=list_delta),
        InquiryDimension.PREGNANCY_STATUS: PatientSafetyDelta(
            pregnancy=PregnancyDelta(
                status=CollectionStatus.EXPLICITLY_NONE,
                source_message_id=message.message_id,
                span=span,
            )
        ),
        InquiryDimension.LACTATION_STATUS: PatientSafetyDelta(
            lactation=LactationDelta(
                status=CollectionStatus.EXPLICITLY_NONE,
                source_message_id=message.message_id,
                span=span,
            )
        ),
    }
    try:
        selected_dimension = InquiryDimension(context.selected_dimension)
    except ValueError:
        return None
    safety_delta = safety_by_dimension.get(selected_dimension)
    if safety_delta is None:
        return None
    return IntakeExtractionOutput(
        decision=IntakeExtractionDecision.EXTRACTED,
        patient_safety_delta=safety_delta,
    )


def _bound_social_reply_output(
    input_payload: IntakeExtractionInput,
) -> IntakeExtractionOutput | None:
    """Keep greetings on the current question without invoking the model."""

    if input_payload.reply_context is None or len(input_payload.current_messages) != 1:
        return None
    if not _is_social_acknowledgement(input_payload.current_messages[0].content):
        return None
    return IntakeExtractionOutput(decision=IntakeExtractionDecision.ABSTAINED)


def _bound_required_reply_fallback_output(
    input_payload: IntakeExtractionInput,
) -> IntakeExtractionOutput | None:
    """Turn a gateway outage into a focused follow-up, never an inferred fact.

    The doctor's source message remains durable, but without a verified model
    extraction the clinical dimension stays incomplete.  Safety and conflict
    replies deliberately remain outside this availability fallback.
    """

    context = input_payload.reply_context
    if context is None or context.selection_kind != "required" or len(input_payload.current_messages) != 1:
        return None
    try:
        selected_dimension = InquiryDimension(context.selected_dimension)
    except ValueError:
        return None
    if selected_dimension not in _BOUND_REQUIRED_OBSERVATION_DIMENSIONS:
        return None
    return IntakeExtractionOutput(decision=IntakeExtractionDecision.NEEDS_CLARIFICATION)


def _gateway_bound_reply_fallback_output(
    input_payload: IntakeExtractionInput,
    failure_code: str,
) -> IntakeExtractionOutput | None:
    """Fallback for allowlisted soft intake failures (gateway/model quality).

    Degrade to a focused follow-up instead of failing the whole intake claim:
    - when the reply is bound to a required observation dimension, keep the
      existing NEEDS_CLARIFICATION template follow-up;
    - otherwise abstain (no facts are inferred, no safety/red-flag signal is
      fabricated) and let the completeness gate drive the next question.
    The degradation fact is recorded by the caller into the claim
    ``intermediate_payload`` (``fallback_error_code`` / ``degraded``).
    """

    if failure_code not in _INTAKE_SILENT_DEGRADE_CODES:
        return None
    bound = _bound_required_reply_fallback_output(input_payload)
    if bound is not None:
        return bound
    return IntakeExtractionOutput(decision=IntakeExtractionDecision.ABSTAINED)


def _is_social_acknowledgement(content: str) -> bool:
    """Do not treat a greeting-only doctor/patient turn as clinical stagnation."""

    return _SOCIAL_ACKNOWLEDGEMENT_PATTERN.fullmatch(content) is not None


def _active_observation_ids_by_fact_key(
    state: DomainState,
) -> dict[str, frozenset[str]]:
    """Index active observation ids by fact_key for E1 correction/retract降级判定。

    E1 闸门对越界 CORRECT/RETRACT 键会判断 ``target_observation_id`` 是否命中当前 state 里的
    active 事实——若命中则降级为伪 RETRACT 清掉历史畸键。本函数提供该索引（fact_key →
    active observation_id 集合）。
    """

    index: dict[str, frozenset[str]] = {}
    for item in _current_observations(state.observations):
        index[item.fact_key] = index.get(item.fact_key, frozenset()) | {str(item.observation_id)}
    return index


def _intake_output_to_delta(
    *,
    run_id: uuid.UUID,
    session_id: uuid.UUID,
    expected_state_version: int,
    source_message_id: uuid.UUID,
    state: DomainState,
    observations: tuple[Any, ...],
    safety_delta: PatientSafetyDelta,
    rejected_observations: list[RejectedObservation] | None = None,
    normalized_observations: list[NormalizedObservation] | None = None,
) -> DomainDelta:
    # E1 fact_key 合法性闸门：在落库前过滤越界畸键。抽取模型对同一临床语义会漂移出
    # schema 越界的 fact_key（trigger session d449735a 实测 ``symptom.cold_heat``、
    # 282a985a ``fever``/``symptom`` 裸键），这类畸键喂不进任何 canonical 维度也不命中 D1
    # 派生覆盖 → 对应维度永远 missing → gap_selector 锁死同一维度 → 命中写死模板 → 死循环。
    # 键桥再厚也追不上随机换键名的模型，故在抽取产出之后立 deterministic 闸门处置畸键：
    # ① ADD 命中 ``DERIVED_KEY_NORMALIZATION`` 归一 → 改写键名透传落库（b7bdf5ab 复现：
    #    ``symptom.chills``/``symptom.fever`` 若直接 reject 则寒热维度永采不到键 → 死循环；
    #    归一为 ``present_illness.chills``/``present_illness.fever`` 落库，D1 立判寒热覆盖）。
    # ② ADD 不命中归一表 → reject 留痕（d449735a / 282a985a 路径）。
    # ③ CORRECT/RETRACT 越界键 target 命中 active 畸键 → 降级伪 RETRACT 清历史脏（d449735a
    #    第 3 轮自治愈）。
    # reject / 归一留痕均回传调用方写进 claim intermediate_payload（与 D5 可观测铁律一致）。
    filter_result = filter_legal_observations(
        tuple(observations),
        active_observation_ids_by_fact_key=_active_observation_ids_by_fact_key(state),
    )
    legal_observations = filter_result.kept + filter_result.downgraded
    if rejected_observations is not None:
        rejected_observations.extend(filter_result.rejected)
    if normalized_observations is not None:
        normalized_observations.extend(filter_result.normalized)
    observation_schemas = tuple(
        ObservationSchema(
            observation_id=uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"xuanhu:observation:{run_id}:{index}:{item.fact_key}:{item.operation.value}",
            ),
            session_id=session_id,
            fact_key=item.fact_key,
            value=item.value,
            normalized_value=item.normalized_value,
            source_message_id=item.source_message_id,
            status=_observation_status(item.operation),
            confidence=item.confidence,
            supersedes_observation_id=item.target_observation_id,
            created_at=datetime.now(UTC),
        )
        for index, item in enumerate(legal_observations)
    )
    # High-risk model output is candidate-only.  It is persisted separately as
    # SafetyFactAssertion(proposed) and can reach SafetyProfile only through an
    # explicit, evidence-verified confirmation transition.
    del safety_delta
    if not observation_schemas:
        artifact_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:intake-empty:{run_id}")
        artifact_revisions: tuple[ArtifactRevisionSchema, ...] = (
            ArtifactRevisionSchema(
                artifact_id=artifact_id,
                artifact_type="intake_noop",
                revision=1,
                session_id=session_id,
                input_state_version=expected_state_version,
                status=ArtifactStatus.CURRENT,
                produced_by_run_id=run_id,
                created_at=datetime.now(UTC),
            ),
        )
    else:
        artifact_revisions = ()
    return DomainDelta(
        delta_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:delta:{run_id}"),
        run_id=run_id,
        session_id=session_id,
        expected_state_version=expected_state_version,
        source_message_ids=(source_message_id,),
        observations=observation_schemas,
        safety_profile=None,
        artifact_revisions=artifact_revisions,
    )


def _observation_status(operation: ObservationOperation) -> ObservationStatus:
    if operation is ObservationOperation.ADD:
        return ObservationStatus.ACTIVE
    if operation is ObservationOperation.CORRECT:
        return ObservationStatus.CORRECTED
    return ObservationStatus.RETRACTED


def _verification_context(
    *,
    delta: DomainDelta,
    state: DomainState,
    trace_id: str,
    idempotency_key: str,
) -> VerificationContext:
    run_spec = RunSpec(
        run_id=delta.run_id,
        session_id=delta.session_id,
        state_version=delta.expected_state_version,
        stage="intake_reduce",
        agent_spec_version="intake-domain-delta.v1",
        prompt_version="intake-domain-delta.v1",
        policy_version="intake-domain-delta-policy.v1",
        deadline_at=_deadline(30),
        total_attempt_budget=1,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )
    agent_spec = AgentSpec(
        name="intake_domain_delta",
        version="intake-domain-delta.v1",
        input_schema=_EmptyOutput,
        output_schema=DomainDelta,
        model_policy=ModelPolicy(model="deterministic-reducer", max_attempts=1),
        tool_permissions=frozenset({Capability.READ_STATE}),
        verifier_chain=tuple(verifier.name.value for verifier in DEFAULT_VERIFIER_CHAIN.verifiers),
        failure_policy=FailurePolicy(),
    )
    artifact = RunArtifact(
        output=delta,
        model_actual="deterministic-reducer",
        attempts=1,
        latency_ms=0,
        trace_id=trace_id,
        run_id=delta.run_id,
        agent_spec_version=agent_spec.version,
        prompt_version=run_spec.prompt_version,
    )
    report = DEFAULT_VERIFIER_CHAIN.verify(
        VerificationContext(
            agent_spec=agent_spec,
            run_spec=run_spec,
            artifact=artifact,
            state=state,
            allowed_source_message_ids=frozenset(delta.source_message_ids),
            allowed_stages=frozenset({"intake_reduce"}),
            satisfied_prerequisites=frozenset({"message_persisted"}),
        )
    )
    return (
        VerificationContext(
            agent_spec=agent_spec,
            run_spec=run_spec,
            artifact=artifact,
            state=state,
            allowed_source_message_ids=frozenset(delta.source_message_ids),
            allowed_stages=frozenset({"intake_reduce"}),
            satisfied_prerequisites=frozenset({"message_persisted"}),
        ).model_copy(update={"artifact": artifact})
        if report.passed
        else _raise_verification_failed(report.failure_code)
    )


def _raise_verification_failed(code: object) -> VerificationContext:
    raise AgentTriggerFailedError(
        detail=f"intake domain delta verification failed code={code}",
        agent_error_code=str(code or "VERIFICATION_FAILED"),
        retryable=False,
    )


def _current_observations(observations: tuple[ObservationSchema, ...]) -> tuple[ObservationSchema, ...]:
    superseded_ids = {
        item.supersedes_observation_id
        for item in observations
        if item.status is not ObservationStatus.ACTIVE and item.supersedes_observation_id is not None
    }
    return tuple(
        item
        for item in observations
        if item.observation_id not in superseded_ids and item.status is not ObservationStatus.RETRACTED
    )


def _completeness_snapshot(state: DomainState) -> CompletenessDomainSnapshot:
    facts = tuple(
        CompletenessObservationFact(
            observation_id=row.observation_id,
            session_id=row.session_id,
            fact_key=row.fact_key,
            value_fingerprint=_fingerprint(row.normalized_value if row.normalized_value is not None else row.value),
            normalized_code=_normalized_code(row.normalized_value if row.normalized_value is not None else row.value),
            status=row.status,
            supersedes_observation_id=row.supersedes_observation_id,
        )
        for row in state.observations
    )
    safety_profile = None
    if state.safety_profile is not None:
        safety = state.safety_profile
        safety_profile = CompletenessSafetyProfile(
            session_id=state.session_id,
            allergy_collection_status=safety.allergy_collection_status,
            allergen_count=len(safety.allergens or []),
            pregnancy_collection_status=safety.pregnancy_collection_status,
            lactation_collection_status=safety.lactation_collection_status,
            medications_collection_status=safety.medications_collection_status,
            medication_count=len(safety.medications or []),
            major_conditions_collection_status=safety.major_conditions_collection_status,
            major_condition_count=len(safety.major_conditions or []),
            contraindications_collection_status=safety.contraindications_collection_status,
            contraindication_count=len(safety.contraindications or []),
        )
    return CompletenessDomainSnapshot(
        session_id=state.session_id,
        state_version=state.state_version,
        observations=facts,
        safety_profile=safety_profile,
    )


def _session_updates(
    disposition: CompletenessDisposition,
    triage_disposition: TriageDisposition,
    *,
    trace_id: str,
    run_id: uuid.UUID,
    patient_message: ConsultMessage,
    agent_item: AgentMessageItem | None,
    triage_gate: GateResultSchema,
    completeness_gate: GateResultSchema,
    progress: CompletenessProgress,
    pending_safety_dimensions: tuple[InquiryDimension, ...],
    output_state_version: int,
) -> dict[str, object]:
    if disposition is CompletenessDisposition.READY or disposition in {
        CompletenessDisposition.INCOMPLETE,
        CompletenessDisposition.CONFLICT,
        # 2d(决策 11): PARTIAL 落库推进,不阻断(partial 缺口随快照走)。
        CompletenessDisposition.PARTIAL,
    }:
        current_stage = "inquiry"
        status = "active"
        recovery_status = "normal"
        blocked_reason = None
        blocked_at = None
    else:
        current_stage = "blocked"
        status = "blocked"
        recovery_status = "manual_required"
        blocked_reason = (
            f"triage_hold:{triage_disposition.value}"
            if disposition is CompletenessDisposition.TRIAGE_BLOCKED
            else "intake_stagnated_manual_required"
        )
        blocked_at = _naive_now()

    completeness_details = completeness_gate.details or {}
    missing_required = {str(item) for item in completeness_details.get("missing_required") or ()}
    pending_values = {item.value for item in pending_safety_dimensions}
    awaiting_safety_confirmation = bool(missing_required) and missing_required <= pending_values
    dialogue_status = (
        "awaiting_safety_confirmation"
        if disposition is CompletenessDisposition.INCOMPLETE and agent_item is None and awaiting_safety_confirmation
        else "questioning"
        if agent_item is not None
        # 2d(决策 11): PARTIAL 用独立标记(读模型不会误显示人工接管)。
        else "partial"
        if disposition is CompletenessDisposition.PARTIAL
        else "complete"
        if disposition is CompletenessDisposition.READY
        else "manual_required"
    )

    snapshot: dict[str, object] = {
        "agent_runtime": "langgraph",
        "current_stage": current_stage,
        "state_version": output_state_version,
        "recovery_status": recovery_status,
        "blocked_reason": blocked_reason,
        "sufficiency_report": _sufficiency_report(completeness_gate).model_dump(mode="json"),
        "langgraph_intake": {
            "version": "intake-subgraph.v1",
            "last_run_id": str(run_id),
            "last_patient_message_id": str(patient_message.id),
            "last_question_message_id": agent_item.message_id if agent_item else None,
            "triage": {
                "decision": triage_gate.decision.value,
                "policy_version": triage_gate.policy_version,
                "disposition": (triage_gate.details or {}).get("disposition"),
            },
            "completeness": {
                "decision": completeness_gate.decision.value,
                "policy_version": completeness_gate.policy_version,
                "disposition": (completeness_gate.details or {}).get("disposition"),
            },
            "progress": progress.model_dump(mode="json"),
            "dialogue_status": dialogue_status,
            "pending_safety_dimensions": [item.value for item in pending_safety_dimensions],
            # 2d(决策 11): PARTIAL 落库推进时带缺口列表,下游辨证降置信不跳过。
            "partial_dimensions": (
                sorted(missing_required) if disposition is CompletenessDisposition.PARTIAL else None
            ),
            "trace_id": trace_id,
        },
    }
    return {
        "current_stage": current_stage,
        "status": status,
        "recovery_status": recovery_status,
        "blocked_reason": blocked_reason,
        "blocked_at": blocked_at,
        "state_snapshot": snapshot,
    }


def _question_clinical_context(state: DomainState) -> tuple[QuestionComposerClinicalFact, ...]:
    """Project active non-identity clinical facts for natural question wording."""

    allowed_prefixes = (
        "chief_complaint.",
        "present_illness.",
        "ten_questions.",
        "past_history",
        "four_diagnosis",
    )
    facts: list[QuestionComposerClinicalFact] = []
    for item in sorted(_current_observations(state.observations), key=lambda row: row.fact_key):
        if not item.fact_key.startswith(allowed_prefixes):
            continue
        raw_value = item.normalized_value if item.normalized_value is not None else item.value
        if raw_value is None:
            continue
        value = (
            raw_value
            if isinstance(raw_value, str)
            else json.dumps(raw_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        value = value.strip()[:240]
        if not value:
            continue
        facts.append(QuestionComposerClinicalFact(fact_key=item.fact_key, value=value))
        if len(facts) == 24:
            break
    return tuple(facts)


# 1b: 维度 → 中文缺口提示(槽位缺口暂从 missing_required 派生,阶段 2 换槽位对象)
_DIMENSION_MISSING_HINTS: dict[str, str] = {
    "chief_complaint.symptom": "最主要的不适具体是什么",
    "basic_course": "主要不适已持续多久",
    "present_illness.change": "症状近期如何变化",
    "ten_questions.cold_heat": "怕冷、发热的情况",
    "ten_questions.sweat": "出汗情况",
    "ten_questions.head_body": "头身感受",
    "ten_questions.stool_urine": "二便情况",
    "ten_questions.diet": "饮食情况",
    "ten_questions.chest_abdomen": "胸腹部感受",
    "ten_questions.thirst": "口渴情况",
    "ten_questions.sleep": "睡眠情况",
    "ten_questions.menses_leukorrhea": "经带情况",
    "ten_questions.pain": "疼痛情况",
    "ten_questions.respiratory": "呼吸情况",
    "safety.allergy_status": "药物/食物过敏史",
    "safety.pregnancy_status": "是否处于妊娠状态",
    "safety.lactation_status": "是否处于哺乳期",
    "safety.medication_status": "当前用药情况",
    "safety.major_condition_status": "重要疾病史",
    "past_history": "既往病史",
    "four_diagnosis": "四诊信息",
    "patient.sex": "性别",
    "patient.age": "年龄",
}


def _missing_slot_text(
    completeness_result: object,
    recent_turns: tuple[QuestionComposerTurn, ...] = (),
) -> str | None:
    """1b: 从 completeness 的 missing_required 派生缺哪个槽位(首个缺口)。

    最近一轮患者回答已覆盖该维度部分子槽位时,优先给出「还缺哪一项」的精确提示,
    避免整维原句重复(真实后端 d190 复盘: 患者答过怕冷/怕风/发热仍被整维追问)。
    """
    missing = getattr(completeness_result, "missing_required", ())
    if not missing:
        return None
    dimension = missing[0]
    value = getattr(dimension, "value", str(dimension))
    base = _DIMENSION_MISSING_HINTS.get(value, value)
    if recent_turns:
        targeted = slot_followup_text(dimension, recent_turns)
        if targeted:
            return targeted
    return base


def _activated_dimension_values(completeness_result: object) -> tuple[str, ...]:
    """1b: 激活维度集 = covered + missing_required(全部 required 维度)。

    给 writer 作提案边界:只能在激活集内自然引导,安全项恒优先。
    """
    values: set[str] = set()
    for dim in getattr(completeness_result, "covered_dimensions", ()):
        values.add(getattr(dim, "value", str(dim)))
    for dim in getattr(completeness_result, "missing_required", ()):
        values.add(getattr(dim, "value", str(dim)))
    return tuple(sorted(values))


def _chief_complaint_text(state: DomainState) -> str | None:
    """1b: 主诉原文(chief_complaint.symptom 的 value),供首问/追问贴合主诉。"""
    for item in _current_observations(state.observations):
        if item.fact_key == "chief_complaint.symptom" and isinstance(item.value, str) and item.value.strip():
            return item.value.strip()[:2000]
    return None


async def _recent_question_turns(
    db: AsyncSession,
    session_id: uuid.UUID,
    limit: int = 6,
) -> tuple[QuestionComposerTurn, ...]:
    """1b: 最近 limit 条医患消息(agent 问句 + doctor/patient_proxy 回答)。

    身份遮罩后传给 writer,使其能承接前文、避免原话重复。
    """
    from sqlalchemy import select as _select

    rows = (
        (
            await db.execute(
                _select(ConsultMessage)
                .where(
                    ConsultMessage.session_id == session_id,
                    ConsultMessage.role.in_(("agent", "doctor", "patient_proxy")),
                )
                .order_by(ConsultMessage.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    ordered = list(reversed(rows))
    contents = [item.content for item in ordered]
    masked = project_model_input_identity_sequences(contents)
    turns: list[QuestionComposerTurn] = []
    for item, masked_content in zip(ordered, masked, strict=False):
        if not masked_content or not masked_content.strip():
            continue
        role = "patient" if item.role in {"doctor", "patient_proxy"} else "doctor"
        turns.append(QuestionComposerTurn(role=role, content=masked_content.strip()[:1000]))
        if len(turns) == 8:
            break
    return tuple(turns)


def _graph_steps(disposition: CompletenessDisposition) -> tuple[GraphStepSpec, ...]:
    return tuple(
        GraphStepSpec(step_name=name, status="completed", metadata={})
        for name in (
            "persist_message",
            "triage_precheck",
            "classify_complaint",
            "build_intake_context",
            "extract_intake",
            "verify_intake",
            "reduce_observations",
            "triage_gate",
            "completeness_gate",
            f"route:{disposition.value}",
        )
    )


def _sufficiency_report(completeness_gate: GateResultSchema) -> SufficiencyReportData:
    details = completeness_gate.details or {}
    return SufficiencyReportData(
        sufficient=details.get("disposition") == CompletenessDisposition.READY.value,
        covered=list(details.get("covered_dimensions") or []),
        missing=list(details.get("missing_required") or []),
        suggestions=[],
    )


def _graph_update(
    *,
    patient_message_id: uuid.UUID,
    agent_item: AgentMessageItem | None,
    output_state_version: int,
    triage_gate: GateResultSchema,
    completeness_gate: GateResultSchema,
) -> dict[str, Any]:
    artifact_refs: list[ArtifactRef] = [{"kind": "message", "artifact_id": str(patient_message_id), "revision": 1}]
    if agent_item is not None:
        artifact_refs.append({"kind": "message", "artifact_id": agent_item.message_id, "revision": 1})
    return {
        "route": NODE_INTAKE_SUBGRAPH_V1,
        "domain_state_version": output_state_version,
        "gate_results": [
            {
                "gate_name": triage_gate.gate_name,
                "decision": triage_gate.decision.value,
                "policy_version": triage_gate.policy_version,
            },
            {
                "gate_name": completeness_gate.gate_name,
                "decision": completeness_gate.decision.value,
                "policy_version": completeness_gate.policy_version,
            },
        ],
        "artifact_refs": artifact_refs,
        "last_error": None,
    }


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_CODE_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")


def _normalized_code(value: Any) -> str | None:
    raw: Any = value
    if isinstance(value, dict):
        raw = value.get("normalized_code") or value.get("code") or value.get("value")
    if isinstance(raw, bool):
        raw = "true" if raw else "false"
    if isinstance(raw, int | float):
        raw = str(raw)
    if not isinstance(raw, str):
        return None
    code = _CODE_RE.sub("_", raw.strip().lower()).strip("_")
    return code[:64] or None
