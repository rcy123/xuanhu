"""Deterministic product Record assembly for LangGraph sessions.

The node consumes only the current, persisted L5 authority closure and commits
the Domain artifact plus the legacy ``medical_records`` projection in one
transaction.  It never calls a model and never treats checkpoint/state_snapshot
content as a clinical source of truth.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select

from app.agent_runtime.commands import NODE_REVIEW_PLACEHOLDER
from app.agent_runtime.config import DEFAULT_GRAPH_VERSION
from app.agent_runtime.reducer import DomainDelta, DomainState
from app.agent_runtime.repository import (
    ArtifactPayloadRecord,
    ArtifactPayloadSpec,
    AuditEventSpec,
    GraphStepSpec,
    MedicalRecordSpec,
    PostgresDomainRepository,
    RepositoryError,
    RepositoryErrorCode,
    artifact_payload_digest,
)
from app.agent_runtime.state import ArtifactRef, XuanhuGraphState
from app.db.session import get_session_factory
from app.models.consult import ConsultSession
from app.models.domain import (
    ArtifactRevision,
    ArtifactRevisionPayload,
    DomainCommandCommit,
    GateResult,
    GraphRun,
    IntakeCommandClaim,
    OutboxEvent,
)
from app.models.review import DoctorReview, MedicalRecord
from app.schemas.agent import SafetyRuleResult
from app.schemas.domain import ArtifactRevisionSchema, ArtifactStatus, GateDecision, GateResultSchema
from app.services.langgraph_review import (
    DOCTOR_REVIEW_ARTIFACT_TYPE,
    DOCTOR_REVIEW_SCHEMA_VERSION,
    FormulaAuthority,
    _artifact_revision,
    _complete_advance_claim,
    _formula_ref,
    _load_formula_authority,
    _load_safety_authority,
    _node_trace_id,
    _payload_spec,
    _require_completed_producer,
    _verification_context,
)

MEDICAL_RECORD_ARTIFACT_TYPE = "medical_record"
MEDICAL_RECORD_SCHEMA_VERSION = "medical-record.product.v1"
RECORD_POLICY_VERSION = "record-consistency.product.v1"
RECORD_DISCLAIMER = "本记录由确定性规则根据已确认处方生成，须由具备资质的医师结合原始病历复核。"


def _stable_record_artifact_id(session_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:{MEDICAL_RECORD_ARTIFACT_TYPE}:{session_id}")


def _projection_record_id(artifact_id: uuid.UUID, revision: int) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:medical-record-projection:{artifact_id}:{revision}")


async def _load_record_session(session_id: uuid.UUID) -> ConsultSession:
    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(ConsultSession, session_id)
        if session is None:
            raise RepositoryError(RepositoryErrorCode.SESSION_NOT_FOUND)
        if (
            session.agent_runtime != "langgraph"
            or session.current_stage != "record"
            or session.status != "active"
            or session.pending_review
            or session.recovery_status != "normal"
        ):
            raise RepositoryError(RepositoryErrorCode.STATE_VERSION_CONFLICT)
        return session


def _current_doctor_review_ref(state: DomainState) -> ArtifactRevisionSchema:
    current = [
        item
        for item in state.artifacts
        if item.status is ArtifactStatus.CURRENT
        and item.artifact_type == DOCTOR_REVIEW_ARTIFACT_TYPE
    ]
    if len(current) != 1:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    return current[0]


def _payload_digest_matches(record: ArtifactPayloadRecord) -> bool:
    return (
        artifact_payload_digest(record.payload_schema_version, record.payload)
        == record.content_digest
    )


def _utc_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID) from None
    if parsed.tzinfo is None:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    return parsed.astimezone(UTC)


async def _load_doctor_review_authority(
    repository: PostgresDomainRepository,
    state: DomainState,
) -> tuple[
    ArtifactPayloadRecord,
    DoctorReview,
    FormulaAuthority,
    ArtifactPayloadRecord,
    SafetyRuleResult,
    uuid.UUID,
]:
    ref = _current_doctor_review_ref(state)
    record = await repository.get_artifact_payload(
        state.session_id,
        artifact_type=DOCTOR_REVIEW_ARTIFACT_TYPE,
        artifact_id=ref.artifact_id,
        revision=ref.revision,
        status="current",
    )
    if (
        record is None
        or record.payload_schema_version != DOCTOR_REVIEW_SCHEMA_VERSION
        or not _payload_digest_matches(record)
    ):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    await _require_completed_producer(record)
    payload = record.payload
    review_id_raw = payload.get("review_id")
    action = payload.get("action")
    expected_payload_fields = {
        "kind",
        "review_id",
        "action",
        "submission_ref",
        "safety_ref",
        "formula_ref",
        "reviewed_by",
        "reviewed_at",
        "feedback",
        "original_formula",
        "formula_override",
    }
    if (
        set(payload) != expected_payload_fields
        or payload.get("kind") != DOCTOR_REVIEW_ARTIFACT_TYPE
        or not isinstance(review_id_raw, str)
        or action not in {"confirm", "modify"}
        or not isinstance(payload.get("submission_ref"), dict)
        or not isinstance(payload.get("safety_ref"), dict)
        or not isinstance(payload.get("formula_ref"), dict)
        or not (
            payload.get("reviewed_by") is None
            or isinstance(payload.get("reviewed_by"), str)
        )
        or not (
            payload.get("feedback") is None
            or isinstance(payload.get("feedback"), str)
        )
        or not isinstance(payload.get("original_formula"), dict)
    ):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    try:
        review_id = uuid.UUID(review_id_raw)
    except ValueError:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID) from None

    formula = await _load_formula_authority(repository, state.session_id)
    safety_record, safety_result, safety_run_id = await _load_safety_authority(
        repository,
        state.session_id,
        formula,
        state.safety_profile,
    )
    if not safety_result.passed:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    expected_safety_ref = {
        "artifact_id": str(safety_record.artifact_id),
        "revision": safety_record.revision,
        "content_digest": safety_record.content_digest,
        "safety_rule_run_id": str(safety_run_id),
    }
    if payload.get("safety_ref") != expected_safety_ref:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    if payload.get("formula_ref") != _formula_ref(formula):
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    reviewed_at = _utc_datetime(payload.get("reviewed_at"))

    factory = get_session_factory()
    async with factory() as db:
        projection = await db.get(DoctorReview, review_id)
        projection_created_at = (
            projection.created_at.replace(tzinfo=UTC)
            if projection is not None and projection.created_at.tzinfo is None
            else projection.created_at.astimezone(UTC)
            if projection is not None
            else None
        )
        if (
            projection is None
            or projection.session_id != state.session_id
            or projection.agent_run_id is not None
            or projection.action != action
            or projection.safety_rule_run_id != safety_run_id
            or projection.reviewed_by != payload.get("reviewed_by")
            or projection_created_at != reviewed_at
            or projection.feedback != payload.get("feedback")
            or projection.original_formula != payload.get("original_formula")
            or projection.formula_override != payload.get("formula_override")
        ):
            raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
        formula_json = cast(dict[str, object], formula.formula.model_dump(mode="json"))
        if action == "modify":
            if (
                projection.formula_override != formula_json
                or projection.original_formula is None
            ):
                raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
        elif projection.formula_override is not None or projection.original_formula != formula_json:
            raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    return record, projection, formula, safety_record, safety_result, safety_run_id


def _render_record_text(
    *,
    formula: FormulaAuthority,
    action: str,
    safety_rule_version: str,
) -> str:
    composition = "、".join(
        f"{item.herb}{'' if item.dose is None else item.dose}{item.unit}"
        for item in formula.formula.composition
    )
    return "\n".join(
        (
            f"处方：{formula.formula.name or '未命名处方'}",
            f"组成：{composition}",
            f"安全审核：通过（{safety_rule_version}）",
            f"医师复核：{action}",
            f"免责声明：{RECORD_DISCLAIMER}",
        )
    )


def _doctor_review_output(review: DoctorReview) -> dict[str, object]:
    return {
        "review_id": str(review.id),
        "session_id": str(review.session_id),
        "agent_run_id": None,
        "safety_rule_run_id": str(review.safety_rule_run_id),
        "action": review.action,
        "reviewed_by": review.reviewed_by,
        "reviewed_at": review.created_at.isoformat(),
        "feedback": review.feedback,
        "original_formula": review.original_formula,
        "formula_override": review.formula_override,
    }


def _record_json(
    *,
    session_id: uuid.UUID,
    record_id: uuid.UUID,
    artifact: ArtifactRevisionSchema,
    review_record: ArtifactPayloadRecord,
    review: DoctorReview,
    formula: FormulaAuthority,
    safety_record: ArtifactPayloadRecord,
    safety_result: SafetyRuleResult,
    safety_run_id: uuid.UUID,
) -> dict[str, object]:
    result_dump = safety_result.model_dump(mode="json")
    return {
        "schema_version": MEDICAL_RECORD_SCHEMA_VERSION,
        "record_id": str(record_id),
        "session_id": str(session_id),
        "record_version": artifact.revision,
        "formula": formula.formula.model_dump(mode="json"),
        "safety_review": result_dump,
        "doctor_review": _doctor_review_output(review),
        "authority_refs": {
            "formula": _formula_ref(formula),
            "safety": {
                "artifact_id": str(safety_record.artifact_id),
                "revision": safety_record.revision,
                "content_digest": safety_record.content_digest,
                "safety_rule_run_id": str(safety_run_id),
            },
            "doctor_review": {
                "artifact_id": str(review_record.artifact_id),
                "revision": review_record.revision,
                "content_digest": review_record.content_digest,
            },
        },
        "disclaimer": RECORD_DISCLAIMER,
    }


def _record_session_updates(state_version: int) -> dict[str, object]:
    return {
        "current_stage": "done",
        "status": "done",
        "pending_review": False,
        "recovery_status": "normal",
        "blocked_reason": None,
        "blocked_at": None,
        "state_snapshot": {
            "agent_runtime": "langgraph",
            "current_stage": "done",
            "state_version": state_version,
            "pending_review": False,
            "langgraph_record": {
                "version": RECORD_POLICY_VERSION,
                "route": "record_completed",
            },
        },
    }


def _advance_response(
    *,
    session_id: uuid.UUID,
    state_version: int,
    artifact: ArtifactRevisionSchema,
    record_id: uuid.UUID,
    trace_id: str,
) -> dict[str, Any]:
    return {
        "session_id": str(session_id),
        "current_stage": "done",
        "from_stage": "record",
        "state_version": state_version,
        "blocked_reason": None,
        "agent_name": "record_subgraph",
        "trace_id": trace_id,
        "route": NODE_REVIEW_PLACEHOLDER,
        "artifact_refs": [
            {
                "kind": MEDICAL_RECORD_ARTIFACT_TYPE,
                "artifact_id": str(artifact.artifact_id),
                "revision": artifact.revision,
            }
        ],
        "gate_results": [
            {
                "gate_name": "record_consistency",
                "decision": "passed",
                "policy_version": RECORD_POLICY_VERSION,
            }
        ],
        "record_id": str(record_id),
    }


def _record_domain_commit_key(command_id: str) -> str:
    value = f"{command_id}:record"
    return f"command:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


async def resolve_committed_record_advance(
    *,
    session_id: uuid.UUID,
    command_id: str,
    payload_digest: str,
) -> dict[str, Any] | None:
    """Read and verify a durable Record outcome without changing any claim.

    This resolver closes the narrow window where the atomic Domain commit
    completed but the public/internal advance claim did not.  Every row used
    to reconstruct the response must belong to the same command/run and agree
    on the input/output versions; partial or tampered state fails closed.
    """

    factory = get_session_factory()
    async with factory() as db:
        claim = await db.scalar(
            select(IntakeCommandClaim).where(
                IntakeCommandClaim.session_id == session_id,
                IntakeCommandClaim.idempotency_key == command_id,
            )
        )
        if claim is None:
            return None
        if claim.payload_digest != payload_digest:
            raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)

        commits = tuple(
            await db.scalars(
                select(DomainCommandCommit).where(
                    DomainCommandCommit.session_id == session_id,
                    DomainCommandCommit.idempotency_key == _record_domain_commit_key(command_id),
                    DomainCommandCommit.graph_run_id == claim.run_id,
                    DomainCommandCommit.input_state_version == claim.input_state_version,
                )
            )
        )
        if len(commits) != 1:
            return None
        commit = commits[0]
        if (
            not commit.changed
            or commit.output_state_version != claim.input_state_version + 1
        ):
            return None

        session = await db.get(ConsultSession, session_id)
        run = await db.get(GraphRun, claim.run_id)
        if (
            session is None
            or session.current_stage != "done"
            or session.status != "done"
            or session.pending_review
            or session.recovery_status != "normal"
            or session.state_version != commit.output_state_version
            or run is None
            or run.session_id != session_id
            or run.command_id != command_id
            or run.input_state_version != claim.input_state_version
            or run.status != "completed"
            or run.completed_at is None
        ):
            return None

        artifact_id = _stable_record_artifact_id(session_id)
        artifacts = tuple(
            await db.scalars(
                select(ArtifactRevision).where(
                    ArtifactRevision.session_id == session_id,
                    ArtifactRevision.artifact_id == artifact_id,
                    ArtifactRevision.artifact_type == MEDICAL_RECORD_ARTIFACT_TYPE,
                    ArtifactRevision.status == "current",
                )
            )
        )
        if len(artifacts) != 1:
            return None
        artifact_row = artifacts[0]
        if (
            artifact_row.input_state_version != claim.input_state_version
            or artifact_row.produced_by_run_id != claim.run_id
        ):
            return None

        payload_row = await db.scalar(
            select(ArtifactRevisionPayload).where(
                ArtifactRevisionPayload.artifact_revision_id == artifact_row.id,
                ArtifactRevisionPayload.session_id == session_id,
                ArtifactRevisionPayload.artifact_id == artifact_id,
                ArtifactRevisionPayload.revision == artifact_row.revision,
            )
        )
        if (
            payload_row is None
            or payload_row.payload_schema_version != MEDICAL_RECORD_SCHEMA_VERSION
            or artifact_payload_digest(
                payload_row.payload_schema_version,
                cast(dict[str, object], payload_row.payload),
            )
            != payload_row.content_digest
            or set(payload_row.payload) != {"kind", "record", "record_text"}
            or payload_row.payload.get("kind") != MEDICAL_RECORD_ARTIFACT_TYPE
            or not isinstance(payload_row.payload.get("record"), dict)
            or not isinstance(payload_row.payload.get("record_text"), str)
        ):
            return None

        expected_record_id = _projection_record_id(artifact_id, artifact_row.revision)
        medical_record = await db.get(MedicalRecord, expected_record_id)
        record_json = cast(dict[str, object], payload_row.payload["record"])
        if (
            medical_record is None
            or medical_record.session_id != session_id
            or medical_record.version != artifact_row.revision
            or medical_record.doctor_review_id is None
            or medical_record.edited_by_doctor
            or medical_record.diff_from_previous is not None
            or medical_record.record_text != payload_row.payload["record_text"]
            or medical_record.record_json != record_json
            or medical_record.disclaimer != RECORD_DISCLAIMER
            or set(record_json)
            != {
                "schema_version",
                "record_id",
                "session_id",
                "record_version",
                "formula",
                "safety_review",
                "doctor_review",
                "authority_refs",
                "disclaimer",
            }
            or record_json.get("schema_version") != MEDICAL_RECORD_SCHEMA_VERSION
            or record_json.get("record_id") != str(expected_record_id)
            or record_json.get("session_id") != str(session_id)
            or record_json.get("record_version") != artifact_row.revision
            or record_json.get("disclaimer") != RECORD_DISCLAIMER
            or not isinstance(record_json.get("formula"), dict)
            or not isinstance(record_json.get("safety_review"), dict)
            or not isinstance(record_json.get("doctor_review"), dict)
            or not isinstance(record_json.get("authority_refs"), dict)
        ):
            return None
        doctor_output = cast(dict[str, object], record_json["doctor_review"])
        review = await db.get(DoctorReview, medical_record.doctor_review_id)
        if (
            review is None
            or review.session_id != session_id
            or review.safety_rule_run_id is None
            or doctor_output != _doctor_review_output(review)
        ):
            return None

        record_gates = tuple(
            await db.scalars(
                select(GateResult).where(
                    GateResult.session_id == session_id,
                    GateResult.graph_run_id == claim.run_id,
                    GateResult.gate_name == "record_consistency",
                    GateResult.input_state_version == claim.input_state_version,
                )
            )
        )
        if len(record_gates) != 1:
            return None
        gate = record_gates[0]
        if (
            gate.policy_version != RECORD_POLICY_VERSION
            or gate.decision != "passed"
            or not isinstance(gate.details, dict)
            or gate.details.get("artifact_digest") != payload_row.content_digest
        ):
            return None

        outbox = await db.get(OutboxEvent, commit.outbox_event_id)
        if (
            outbox is None
            or outbox.session_id != session_id
            or outbox.graph_run_id != claim.run_id
            or outbox.event_type != "session.done.v1"
            or outbox.state_version != commit.output_state_version
            or outbox.payload.get("record_id") != str(expected_record_id)
            or outbox.payload.get("record_artifact_id") != str(artifact_id)
            or outbox.payload.get("record_revision") != artifact_row.revision
        ):
            return None

        artifact = ArtifactRevisionSchema(
            artifact_id=artifact_row.artifact_id,
            artifact_type=artifact_row.artifact_type,
            revision=artifact_row.revision,
            session_id=artifact_row.session_id,
            input_state_version=artifact_row.input_state_version,
            status=ArtifactStatus(artifact_row.status),
            produced_by_run_id=artifact_row.produced_by_run_id,
            parent_revision_id=artifact_row.parent_revision_id,
            parent_revision=artifact_row.parent_revision,
            created_at=artifact_row.created_at,
        )
        return _advance_response(
            session_id=session_id,
            state_version=commit.output_state_version,
            artifact=artifact,
            record_id=expected_record_id,
            trace_id=str(claim.run_id),
        )


async def execute_record_command(state: XuanhuGraphState) -> dict[str, Any]:
    """Assemble and atomically persist one deterministic product record."""

    try:
        session_id = uuid.UUID(state.get("session_id", ""))
        run_id = uuid.UUID(state.get("run_id", ""))
    except (TypeError, ValueError):
        return {
            "route": NODE_REVIEW_PLACEHOLDER,
            "last_error": {
                "code": "RECORD_COMMAND_REF_INVALID",
                "trace_id": state.get("run_id", ""),
                "detail": "record command refs are invalid",
            },
        }
    command_id = state.get("command_id", "")
    if not command_id:
        raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
    session = await _load_record_session(session_id)
    repository = PostgresDomainRepository(get_session_factory())
    domain_state = await repository.get_state(session_id)
    if session.state_version != domain_state.state_version:
        raise RepositoryError(RepositoryErrorCode.STATE_VERSION_CONFLICT)
    review_record, review, formula, safety_record, safety_result, safety_run_id = (
        await _load_doctor_review_authority(repository, domain_state)
    )

    artifact_id = _stable_record_artifact_id(session_id)
    latest = await repository.get_artifact_payload(
        session_id,
        artifact_type=MEDICAL_RECORD_ARTIFACT_TYPE,
        artifact_id=artifact_id,
        status=None,
    )
    artifact = _artifact_revision(
        session_id=session_id,
        artifact_id=artifact_id,
        artifact_type=MEDICAL_RECORD_ARTIFACT_TYPE,
        state_version=domain_state.state_version,
        run_id=run_id,
        latest=latest,
    )
    record_id = _projection_record_id(artifact.artifact_id, artifact.revision)
    record_json = _record_json(
        session_id=session_id,
        record_id=record_id,
        artifact=artifact,
        review_record=review_record,
        review=review,
        formula=formula,
        safety_record=safety_record,
        safety_result=safety_result,
        safety_run_id=safety_run_id,
    )
    record_text = _render_record_text(
        formula=formula,
        action=review.action,
        safety_rule_version=safety_result.rule_version,
    )
    payload: dict[str, object] = {
        "kind": MEDICAL_RECORD_ARTIFACT_TYPE,
        "record": record_json,
        "record_text": record_text,
    }
    payload_spec: ArtifactPayloadSpec = _payload_spec(
        artifact,
        schema_version=MEDICAL_RECORD_SCHEMA_VERSION,
        payload=payload,
    )
    delta = DomainDelta(
        delta_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:delta:record:{run_id}"),
        run_id=run_id,
        session_id=session_id,
        expected_state_version=domain_state.state_version,
        artifact_revisions=(artifact,),
    )
    trace_id = _node_trace_id(state)
    commit = await repository.commit(
        delta,
        _verification_context(
            delta,
            domain_state,
            stage="record",
            idempotency_key=f"{command_id}:record",
            trace_id=trace_id,
        ),
        graph_version=DEFAULT_GRAPH_VERSION,
        gate_results=(
            GateResultSchema(
                gate_name="record_consistency",
                policy_version=RECORD_POLICY_VERSION,
                input_state_version=domain_state.state_version,
                decision=GateDecision.PASSED,
                details={
                    "artifact_digest": payload_spec.content_digest,
                    "doctor_review_artifact_id": str(review_record.artifact_id),
                    "doctor_review_revision": review_record.revision,
                    "safety_artifact_id": str(safety_record.artifact_id),
                    "safety_revision": safety_record.revision,
                },
            ),
        ),
        graph_steps=(
            GraphStepSpec(step_name="assemble_record", status="completed", metadata={}),
            GraphStepSpec(step_name="verify_record_consistency", status="completed", metadata={}),
        ),
        artifact_payloads=(payload_spec,),
        medical_records=(
            MedicalRecordSpec(
                record_id=record_id,
                session_id=session_id,
                version=artifact.revision,
                record_text=record_text,
                record_json=record_json,
                doctor_review_id=review.id,
                disclaimer=RECORD_DISCLAIMER,
            ),
        ),
        audit_events=(
            AuditEventSpec(
                event_id=uuid.uuid5(uuid.NAMESPACE_URL, f"xuanhu:audit:record-generated:{record_id}"),
                session_id=session_id,
                event_type="record.generated",
                actor_type="system",
                actor_id=None,
                payload={
                    "record_id": str(record_id),
                    "artifact_id": str(artifact.artifact_id),
                    "revision": artifact.revision,
                },
                trace_id=trace_id,
            ),
        ),
        session_updates=_record_session_updates(domain_state.state_version + 1),
        outbox_event_type="session.done.v1",
        outbox_payload={
            "session_id": str(session_id),
            "record_id": str(record_id),
            "record_artifact_id": str(artifact.artifact_id),
            "record_revision": artifact.revision,
            "input_state_version": domain_state.state_version,
            "output_state_version": domain_state.state_version + 1,
        },
    )
    response = _advance_response(
        session_id=session_id,
        state_version=commit.output_state_version,
        artifact=artifact,
        record_id=record_id,
        trace_id=trace_id,
    )
    await _complete_advance_claim(
        session_id=session_id,
        command_id=command_id,
        response=response,
        state_version=commit.output_state_version,
    )
    refs: list[ArtifactRef] = cast(list[ArtifactRef], response["artifact_refs"])
    return {
        "route": NODE_REVIEW_PLACEHOLDER,
        "domain_state_version": commit.output_state_version,
        "artifact_refs": refs,
        "gate_results": response["gate_results"],
        "pending_interrupt": None,
        "last_error": None,
    }


__all__ = [
    "MEDICAL_RECORD_ARTIFACT_TYPE",
    "MEDICAL_RECORD_SCHEMA_VERSION",
    "RECORD_DISCLAIMER",
    "RECORD_POLICY_VERSION",
    "execute_record_command",
    "resolve_committed_record_advance",
]
