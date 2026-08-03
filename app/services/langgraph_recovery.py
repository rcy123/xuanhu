"""Product recovery for persisted LangGraph sessions.

Recovery treats PostgreSQL Domain State as authority and the LangGraph
checkpoint as a control-plane cursor only.  Clinical payloads are never read
from, copied into, or logged from the checkpoint.  Public request details are
persisted in the existing durable command-claim ledger; Graph State contains
only the claim reference.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)
from pydantic import (
    ValidationError as PydanticValidationError,
)
from sqlalchemy import func, select
from sqlalchemy import null as sql_null
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.checkpoint import postgres_checkpointer
from app.agent_runtime.commands import NODE_RECOVERY_PLACEHOLDER, XuanhuCommand
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION, make_run_config
from app.agent_runtime.graph import build_main_graph
from app.agent_runtime.lifecycle import SharedLangGraphRuntime
from app.agent_runtime.reducer import DomainDelta, DomainState
from app.agent_runtime.repository import (
    ArtifactPayloadSpec,
    AuditEventSpec,
    GraphStepSpec,
    PostgresDomainRepository,
    RepositoryError,
    RepositoryErrorCode,
)
from app.agent_runtime.runner import GraphRunner
from app.agent_runtime.state import XuanhuGraphState, default_state
from app.core.config import get_settings
from app.core.exceptions import (
    IdempotencyConflictError,
    ModelGatewayUnavailableError,
    RecoveryNotNeededError,
    SessionBusyError,
    SessionNotFoundError,
    StateRecoveryRequiredError,
    ValidationError,
)
from app.db.session import get_session_factory
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import (
    ArtifactRevision,
    ArtifactRevisionPayload,
    DomainCommandCommit,
    GraphRun,
    IntakeCommandClaim,
)
from app.schemas.domain import ArtifactRevisionSchema, ArtifactStatus
from app.schemas.recovery import RecoveryRequest, RecoveryResponse
from app.services.langgraph_review import (
    DOCTOR_REVIEW_ARTIFACT_TYPE,
    REVIEW_SUBMISSION_ARTIFACT_TYPE,
    REVIEWED_FORMULA_ATTEMPT_ARTIFACT_TYPE,
    SAFETY_ARTIFACT_TYPE,
    SAFETY_RECHECK_ATTEMPT_ARTIFACT_TYPE,
    _artifact_revision,
    _load_formula_authority,
    _node_trace_id,
    _payload_spec,
    _verification_context,
)

logger = logging.getLogger("xuanhu.langgraph_recovery")

RECOVERY_CONTROL_ARTIFACT_TYPE = "recovery_control"
RECOVERY_CONTROL_SCHEMA_VERSION = "langgraph-recovery-control.v1"
RECOVERY_POLICY_VERSION = "langgraph-recovery.product.v1"
RECOVERY_COMMAND_SCHEMA_VERSION = "langgraph-recovery-command.v1"

_RECOVERABLE_STATUSES = frozenset({"blocked"})
_RECOVERABLE_RECOVERY_STATUSES = frozenset({"manual_required", "recovering"})
_LANGGRAPH_ROLLBACK_TARGETS = frozenset({"inquiry", "safety", "record"})
_STAGE_ORDER = {"inquiry": 0, "syndrome": 1, "safety": 2, "review": 3, "record": 4}
_DOWNSTREAM_FROM_SAFETY = frozenset(
    {
        SAFETY_ARTIFACT_TYPE,
        REVIEW_SUBMISSION_ARTIFACT_TYPE,
        DOCTOR_REVIEW_ARTIFACT_TYPE,
        "medical_record",
        REVIEWED_FORMULA_ATTEMPT_ARTIFACT_TYPE,
        SAFETY_RECHECK_ATTEMPT_ARTIFACT_TYPE,
    }
)

_SafeRef = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:+-]*$",
    ),
]
_SafeVersion = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:+-]*$",
    ),
]
_NonNegativeCounter = Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]
_PositiveRevision = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]


def _canonical_uuid_ref(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("UUID reference must be a string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("UUID reference is invalid") from None
    if str(parsed) != value.lower():
        raise ValueError("UUID reference must use canonical form")
    return value


class _CheckpointControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _CheckpointGateRef(_CheckpointControlModel):
    gate_name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=96,
            pattern=r"^[a-z][a-z0-9_.:-]*$",
        ),
    ]
    decision: Literal["passed", "failed", "blocked"]
    policy_version: _SafeVersion


class _CheckpointArtifactRef(_CheckpointControlModel):
    kind: Annotated[
        str,
        Field(
            min_length=1,
            max_length=96,
            pattern=r"^[a-z][a-z0-9_.:-]*$",
        ),
    ]
    artifact_id: str
    revision: _PositiveRevision

    @field_validator("artifact_id", mode="before")
    @classmethod
    def artifact_id_must_be_uuid(cls, value: object) -> str:
        return _canonical_uuid_ref(value)


class _CheckpointInterruptRef(_CheckpointControlModel):
    kind: Literal["doctor_review"]
    interrupt_id: str
    resume_token_ref: Literal["review_submission_ref"]

    @field_validator("interrupt_id", mode="before")
    @classmethod
    def interrupt_id_must_reference_revision(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("interrupt reference must be a string")
        artifact_id, separator, revision_text = value.rpartition(":")
        if not separator or not revision_text.isdecimal():
            raise ValueError("interrupt reference is invalid")
        _canonical_uuid_ref(artifact_id)
        revision = int(revision_text)
        if not 1 <= revision <= 2_147_483_647:
            raise ValueError("interrupt revision is invalid")
        return value


class _CheckpointBudget(_CheckpointControlModel):
    remaining_steps: _NonNegativeCounter
    remaining_tokens: _NonNegativeCounter
    deadline_ref: Annotated[
        str,
        Field(
            max_length=128,
            pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._:+-]*)?$",
        ),
    ]


class _CheckpointLastError(_CheckpointControlModel):
    code: Annotated[
        str,
        Field(
            min_length=1,
            max_length=96,
            pattern=r"^[A-Z][A-Z0-9_]*$",
        ),
    ]
    trace_id: _SafeRef
    detail: Annotated[str, Field(min_length=1, max_length=256)]


class _RecoveryCheckpointState(_CheckpointControlModel):
    """Complete reference-only runtime state accepted by Recovery preflight."""

    session_id: str
    domain_state_version: _NonNegativeCounter
    command: Literal["message", "advance", "review", "recover"]
    command_id: _SafeRef
    graph_version: Literal["v1"]
    run_id: str
    route: Literal[
        "",
        "command_router",
        "intake_subgraph_v1",
        "reasoning_subgraph_v1",
        "reasoning_placeholder",
        "review_placeholder",
        "recovery_placeholder",
        "blocked_terminal",
        "manual_terminal",
    ]
    intake_route: Literal["", "ready", "incomplete", "conflict", "manual"]
    reasoning_route: Literal[
        "",
        "syndrome_completed",
        "formula_completed",
        "needs_more_info",
        "manual_required",
    ]
    gate_results: Annotated[list[_CheckpointGateRef], Field(max_length=128)]
    artifact_refs: Annotated[list[_CheckpointArtifactRef], Field(max_length=128)]
    pending_interrupt: _CheckpointInterruptRef | None
    budget: _CheckpointBudget
    last_error: _CheckpointLastError | None

    @field_validator("session_id", "run_id", mode="before")
    @classmethod
    def ids_must_be_uuid_refs(cls, value: object) -> str:
        return _canonical_uuid_ref(value)


@dataclass(frozen=True, slots=True)
class _SessionMeta:
    session_id: uuid.UUID
    current_stage: str
    status: str
    pending_review: bool
    state_version: int
    recovery_status: str
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class RecoveryCheckpointProof:
    exists: bool
    domain_state_version: int | None
    command: str | None
    route: str | None
    has_pending_interrupt: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "exists": self.exists,
            "domain_state_version": self.domain_state_version,
            "command": self.command,
            "route": self.route,
            "has_pending_interrupt": self.has_pending_interrupt,
        }


@dataclass(frozen=True, slots=True)
class _ClaimedRecovery:
    run_id: uuid.UUID
    command_key: str
    target_stage: str


def _digest_json(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _command_key(idempotency_key: str) -> str:
    return f"recover:{_digest_text(idempotency_key)}"


def _request_marker(
    request: RecoveryRequest,
    *,
    doctor_id: str | None,
) -> dict[str, object]:
    return {
        "schema_version": RECOVERY_COMMAND_SCHEMA_VERSION,
        "action": request.action,
        "target_stage": request.target_stage,
        "reason_digest": _digest_text(request.reason or ""),
        "actor_type": "doctor" if doctor_id else "system",
        "actor_id": doctor_id,
    }


def _safe_trace(value: str) -> str:
    if 1 <= len(value) <= 64:
        return value
    return f"recovery:{_digest_text(value)[:48]}"


def _stable_control_artifact_id(session_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:{RECOVERY_CONTROL_ARTIFACT_TYPE}:{session_id}")


def _meta(row: ConsultSession) -> _SessionMeta:
    return _SessionMeta(
        session_id=row.id,
        current_stage=row.current_stage,
        status=row.status,
        pending_review=row.pending_review,
        state_version=row.state_version,
        recovery_status=row.recovery_status,
        blocked_reason=row.blocked_reason,
    )


async def _load_meta(session_id: uuid.UUID) -> _SessionMeta:
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(ConsultSession, session_id)
        if row is None:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} not found",
                retryable=False,
            )
        if row.agent_runtime != "langgraph":
            raise StateRecoveryRequiredError(
                detail=f"session_id={session_id} runtime mismatch",
                retryable=False,
            )
        return _meta(row)


def _checkpoint_proof(snapshot: Any, meta: _SessionMeta) -> RecoveryCheckpointProof:
    values = getattr(snapshot, "values", None)
    if not isinstance(values, dict) or not values:
        return RecoveryCheckpointProof(False, None, None, None, False)
    try:
        checkpoint = _RecoveryCheckpointState.model_validate(values)
    except PydanticValidationError:
        raise StateRecoveryRequiredError(
            detail=f"session_id={meta.session_id} checkpoint control schema is invalid",
            retryable=False,
        ) from None
    if checkpoint.session_id != str(meta.session_id):
        raise StateRecoveryRequiredError(
            detail=f"session_id={meta.session_id} checkpoint session mismatch",
            retryable=False,
        )
    if checkpoint.graph_version != DEFAULT_GRAPH_VERSION:
        raise StateRecoveryRequiredError(
            detail=f"session_id={meta.session_id} checkpoint graph version mismatch",
            retryable=False,
        )
    domain_version = checkpoint.domain_state_version
    if domain_version > meta.state_version:
        raise StateRecoveryRequiredError(
            detail=f"session_id={meta.session_id} checkpoint domain version is invalid",
            retryable=False,
        )
    return RecoveryCheckpointProof(
        True,
        domain_version,
        checkpoint.command,
        checkpoint.route,
        checkpoint.pending_interrupt is not None,
    )


def _source_stage(meta: _SessionMeta, proof: RecoveryCheckpointProof) -> str:
    if meta.current_stage in _STAGE_ORDER:
        return meta.current_stage
    reason = meta.blocked_reason or ""
    if reason.startswith("triage_hold:") or reason == "intake_stagnated_manual_required":
        return "inquiry"
    if reason == "reasoning_manual_required":
        return "syndrome"
    if reason == "safety_rule_blocked":
        return "safety"
    if proof.route == "intake_subgraph_v1":
        return "inquiry"
    if proof.route == "reasoning_subgraph_v1":
        return "syndrome"
    if proof.route == NODE_RECOVERY_PLACEHOLDER:
        raise StateRecoveryRequiredError(
            detail=f"session_id={meta.session_id} recovery checkpoint has no recoverable predecessor",
            retryable=False,
        )
    raise StateRecoveryRequiredError(
        detail=f"session_id={meta.session_id} blocked stage cannot be resolved safely",
        retryable=False,
    )


def _resolve_target(
    meta: _SessionMeta,
    request: RecoveryRequest,
    proof: RecoveryCheckpointProof,
) -> tuple[str, str]:
    if request.action != "rollback_to_stage" and request.target_stage is not None:
        raise ValidationError(
            message="仅 rollback_to_stage 可提供 target_stage",
            detail=f"session_id={meta.session_id} unexpected recovery target",
            retryable=False,
        )
    if request.action != "terminate" and not proof.exists:
        raise StateRecoveryRequiredError(
            detail=f"session_id={meta.session_id} LangGraph checkpoint is missing",
            retryable=False,
        )
    if request.action == "terminate":
        source = meta.current_stage if meta.current_stage in _STAGE_ORDER else "inquiry"
        if proof.exists:
            try:
                source = _source_stage(meta, proof)
            except StateRecoveryRequiredError:
                # Termination does not restore or consume clinical authority;
                # an unknown predecessor is recorded conservatively.
                source = "inquiry"
        return source, "blocked"
    if (meta.blocked_reason or "").startswith("triage_hold:"):
        raise StateRecoveryRequiredError(
            message="红旗分诊阻断不能通过运行时恢复解除",
            detail=f"session_id={meta.session_id} triage hold requires explicit clinical disposition",
            retryable=False,
        )
    if proof.has_pending_interrupt or meta.pending_review or meta.current_stage == "review":
        raise StateRecoveryRequiredError(
            message="待医师复核会话应通过 review 引用恢复",
            detail=f"session_id={meta.session_id} pending review checkpoint must use /review",
            retryable=False,
        )

    source = _source_stage(meta, proof)
    if request.action == "rollback_to_stage":
        target = request.target_stage
        if target is None:
            raise ValidationError(
                message="action=rollback_to_stage 时必须提供 target_stage",
                detail=f"session_id={meta.session_id} rollback target is missing",
                retryable=False,
            )
        if target not in _LANGGRAPH_ROLLBACK_TARGETS:
            raise StateRecoveryRequiredError(
                message="LangGraph 不支持该恢复目标阶段",
                detail=f"session_id={meta.session_id} unsupported LangGraph rollback target={target}",
                retryable=False,
            )
        if _STAGE_ORDER[target] > _STAGE_ORDER[source]:
            raise ValidationError(
                message="rollback_to_stage 不能前进到更晚阶段",
                detail=f"session_id={meta.session_id} source={source} target={target}",
                retryable=False,
            )
        return source, target

    # A syndrome-stage retry cannot reuse an old completeness gate at a new
    # Domain version.  Conservatively return to inquiry so the next message
    # produces a fresh, version-bound completeness authority.
    target = "inquiry" if source == "syndrome" else source
    if target not in _LANGGRAPH_ROLLBACK_TARGETS:
        raise StateRecoveryRequiredError(
            detail=f"session_id={meta.session_id} source stage={source} cannot be resumed automatically",
            retryable=False,
        )
    return source, target


def _require_recoverable(meta: _SessionMeta) -> None:
    if meta.status not in _RECOVERABLE_STATUSES and meta.recovery_status not in _RECOVERABLE_RECOVERY_STATUSES:
        raise RecoveryNotNeededError(
            detail=(f"session_id={meta.session_id} status={meta.status} recovery_status={meta.recovery_status}"),
            retryable=False,
        )
    if meta.status == "terminated":
        raise RecoveryNotNeededError(
            detail=f"session_id={meta.session_id} is terminated",
            retryable=False,
        )


async def _claim_recovery(
    *,
    meta: _SessionMeta,
    request: RecoveryRequest,
    doctor_id: str | None,
    trace_id: str,
    command_key: str,
    request_digest: str,
    proof: RecoveryCheckpointProof,
) -> _ClaimedRecovery | RecoveryResponse:
    factory = get_session_factory()
    async with factory() as db, db.begin():
        row = await db.get(ConsultSession, meta.session_id, with_for_update=True)
        if row is None:
            raise SessionNotFoundError(
                detail=f"session_id={meta.session_id} not found",
                retryable=False,
            )
        if row.agent_runtime != "langgraph":
            raise StateRecoveryRequiredError(
                detail=f"session_id={meta.session_id} runtime mismatch",
                retryable=False,
            )
        existing = await db.scalar(
            select(IntakeCommandClaim)
            .where(
                IntakeCommandClaim.session_id == meta.session_id,
                IntakeCommandClaim.idempotency_key == command_key,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.payload_digest != request_digest:
                raise IdempotencyConflictError(
                    detail=f"session_id={meta.session_id} recovery payload digest mismatch",
                    retryable=False,
                )
            if existing.status == "completed" and existing.response_payload is not None:
                return RecoveryResponse.model_validate(existing.response_payload)
            committed = await db.scalar(
                select(DomainCommandCommit)
                .where(
                    DomainCommandCommit.session_id == meta.session_id,
                    DomainCommandCommit.graph_run_id == existing.run_id,
                    DomainCommandCommit.input_state_version == existing.input_state_version,
                )
                .limit(1)
            )
            committed_run = await db.get(GraphRun, existing.run_id)
            if committed is not None and committed_run is not None and committed_run.status == "completed":
                persisted_payload, _persisted_request = _parse_claim_payload(existing)
                _persisted_source, persisted_target = _resolved_target(persisted_payload)
                return _ClaimedRecovery(existing.run_id, command_key, persisted_target)

        locked_meta = _meta(row)
        if locked_meta != meta:
            raise StateRecoveryRequiredError(
                detail=f"session_id={meta.session_id} changed during recovery preflight",
                retryable=True,
            )
        _require_recoverable(locked_meta)
        source_stage, target_stage = _resolve_target(locked_meta, request, proof)

        other_running = await db.scalar(
            select(IntakeCommandClaim.id).where(
                IntakeCommandClaim.session_id == meta.session_id,
                IntakeCommandClaim.status == "running",
                IntakeCommandClaim.idempotency_key != command_key,
            )
        )
        if other_running is not None:
            raise SessionBusyError(
                detail=f"session_id={meta.session_id} already has an in-flight command",
                retryable=True,
            )

        run_id = (
            existing.run_id
            if existing is not None
            else uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:{meta.session_id}:{command_key}")
        )
        marker = _request_marker(request, doctor_id=doctor_id)
        intermediate: dict[str, object] = {
            "kind": RECOVERY_COMMAND_SCHEMA_VERSION,
            "request": marker,
            "source_stage": source_stage,
            "source_status": meta.status,
            "source_recovery_status": meta.recovery_status,
            "source_blocked_reason": meta.blocked_reason,
            "resolved_target_stage": target_stage,
            "checkpoint": proof.as_payload(),
            "trace_id": _safe_trace(trace_id),
        }
        if existing is None:
            db.add(
                IntakeCommandClaim(
                    id=uuid.uuid4(),
                    session_id=meta.session_id,
                    idempotency_key=command_key,
                    payload_digest=request_digest,
                    input_state_version=meta.state_version,
                    status="running",
                    run_id=run_id,
                    intermediate_payload=intermediate,
                )
            )
        else:
            if existing.input_state_version != meta.state_version:
                raise StateRecoveryRequiredError(
                    detail=f"session_id={meta.session_id} stale recovery command claim",
                    retryable=False,
                )
            existing.status = "running"
            existing.intermediate_payload = intermediate
            # JSONB(None) is persisted as the JSON literal ``null``.  The
            # object-only constraint requires a database NULL while the claim
            # is reset for a retry.
            existing.response_payload = cast(Any, sql_null())
            existing.output_state_version = None
            existing.error_code = None
            existing.updated_at = func.now()

        graph_run = await db.get(GraphRun, run_id)
        if graph_run is None:
            db.add(
                GraphRun(
                    id=run_id,
                    session_id=meta.session_id,
                    graph_version=DEFAULT_GRAPH_VERSION,
                    command_id=command_key,
                    input_state_version=meta.state_version,
                    status="running",
                )
            )
        elif (
            graph_run.session_id != meta.session_id
            or graph_run.graph_version != DEFAULT_GRAPH_VERSION
            or graph_run.input_state_version != meta.state_version
        ):
            raise StateRecoveryRequiredError(
                detail=f"session_id={meta.session_id} recovery graph run mismatch",
                retryable=False,
            )
        elif graph_run.status != "completed":
            graph_run.status = "running"
            graph_run.completed_at = None

        return _ClaimedRecovery(run_id, command_key, target_stage)


async def _mark_recovery_failed(
    session_id: uuid.UUID,
    command_key: str,
    run_id: uuid.UUID,
) -> None:
    factory = get_session_factory()
    async with factory() as db, db.begin():
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
            claim.error_code = "RECOVERY_GRAPH_FAILED"
            claim.updated_at = func.now()
        graph_run = await db.get(GraphRun, run_id, with_for_update=True)
        if graph_run is not None and graph_run.status != "completed":
            graph_run.status = "failed"
            graph_run.completed_at = func.now()


async def _load_recovery_response(
    session_id: uuid.UUID,
    command_key: str,
    request_digest: str,
) -> RecoveryResponse:
    factory = get_session_factory()
    async with factory() as db:
        claim = await db.scalar(
            select(IntakeCommandClaim).where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == command_key,
            )
        )
        if claim is None:
            raise StateRecoveryRequiredError(
                detail=f"session_id={session_id} recovery claim disappeared",
                retryable=False,
            )
        if claim.payload_digest != request_digest:
            raise IdempotencyConflictError(
                detail=f"session_id={session_id} recovery payload digest mismatch",
                retryable=False,
            )
        if claim.status == "completed" and claim.response_payload is not None:
            return RecoveryResponse.model_validate(claim.response_payload)
        if claim.status == "failed":
            raise StateRecoveryRequiredError(
                detail=f"session_id={session_id} LangGraph recovery failed",
                retryable=False,
            )
        raise SessionBusyError(
            detail=f"session_id={session_id} recovery command is still running",
            retryable=True,
        )


class LangGraphRecoveryService:
    """Validate the prior checkpoint, claim one command, and run Recovery."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def recover(
        self,
        session_id: str,
        request: RecoveryRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
        idempotency_key: str,
        shared_runtime: SharedLangGraphRuntime | None,
        allow_request_local_runtime: bool,
    ) -> RecoveryResponse:
        del self._db
        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} format is invalid",
                retryable=False,
            ) from None
        marker = _request_marker(request, doctor_id=doctor_id)
        digest = _digest_json(marker)
        command_key = _command_key(idempotency_key)
        meta = await _load_meta(sid)
        config = make_run_config(session_id, graph_version=DEFAULT_GRAPH_VERSION)

        if shared_runtime is not None:
            return await self._recover_with_graph(
                graph=shared_runtime.graph,
                runner=shared_runtime.runner(timeout_seconds=120),
                config=config,
                meta=meta,
                request=request,
                doctor_id=doctor_id,
                trace_id=trace_id,
                command_key=command_key,
                request_digest=digest,
            )
        if not allow_request_local_runtime:
            raise ModelGatewayUnavailableError(
                "shared LangGraph runtime is unavailable",
                retryable=True,
            )
        async with postgres_checkpointer(get_settings().database_url) as saver:
            graph = build_main_graph(checkpointer=saver)
            return await self._recover_with_graph(
                graph=graph,
                runner=GraphRunner(graph, timeout_seconds=120),
                config=config,
                meta=meta,
                request=request,
                doctor_id=doctor_id,
                trace_id=trace_id,
                command_key=command_key,
                request_digest=digest,
            )

    async def _recover_with_graph(
        self,
        *,
        graph: Any,
        runner: GraphRunner,
        config: dict[str, Any],
        meta: _SessionMeta,
        request: RecoveryRequest,
        doctor_id: str | None,
        trace_id: str,
        command_key: str,
        request_digest: str,
    ) -> RecoveryResponse:
        snapshot = await graph.aget_state(config)
        proof = _checkpoint_proof(snapshot, meta)
        claimed = await _claim_recovery(
            meta=meta,
            request=request,
            doctor_id=doctor_id,
            trace_id=trace_id,
            command_key=command_key,
            request_digest=request_digest,
            proof=proof,
        )
        if isinstance(claimed, RecoveryResponse):
            return claimed
        state = default_state(
            session_id=str(meta.session_id),
            command=XuanhuCommand.RECOVER.value,
            command_id=claimed.command_key,
            graph_version=DEFAULT_GRAPH_VERSION,
            run_id=str(claimed.run_id),
        )
        try:
            result = await runner.ainvoke(dict(state), config=config)
            if result.get("last_error") is not None:
                raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED)
        except Exception:
            await _mark_recovery_failed(meta.session_id, claimed.command_key, claimed.run_id)
            raise StateRecoveryRequiredError(
                detail=f"session_id={meta.session_id} LangGraph recovery execution failed",
                retryable=False,
            ) from None
        # P1-1: 恢复控制落库后,把最近一条 retryable 失败 intake 消息重放一遍,
        # 让恢复不只清状态、还能推进对话(重放失败不影响本次恢复结果)。
        await _replay_failed_intake_message(meta.session_id, trace_id=trace_id)
        return await _load_recovery_response(meta.session_id, command_key, request_digest)


def _parse_claim_payload(claim: IntakeCommandClaim) -> tuple[dict[str, object], dict[str, object]]:
    payload = claim.intermediate_payload
    expected_payload_keys = {
        "kind",
        "request",
        "source_stage",
        "source_status",
        "source_recovery_status",
        "source_blocked_reason",
        "resolved_target_stage",
        "checkpoint",
        "trace_id",
    }
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != RECOVERY_COMMAND_SCHEMA_VERSION
        or set(payload) != expected_payload_keys
        or not claim.idempotency_key.startswith("recover:")
    ):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    request = payload.get("request")
    checkpoint = payload.get("checkpoint")
    if not isinstance(request, dict) or not isinstance(checkpoint, dict):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    expected_request_keys = {
        "schema_version",
        "action",
        "target_stage",
        "reason_digest",
        "actor_type",
        "actor_id",
    }
    action = request.get("action")
    target = request.get("target_stage")
    reason_digest = request.get("reason_digest")
    actor_type = request.get("actor_type")
    actor_id = request.get("actor_id")
    if (
        set(request) != expected_request_keys
        or request.get("schema_version") != RECOVERY_COMMAND_SCHEMA_VERSION
        or action
        not in {
            "resume_from_pg_snapshot",
            "retry_current_stage",
            "rollback_to_stage",
            "terminate",
        }
        or (action == "rollback_to_stage" and not isinstance(target, str))
        or (action != "rollback_to_stage" and target is not None)
        or not isinstance(reason_digest, str)
        or len(reason_digest) != 64
        or any(character not in "0123456789abcdef" for character in reason_digest)
        or actor_type not in {"doctor", "system"}
        or (actor_type == "doctor" and (not isinstance(actor_id, str) or not actor_id))
        or (actor_type == "system" and actor_id is not None)
        or not isinstance(payload.get("trace_id"), str)
    ):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    if _digest_json(cast(dict[str, object], request)) != claim.payload_digest:
        raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
    return cast(dict[str, object], payload), cast(dict[str, object], request)


async def _load_claim_and_session(
    session_id: uuid.UUID,
    command_key: str,
    run_id: uuid.UUID,
) -> tuple[IntakeCommandClaim, ConsultSession, dict[str, object], dict[str, object]]:
    factory = get_session_factory()
    async with factory() as db:
        claim = await db.scalar(
            select(IntakeCommandClaim).where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == command_key,
            )
        )
        session = await db.get(ConsultSession, session_id)
        if claim is None or session is None or claim.run_id != run_id:
            raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
        if session.agent_runtime != "langgraph":
            raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
        payload, request = _parse_claim_payload(claim)
        return claim, session, payload, request


def _proof_from_payload(payload: dict[str, object]) -> RecoveryCheckpointProof:
    raw = payload.get("checkpoint")
    if not isinstance(raw, dict):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    exists = raw.get("exists")
    domain_version = raw.get("domain_state_version")
    command = raw.get("command")
    route = raw.get("route")
    pending = raw.get("has_pending_interrupt")
    if (
        not isinstance(exists, bool)
        or (domain_version is not None and not isinstance(domain_version, int))
        or (command is not None and not isinstance(command, str))
        or (route is not None and not isinstance(route, str))
        or not isinstance(pending, bool)
    ):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    return RecoveryCheckpointProof(exists, domain_version, command, route, pending)


def _resolved_target(payload: dict[str, object]) -> tuple[str, str]:
    source = payload.get("source_stage")
    target = payload.get("resolved_target_stage")
    if (
        not isinstance(source, str)
        or not isinstance(target, str)
        or source not in _STAGE_ORDER
        or target not in _LANGGRAPH_ROLLBACK_TARGETS | {"blocked"}
    ):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    return source, target


async def _validate_target_authority(
    repository: PostgresDomainRepository,
    state: DomainState,
    target: str,
) -> None:
    if target in {"inquiry", "blocked"}:
        return
    if target == "safety":
        await _load_formula_authority(repository, state.session_id)
        return
    if target == "record":
        from app.services.langgraph_record import _load_doctor_review_authority

        await _load_doctor_review_authority(repository, state)
        return
    raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)


def _invalidation_ids(state: DomainState, target: str) -> tuple[uuid.UUID, ...]:
    current = [item for item in state.artifacts if item.status is ArtifactStatus.CURRENT]
    if target == "inquiry":
        selected = [item.artifact_id for item in current if item.artifact_type != RECOVERY_CONTROL_ARTIFACT_TYPE]
    elif target == "safety":
        selected = [item.artifact_id for item in current if item.artifact_type in _DOWNSTREAM_FROM_SAFETY]
    elif target == "record":
        selected = [item.artifact_id for item in current if item.artifact_type == "medical_record"]
    else:
        selected = []
    return tuple(dict.fromkeys(selected))


def _session_updates(
    *,
    action: str,
    source: str,
    target: str,
    state_version: int,
    proof: RecoveryCheckpointProof,
    preserve_advance: dict[str, Any] | None = None,
) -> dict[str, object]:
    terminated = action == "terminate"
    current_stage = "blocked" if terminated else target
    snapshot: dict[str, object] = {
        "agent_runtime": "langgraph",
        "current_stage": current_stage,
        "state_version": state_version,
        "pending_review": False,
        "recovery_status": "normal",
        "langgraph_recovery": {
            "version": RECOVERY_POLICY_VERSION,
            "action": action,
            "source_stage": source,
            "target_stage": current_stage,
            "checkpoint_domain_state_version": proof.domain_state_version,
            "checkpoint_had_interrupt": proof.has_pending_interrupt,
        },
    }
    # 保留 intake→syndrome 的 advance 出处：safety 目标恢复后仍需经过
    # review/reject 回到 syndrome 重新开方的链路。
    if preserve_advance is not None:
        snapshot["advance"] = preserve_advance
    return {
        "current_stage": current_stage,
        "status": "terminated" if terminated else "active",
        "pending_review": False,
        "recovery_status": "normal",
        "blocked_reason": "terminated_by_doctor" if terminated else None,
        "blocked_at": datetime.now(UTC).replace(tzinfo=None) if terminated else None,
        "state_snapshot": snapshot,
    }


def _response_payload(
    *,
    session_id: uuid.UUID,
    action: str,
    target: str,
    state_version: int,
    updated_at: datetime,
) -> dict[str, object]:
    terminated = action == "terminate"
    return {
        "session_id": str(session_id),
        "current_stage": "blocked" if terminated else target,
        "status": "terminated" if terminated else "active",
        "recovery_status": "normal",
        "action": action,
        "updated_at": updated_at.isoformat(),
        "state_version": state_version,
    }


async def _complete_claim(
    claim_id: uuid.UUID,
    response: dict[str, object],
    state_version: int,
) -> None:
    factory = get_session_factory()
    async with factory() as db, db.begin():
        claim = await db.get(IntakeCommandClaim, claim_id, with_for_update=True)
        if claim is None:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED)
        if claim.status == "completed":
            if claim.response_payload != response:
                raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
            return
        claim.status = "completed"
        claim.output_state_version = state_version
        claim.response_payload = response
        claim.error_code = None
        claim.updated_at = func.now()


async def _recover_committed_claim(
    claim: IntakeCommandClaim,
    session: ConsultSession,
    request: dict[str, object],
    target: str,
) -> dict[str, Any] | None:
    factory = get_session_factory()
    async with factory() as db:
        commit = await db.scalar(
            select(DomainCommandCommit).where(
                DomainCommandCommit.session_id == claim.session_id,
                DomainCommandCommit.graph_run_id == claim.run_id,
                DomainCommandCommit.input_state_version == claim.input_state_version,
            )
        )
        run = await db.get(GraphRun, claim.run_id)
        revision = await db.scalar(
            select(ArtifactRevision)
            .where(
                ArtifactRevision.session_id == claim.session_id,
                ArtifactRevision.artifact_type == RECOVERY_CONTROL_ARTIFACT_TYPE,
                ArtifactRevision.produced_by_run_id == claim.run_id,
            )
            .order_by(ArtifactRevision.revision.desc())
            .limit(1)
        )
        if commit is None or run is None or run.status != "completed" or revision is None:
            return None
        payload_row = await db.scalar(
            select(ArtifactRevisionPayload).where(ArtifactRevisionPayload.artifact_revision_id == revision.id)
        )
        if payload_row is None or payload_row.payload.get("command_id") != claim.idempotency_key:
            raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
        action = request.get("action")
        if not isinstance(action, str):
            raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
        updated_at = revision.created_at
        response = _response_payload(
            session_id=claim.session_id,
            action=action,
            target=target,
            state_version=commit.output_state_version,
            updated_at=updated_at,
        )
    await _complete_claim(claim.id, response, commit.output_state_version)
    return {
        "route": NODE_RECOVERY_PLACEHOLDER,
        "domain_state_version": commit.output_state_version,
        "pending_interrupt": None,
        "last_error": None,
    }


async def _replay_failed_intake_message(
    session_id: uuid.UUID,
    *,
    trace_id: str,
) -> str | None:
    """Best-effort replay of the latest failed retryable intake patient message.

    The recovery control commit already advanced the session state, so the
    replay creates a fresh durable intake claim against the existing patient
    message at the current state version and runs it through the same intake
    path.  On success the agent reply is persisted and the failed message
    becomes consumed; replay failures are logged and never roll back the
    recovery itself.
    """
    from app.services.langgraph_intake import (
        RETRYABLE_INTAKE_FAILURE_CODES,
        LangGraphIntakeMessageRunner,
    )

    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        if (
            session is None
            or session.status != "active"
            or session.current_stage != "inquiry"
        ):
            return None
        failed = await db.scalar(
            select(IntakeCommandClaim)
            .where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.status == "failed",
                IntakeCommandClaim.error_code.in_(tuple(RETRYABLE_INTAKE_FAILURE_CODES)),
            )
            .order_by(IntakeCommandClaim.updated_at.desc())
            .limit(1)
        )
        if (
            failed is None
            or failed.patient_message_id is None
            or failed.idempotency_key.startswith(("recover:", "replay:"))
        ):
            return None
        committed = await db.scalar(
            select(DomainCommandCommit.id).where(DomainCommandCommit.graph_run_id == failed.run_id)
        )
        if committed is not None:
            return None
        patient_message = await db.get(ConsultMessage, failed.patient_message_id)
        if patient_message is None:
            return None
        replay_key = f"replay:{failed.idempotency_key}"
        existing_replay_id = await db.scalar(
            select(IntakeCommandClaim.id).where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == replay_key,
            )
        )
        # Snapshot plain values before any rollback/commit; SQLAlchemy async
        # sessions cannot lazy-load expired ORM attributes outside a greenlet.
        state_version = session.state_version
        source_claim_id = failed.id
        source_error_code = failed.error_code
        message_id = patient_message.id
        payload_digest = failed.payload_digest
        if db.in_transaction():
            await db.rollback()
        async with db.begin():
            if existing_replay_id is not None:
                existing_replay = await db.get(IntakeCommandClaim, existing_replay_id, with_for_update=True)
                if existing_replay is None or existing_replay.status == "completed":
                    return None
                if existing_replay.status == "running":
                    return None
                existing_replay.status = "running"
                existing_replay.error_code = None
                existing_replay.response_payload = None
                existing_replay.output_state_version = None
                existing_replay.input_state_version = state_version
                # A fresh run id keeps extraction audit provenance unique across
                # replay attempts (stable run id + new trace id would conflict).
                existing_replay.run_id = uuid.uuid4()
                existing_replay.updated_at = func.now()
                replay_id = existing_replay.id
                replay_run_id = existing_replay.run_id
            else:
                replay_claim = IntakeCommandClaim(
                    id=uuid.uuid4(),
                    session_id=session_id,
                    idempotency_key=replay_key,
                    run_id=uuid.uuid4(),
                    input_state_version=state_version,
                    payload_digest=payload_digest,
                    status="running",
                    patient_message_id=message_id,
                    intermediate_payload={
                        "kind": "intake_replay",
                        "source_claim_id": str(source_claim_id),
                        "source_error_code": source_error_code,
                        "trace_id": trace_id,
                    },
                )
                db.add(replay_claim)
                await db.flush()
                replay_id = replay_claim.id
                replay_run_id = replay_claim.run_id
        # The begin() commit expired every loaded ORM attribute; re-fetch the
        # replay claim and patient message so _execute_after_claim sees loaded
        # instances (async sessions cannot lazy-load expired attributes).
        replay_claim = await db.get(IntakeCommandClaim, replay_id)
        patient_message = await db.get(ConsultMessage, message_id)
        if replay_claim is None or patient_message is None:
            return None
        state = default_state(
            session_id=str(session_id),
            command=XuanhuCommand.MESSAGE.value,
            command_id=replay_key,
            graph_version=DEFAULT_GRAPH_VERSION,
            run_id=str(replay_run_id),
        )
        try:
            runner = LangGraphIntakeMessageRunner(db)
            _, response = await runner._execute_after_claim(  # noqa: SLF001
                claim=replay_claim,
                patient_message=patient_message,
                trace_id=trace_id,
                state=state,
            )
        except Exception:
            logger.exception(
                "intake replay after recovery failed session_id=%s claim=%s",
                session_id,
                replay_id,
            )
            return None
        agent_message = response.agent_message
        if agent_message is not None:
            return agent_message.content
        return None


async def execute_recovery_command(state: XuanhuGraphState) -> dict[str, Any]:
    """Consume a durable recovery claim and commit one control revision."""

    try:
        session_id = uuid.UUID(state.get("session_id", ""))
        run_id = uuid.UUID(state.get("run_id", ""))
    except (TypeError, ValueError):
        return {
            "route": NODE_RECOVERY_PLACEHOLDER,
            "last_error": {
                "code": "RECOVERY_COMMAND_REF_INVALID",
                "trace_id": state.get("run_id", ""),
                "detail": "recovery command refs are invalid",
            },
        }
    command_key = state.get("command_id", "")
    if not command_key:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    claim, session, payload, request = await _load_claim_and_session(session_id, command_key, run_id)
    source, target = _resolved_target(payload)
    proof = _proof_from_payload(payload)
    action = request.get("action")
    if not isinstance(action, str):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    if claim.status == "completed" and claim.response_payload is not None:
        RecoveryResponse.model_validate(claim.response_payload)
        return {
            "route": NODE_RECOVERY_PLACEHOLDER,
            "domain_state_version": claim.output_state_version or session.state_version,
            "pending_interrupt": None,
            "last_error": None,
            "artifact_refs": [],
            "gate_results": [],
        }
    if session.state_version != claim.input_state_version:
        recovered = await _recover_committed_claim(claim, session, request, target)
        if recovered is not None:
            return recovered
        raise RepositoryError(RepositoryErrorCode.STATE_VERSION_CONFLICT)
    if (
        session.status != payload.get("source_status")
        or session.recovery_status != payload.get("source_recovery_status")
        or session.blocked_reason != payload.get("source_blocked_reason")
    ):
        raise RepositoryError(RepositoryErrorCode.STATE_VERSION_CONFLICT)

    repository = PostgresDomainRepository(get_session_factory())
    domain_state = await repository.get_state(session_id)
    if domain_state.state_version != claim.input_state_version:
        raise RepositoryError(RepositoryErrorCode.STATE_VERSION_CONFLICT)
    await _validate_target_authority(repository, domain_state, target)

    artifact_id = _stable_control_artifact_id(session_id)
    latest = await repository.get_artifact_payload(
        session_id,
        artifact_type=RECOVERY_CONTROL_ARTIFACT_TYPE,
        artifact_id=artifact_id,
        status=None,
    )
    artifact: ArtifactRevisionSchema = _artifact_revision(
        session_id=session_id,
        artifact_id=artifact_id,
        artifact_type=RECOVERY_CONTROL_ARTIFACT_TYPE,
        state_version=domain_state.state_version,
        run_id=run_id,
        latest=latest,
    )
    control_payload: dict[str, object] = {
        "kind": RECOVERY_CONTROL_ARTIFACT_TYPE,
        "command_id": command_key,
        "action": action,
        "source_stage": source,
        "target_stage": "blocked" if action == "terminate" else target,
        "reason_digest": request.get("reason_digest"),
        "checkpoint": proof.as_payload(),
    }
    payload_spec: ArtifactPayloadSpec = _payload_spec(
        artifact,
        schema_version=RECOVERY_CONTROL_SCHEMA_VERSION,
        payload=control_payload,
    )
    delta = DomainDelta(
        delta_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:delta:recovery:{run_id}"),
        run_id=run_id,
        session_id=session_id,
        expected_state_version=domain_state.state_version,
        artifact_revisions=(artifact,),
        invalidate_artifact_ids=_invalidation_ids(domain_state, target if action != "terminate" else "blocked"),
    )
    trace_id_raw = payload.get("trace_id")
    trace_id = trace_id_raw if isinstance(trace_id_raw, str) else _node_trace_id(state)
    output_version = domain_state.state_version + 1
    event_type = "session.terminated" if action == "terminate" else "session.recovered"
    old_advance = (session.state_snapshot or {}).get("advance")
    preserve_advance = old_advance if isinstance(old_advance, dict) else None
    commit = await repository.commit(
        delta,
        _verification_context(
            delta,
            domain_state,
            stage="recovery",
            idempotency_key=f"{command_key}:apply",
            trace_id=trace_id,
            policy_version=RECOVERY_POLICY_VERSION,
        ),
        graph_version=DEFAULT_GRAPH_VERSION,
        graph_steps=(
            GraphStepSpec(step_name="verify_checkpoint_refs", status="completed", metadata={}),
            GraphStepSpec(step_name="apply_recovery_control", status="completed", metadata={}),
        ),
        artifact_payloads=(payload_spec,),
        audit_events=(
            AuditEventSpec(
                event_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:audit:{event_type}:{run_id}"),
                session_id=session_id,
                event_type=event_type,
                actor_type=cast(str, request.get("actor_type")),
                actor_id=cast(str | None, request.get("actor_id")),
                payload={
                    "action": action,
                    "source_stage": source,
                    "target_stage": "blocked" if action == "terminate" else target,
                    "reason_digest": request.get("reason_digest"),
                    "checkpoint": proof.as_payload(),
                    "input_state_version": domain_state.state_version,
                    "output_state_version": output_version,
                },
                trace_id=trace_id,
            ),
        ),
        session_updates=_session_updates(
            action=action,
            source=source,
            target=target,
            state_version=output_version,
            proof=proof,
            preserve_advance=preserve_advance,
        ),
        outbox_event_type="session.terminated.v1" if action == "terminate" else "session.recovered.v1",
        outbox_payload={
            "session_id": str(session_id),
            "action": action,
            "source_stage": source,
            "target_stage": "blocked" if action == "terminate" else target,
            "input_state_version": domain_state.state_version,
            "output_state_version": output_version,
        },
    )
    factory = get_session_factory()
    async with factory() as db:
        updated = await db.get(ConsultSession, session_id)
        if updated is None:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED)
        response_payload = _response_payload(
            session_id=session_id,
            action=action,
            target=target,
            state_version=commit.output_state_version,
            updated_at=updated.updated_at,
        )
    await _complete_claim(claim.id, response_payload, commit.output_state_version)
    return {
        "route": NODE_RECOVERY_PLACEHOLDER,
        "domain_state_version": commit.output_state_version,
        "artifact_refs": [
            {
                "kind": RECOVERY_CONTROL_ARTIFACT_TYPE,
                "artifact_id": str(artifact.artifact_id),
                "revision": artifact.revision,
            }
        ],
        "gate_results": [],
        "pending_interrupt": None,
        "last_error": None,
    }


__all__ = [
    "LangGraphRecoveryService",
    "RECOVERY_CONTROL_ARTIFACT_TYPE",
    "RECOVERY_CONTROL_SCHEMA_VERSION",
    "RECOVERY_POLICY_VERSION",
    "RecoveryCheckpointProof",
    "execute_recovery_command",
]
