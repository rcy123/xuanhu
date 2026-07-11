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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.checkpoint import postgres_checkpointer
from app.agent_runtime.commands import NODE_INTAKE_SUBGRAPH_V1, XuanhuCommand
from app.agent_runtime.completeness_policy import completeness_to_gate_result_schema, evaluate_completeness_policy
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.intake_verifier import INTAKE_AGENT_VERSION, INTAKE_PROMPT_VERSION
from app.agent_runtime.reducer import DomainDelta, DomainState, reduce_domain_state
from app.agent_runtime.repository import (
    ConsultMessageSpec,
    GraphStepSpec,
    PostgresDomainRepository,
    RepositoryError,
    RepositoryErrorCode,
)
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.runtime import AgentRuntime
from app.agent_runtime.specs import AgentSpec, Capability, FailurePolicy, ModelPolicy, RunArtifact, RunSpec
from app.agent_runtime.state import ArtifactRef, XuanhuGraphState, default_state
from app.agent_runtime.triage_policy import evaluate_triage_policy, to_gate_result_schema
from app.agent_runtime.verifiers import DEFAULT_VERIFIER_CHAIN, VerificationContext
from app.agents.intake_extraction import IntakeExecutionStatus, execute_intake_extraction
from app.agents.question_composer import compose_question
from app.core.config import get_settings
from app.core.exceptions import (
    AgentTriggerFailedError,
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
from app.models.domain import DomainCommandCommit, GraphRun, IntakeCommandClaim, OutboxEvent
from app.schemas.completeness import (
    CompletenessDisposition,
    CompletenessDomainSnapshot,
    CompletenessObservationFact,
    CompletenessPolicyInput,
    CompletenessProgress,
    CompletenessSafetyProfile,
)
from app.schemas.domain import (
    ArtifactRevisionSchema,
    ArtifactStatus,
    CollectionStatus,
    GateResultSchema,
    ObservationSchema,
    ObservationStatus,
    SafetyProfileSchema,
)
from app.schemas.intake import (
    ActiveObservationContext,
    IntakeExtractionInput,
    IntakeExtractionOutput,
    IntakeMessage,
    IntakeMessageRole,
    ObservationOperation,
    PatientSafetyDelta,
)
from app.schemas.message import AgentMessageItem, MessageCreateRequest, MessageCreateResponse, SufficiencyReportData
from app.schemas.question import QuestionCompositionStatus
from app.schemas.triage import TriageDisposition, TriagePolicyInput
from app.services.events import EventService
from app.services.session_lock import SessionLock

logger = logging.getLogger("xuanhu.langgraph_intake")

INTAKE_MESSAGE_CREATED = "intake.message_created.v1"
INTAKE_COMMAND_COMPLETED = "intake.command_completed.v1"
STALE_CLAIM_AFTER_SECONDS = 60
INTAKE_ROUTE_READY = "ready"
INTAKE_ROUTE_INCOMPLETE = "incomplete"
INTAKE_ROUTE_CONFLICT = "conflict"
INTAKE_ROUTE_MANUAL = "manual"


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


_INTAKE_OUTPUT_CACHE: dict[uuid.UUID, IntakeExtractionOutput] = {}


@dataclass(frozen=True)
class _IntakeComputation:
    repository: PostgresDomainRepository
    domain_state: DomainState
    output: IntakeExtractionOutput
    delta: DomainDelta
    context: VerificationContext
    next_state: DomainState
    new_fact_count: int
    triage_result: Any
    progress: CompletenessProgress
    completeness_result: Any
    triage_gate: GateResultSchema
    completeness_gate: GateResultSchema


class LangGraphIntakeMessageRunner:
    """Runs the versioned LangGraph intake flow for sessions fixed to langgraph."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        event_service: EventService | None = None,
    ) -> None:
        self._db = db
        self._event_service = event_service

    async def submit_message(
        self,
        session_id: str,
        body: MessageCreateRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
        x_state_version: int | None,
    ) -> MessageCreateResponse:
        sid = _parse_session_id(session_id)
        command_key = _command_key(trace_id)
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
        async with postgres_checkpointer(get_settings().database_url) as saver:
            graph = build_main_graph(checkpointer=saver)
            runner = GraphRunner(graph, timeout_seconds=60)
            await runner.ainvoke(dict(graph_state), config=config)
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
                        raise ValidationError(
                            message="相同幂等键不能复用不同消息",
                            detail=f"session_id={session_id} command_id={command_key} payload_digest_mismatch",
                            retryable=False,
                        )
                    if existing.status == "completed" and existing.response_payload is not None:
                        return _ClaimResult(existing, None, _response_from_payload(existing.response_payload))
                    if existing.status == "running" and _claim_is_stale(existing):
                        patient_message = None
                        if existing.patient_message_id is not None:
                            patient_message = await self._db.get(ConsultMessage, existing.patient_message_id)
                        if patient_message is not None:
                            existing.updated_at = func.now()
                            return _ClaimResult(existing, patient_message)
                    return _ClaimResult(existing, None)

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

                run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:intake:{session_id}:{command_key}")
                message = ConsultMessage(
                    session_id=session_id,
                    role=body.role,
                    stage=session.current_stage,
                    content=body.content,
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
                raise ValidationError(
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
        for _ in range(120):
            await asyncio.sleep(0.25)
            await self._db.rollback()
            existing = await self._db.scalar(
                select(IntakeCommandClaim).where(
                    IntakeCommandClaim.session_id == session_id,
                    IntakeCommandClaim.idempotency_key == command_key,
                )
            )
            if existing is None or existing.payload_digest != payload_digest:
                break
            if existing.status == "completed" and existing.response_payload is not None:
                return _response_from_payload(existing.response_payload)
            recovered = await self._recover_completed_claim(existing)
            if recovered is not None:
                return recovered
            if existing.status == "failed":
                raise AgentTriggerFailedError(
                    detail=f"session_id={session_id} previous intake command failed",
                    agent_error_code=existing.error_code or "LANGGRAPH_INTAKE_FAILED",
                    retryable=False,
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
            repository = PostgresDomainRepository(get_session_factory())
            domain_state = await repository.get_state(claim.session_id)
            intake_input = _build_intake_input(domain_state, patient_message)
            intake_result = await execute_intake_extraction(
                runtime=AgentRuntime(),
                run_spec=RunSpec(
                    run_id=uuid.uuid4(),
                    session_id=claim.session_id,
                    state_version=claim.input_state_version,
                    stage="inquiry",
                    agent_spec_version=INTAKE_AGENT_VERSION,
                    prompt_version=INTAKE_PROMPT_VERSION,
                    deadline_at=_deadline(30),
                    total_attempt_budget=1,
                    idempotency_key=f"{claim.idempotency_key}:intake",
                    trace_id=trace_id,
                ),
                input_payload=intake_input,
            )
            if intake_result.status is not IntakeExecutionStatus.SUCCEEDED or intake_result.output is None:
                code = str(intake_result.failure_code or "INTAKE_FAILED")
                await self._mark_claim_failed(claim.id, code)
                raise AgentTriggerFailedError(
                    detail=f"session_id={claim.session_id} intake extraction failed code={code}",
                    agent_error_code=code,
                    retryable=False,
                )

            delta = _intake_output_to_delta(
                run_id=claim.run_id,
                session_id=claim.session_id,
                expected_state_version=claim.input_state_version,
                source_message_id=patient_message.id,
                state=domain_state,
                observations=intake_result.output.observations,
                safety_delta=intake_result.output.patient_safety_delta,
            )
            context = _verification_context(
                delta=delta,
                state=domain_state,
                trace_id=trace_id,
                idempotency_key=claim.idempotency_key,
            )
            next_state = reduce_domain_state(domain_state, delta, context)
            new_fact_count = len(delta.observations) + (1 if delta.safety_profile is not None else 0)
            triage_result = evaluate_triage_policy(
                TriagePolicyInput(
                    input_state_version=next_state.state_version,
                    red_flag_candidates=intake_result.output.red_flag_candidates,
                )
            )
            progress = await self._next_progress(claim.session_id, new_fact_count=new_fact_count)
            completeness_result = evaluate_completeness_policy(
                CompletenessPolicyInput(
                    input_state_version=next_state.state_version,
                    domain_snapshot=_completeness_snapshot(next_state),
                    triage_gate=triage_result.gate_result,
                    progress=progress,
                )
            )
            triage_gate = to_gate_result_schema(triage_result)
            completeness_gate = completeness_to_gate_result_schema(completeness_result)

            question_message_id: uuid.UUID | None = None
            question_spec: ConsultMessageSpec | None = None
            agent_item: AgentMessageItem | None = None
            if completeness_result.disposition in {
                CompletenessDisposition.INCOMPLETE,
                CompletenessDisposition.CONFLICT,
            }:
                question = await self._compose_question(claim.session_id, completeness_result, trace_id)
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
                output_state_version=next_state.state_version,
            )
            await repository.commit(
                delta,
                context,
                graph_version=DEFAULT_GRAPH_VERSION,
                gate_results=(triage_gate, completeness_gate),
                graph_steps=_graph_steps(completeness_result.disposition),
                consult_messages=() if question_spec is None else (question_spec,),
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
            if not isinstance(exc, (AgentTriggerFailedError, InvalidStateVersionError)):
                await self._mark_claim_failed(claim.id, type(exc).__name__.upper()[:64])
            raise

    async def _next_progress(self, session_id: uuid.UUID, *, new_fact_count: int) -> CompletenessProgress:
        session = await self._db.get(ConsultSession, session_id)
        raw: dict[str, Any] = {}
        if session is not None and isinstance(session.state_snapshot, dict):
            intake = session.state_snapshot.get("langgraph_intake")
            if isinstance(intake, dict) and isinstance(intake.get("progress"), dict):
                raw = cast(dict[str, Any], intake["progress"])
        previous = CompletenessProgress.model_validate(raw)
        return CompletenessProgress(
            no_new_facts_rounds=0 if new_fact_count else previous.no_new_facts_rounds + 1,
            followup_rounds=previous.followup_rounds,
        )

    async def _compose_question(
        self,
        session_id: uuid.UUID,
        completeness_result: Any,
        trace_id: str,
    ) -> dict[str, Any]:
        outcome = await compose_question(
            completeness_result=completeness_result,
            runtime=AgentRuntime(),
            run_spec=RunSpec(
                run_id=uuid.uuid4(),
                session_id=session_id,
                state_version=completeness_result.input_state_version,
                stage="intake_question",
                agent_spec_version="question-composer-agent.v1",
                prompt_version="question_composer_v1.jinja2",
                deadline_at=_deadline(10),
                total_attempt_budget=1,
                idempotency_key=f"{trace_id}:question",
                trace_id=trace_id,
            ),
        )
        if outcome.status is not QuestionCompositionStatus.SUCCEEDED or outcome.result is None:
            code = str(outcome.failure_code or "QUESTION_COMPOSER_FAILED")
            raise AgentTriggerFailedError(
                detail=f"session_id={session_id} question composition failed code={code}",
                agent_error_code=code,
                retryable=False,
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
            return response

    async def _mark_claim_failed(self, claim_id: uuid.UUID, error_code: str) -> None:
        if self._db.in_transaction():
            await self._db.rollback()
        async with self._db.begin():
            claim = await self._db.get(IntakeCommandClaim, claim_id, with_for_update=True)
            if claim is not None and claim.status != "completed":
                claim.status = "failed"
                claim.error_code = error_code[:64]
                claim.updated_at = func.now()

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
    db, claim, _, runner = loaded
    try:
        completed = await _completed_graph_update(runner, claim)
        if completed is not None:
            return completed
        await _save_intermediate_step(claim.id, "triage_precheck")
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
                    "input_state_version": domain_state.state_version,
                },
            },
            step="build_intake_context",
        )
        return {"route": NODE_INTAKE_SUBGRAPH_V1, "domain_state_version": domain_state.state_version, "last_error": None}
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

        repository = PostgresDomainRepository(get_session_factory())
        domain_state = await repository.get_state(claim.session_id)
        run_id = _stable_intake_extraction_run_id(claim)
        intake_result = await execute_intake_extraction(
            runtime=AgentRuntime(),
            run_spec=RunSpec(
                run_id=run_id,
                session_id=claim.session_id,
                state_version=claim.input_state_version,
                stage="inquiry",
                agent_spec_version=INTAKE_AGENT_VERSION,
                prompt_version=INTAKE_PROMPT_VERSION,
                deadline_at=_deadline(30),
                total_attempt_budget=1,
                idempotency_key=f"{claim.idempotency_key}:intake",
                trace_id=_node_trace_id(state),
            ),
            input_payload=_build_intake_input(domain_state, patient_message),
        )
        if intake_result.status is not IntakeExecutionStatus.SUCCEEDED or intake_result.output is None:
            code = str(intake_result.failure_code or "INTAKE_FAILED")
            await runner._mark_claim_failed(claim.id, code)  # noqa: SLF001
            return _sanitized_graph_error(state, code, "intake extraction failed")
        _INTAKE_OUTPUT_CACHE[claim.id] = intake_result.output
        await _save_intermediate(
            claim.id,
            {"extraction": _extraction_metadata(run_id, intake_result.output, claim.input_state_version)},
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
        except RepositoryError as exc:
            await runner._mark_claim_failed(claim.id, exc.code.value)  # noqa: SLF001
            raise
        await _save_intermediate(
            claim.id,
            {"verified": {"delta_id": str(computation.delta.delta_id), "input_state_version": claim.input_state_version}},
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
        update, _ = await runner._execute_after_claim(  # noqa: SLF001
            claim=claim,
            patient_message=patient_message,
            trace_id=state.get("run_id") or command_id,
            state=state,
        )
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
    delta = _intake_output_to_delta(
        run_id=claim.run_id,
        session_id=claim.session_id,
        expected_state_version=claim.input_state_version,
        source_message_id=patient_message.id,
        state=domain_state,
        observations=output.observations,
        safety_delta=output.patient_safety_delta,
    )
    context = _verification_context(
        delta=delta,
        state=domain_state,
        trace_id=trace_id,
        idempotency_key=claim.idempotency_key,
    )
    next_state = reduce_domain_state(domain_state, delta, context)
    new_fact_count = len(delta.observations) + (1 if delta.safety_profile is not None else 0)
    triage_result = evaluate_triage_policy(
        TriagePolicyInput(
            input_state_version=next_state.state_version,
            red_flag_candidates=output.red_flag_candidates,
        )
    )
    if runner is None:
        factory = get_session_factory()
        async with factory() as db:
            progress = await LangGraphIntakeMessageRunner(db)._next_progress(  # noqa: SLF001
                claim.session_id,
                new_fact_count=new_fact_count,
            )
    else:
        progress = await runner._next_progress(claim.session_id, new_fact_count=new_fact_count)  # noqa: SLF001
    completeness_result = evaluate_completeness_policy(
        CompletenessPolicyInput(
            input_state_version=next_state.state_version,
            domain_snapshot=_completeness_snapshot(next_state),
            triage_gate=triage_result.gate_result,
            progress=progress,
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
        triage_result=triage_result,
        progress=progress,
        completeness_result=completeness_result,
        triage_gate=to_gate_result_schema(triage_result),
        completeness_gate=completeness_to_gate_result_schema(completeness_result),
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
    run_id = _stable_intake_extraction_run_id(claim)
    intake_result = await execute_intake_extraction(
        runtime=AgentRuntime(),
        run_spec=RunSpec(
            run_id=run_id,
            session_id=claim.session_id,
            state_version=claim.input_state_version,
            stage="inquiry",
            agent_spec_version=INTAKE_AGENT_VERSION,
            prompt_version=INTAKE_PROMPT_VERSION,
            deadline_at=_deadline(30),
            total_attempt_budget=1,
            idempotency_key=f"{claim.idempotency_key}:intake",
            trace_id=trace_id,
        ),
        input_payload=_build_intake_input(domain_state, patient_message),
    )
    if intake_result.status is not IntakeExecutionStatus.SUCCEEDED or intake_result.output is None:
        raise KeyError("extraction_output")
    _INTAKE_OUTPUT_CACHE[claim.id] = intake_result.output
    await _save_intermediate(
        claim.id,
        {"extraction": _extraction_metadata(run_id, intake_result.output, claim.input_state_version)},
        step="extract_intake",
    )
    return intake_result.output


def _stable_intake_extraction_run_id(claim: IntakeCommandClaim) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:intake-extraction:{claim.run_id}:{claim.idempotency_key}")


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
        disposition = computation.completeness_result.disposition
        if _route_for_disposition(disposition) != expected_route:
            return _sanitized_graph_error(state, "INTAKE_ROUTE_MISMATCH", "intake route does not match recomputed gate")

        question_message_id: uuid.UUID | None = None
        question_spec: ConsultMessageSpec | None = None
        agent_item: AgentMessageItem | None = None
        progress = computation.progress
        if disposition in {CompletenessDisposition.INCOMPLETE, CompletenessDisposition.CONFLICT}:
            question = await runner._compose_question(claim.session_id, computation.completeness_result, _node_trace_id(state))  # noqa: SLF001
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


def _command_key(trace_id: str) -> str:
    return _stable_ref("command", trace_id)[:128]


def _payload_digest(body: MessageCreateRequest) -> str:
    payload = body.model_dump(mode="json")
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


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
    )


def _intake_output_to_delta(
    *,
    run_id: uuid.UUID,
    session_id: uuid.UUID,
    expected_state_version: int,
    source_message_id: uuid.UUID,
    state: DomainState,
    observations: tuple[Any, ...],
    safety_delta: PatientSafetyDelta,
) -> DomainDelta:
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
        for index, item in enumerate(observations)
    )
    safety_profile = _merge_safety_profile(state.safety_profile, session_id, safety_delta)
    if not observation_schemas and safety_profile is None:
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
        safety_profile=safety_profile,
        artifact_revisions=artifact_revisions,
    )


def _observation_status(operation: ObservationOperation) -> ObservationStatus:
    if operation is ObservationOperation.ADD:
        return ObservationStatus.ACTIVE
    if operation is ObservationOperation.CORRECT:
        return ObservationStatus.CORRECTED
    return ObservationStatus.RETRACTED


def _merge_safety_profile(
    current: SafetyProfileSchema | None,
    session_id: uuid.UUID,
    delta: PatientSafetyDelta,
) -> SafetyProfileSchema | None:
    if not delta.has_candidate():
        return None
    values = (
        current.model_dump(mode="python")
        if current is not None
        else {"session_id": session_id}
    )
    _merge_list_safety(values, "allergy_collection_status", "allergens", delta.allergy.status, delta.allergy.values)
    _merge_scalar_safety(
        values,
        "pregnancy_collection_status",
        "pregnancy_value",
        delta.pregnancy.status,
        delta.pregnancy.value.value if delta.pregnancy.value is not None else None,
    )
    _merge_scalar_safety(
        values,
        "lactation_collection_status",
        "lactation_value",
        delta.lactation.status,
        delta.lactation.value.value if delta.lactation.value is not None else None,
    )
    _merge_list_safety(
        values,
        "medications_collection_status",
        "medications",
        delta.medications.status,
        delta.medications.values,
    )
    _merge_list_safety(
        values,
        "major_conditions_collection_status",
        "major_conditions",
        delta.major_conditions.status,
        delta.major_conditions.values,
    )
    _merge_list_safety(
        values,
        "contraindications_collection_status",
        "contraindications",
        delta.contraindications.status,
        delta.contraindications.values,
    )
    return SafetyProfileSchema.model_validate(values)


def _merge_list_safety(
    values: dict[str, Any],
    status_key: str,
    value_key: str,
    status: CollectionStatus,
    raw_values: tuple[str, ...] | None,
) -> None:
    if status is CollectionStatus.UNKNOWN:
        return
    values[status_key] = status
    values[value_key] = list(raw_values) if status is CollectionStatus.COLLECTED and raw_values else None


def _merge_scalar_safety(
    values: dict[str, Any],
    status_key: str,
    value_key: str,
    status: CollectionStatus,
    raw_value: str | None,
) -> None:
    if status is CollectionStatus.UNKNOWN:
        return
    values[status_key] = status
    values[value_key] = raw_value if status is CollectionStatus.COLLECTED else None


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
    return VerificationContext(
        agent_spec=agent_spec,
        run_spec=run_spec,
        artifact=artifact,
        state=state,
        allowed_source_message_ids=frozenset(delta.source_message_ids),
        allowed_stages=frozenset({"intake_reduce"}),
        satisfied_prerequisites=frozenset({"message_persisted"}),
    ).model_copy(update={"artifact": artifact}) if report.passed else _raise_verification_failed(report.failure_code)


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
    output_state_version: int,
) -> dict[str, object]:
    if disposition is CompletenessDisposition.READY or disposition in {CompletenessDisposition.INCOMPLETE, CompletenessDisposition.CONFLICT}:
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


def _graph_steps(disposition: CompletenessDisposition) -> tuple[GraphStepSpec, ...]:
    return tuple(
        GraphStepSpec(step_name=name, status="completed", metadata={})
        for name in (
            "persist_message",
            "triage_precheck",
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
    artifact_refs: list[ArtifactRef] = [
        {"kind": "message", "artifact_id": str(patient_message_id), "revision": 1}
    ]
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
    if isinstance(raw, (int, float)):
        raw = str(raw)
    if not isinstance(raw, str):
        return None
    code = _CODE_RE.sub("_", raw.strip().lower()).strip("_")
    return code[:64] or None
