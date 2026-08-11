"""PostgreSQL Domain Repository and transactional Outbox for L2-5."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_runtime.reducer import (
    DomainDelta,
    DomainReducerError,
    DomainState,
    domain_delta_digest,
    reduce_domain_state,
)
from app.agent_runtime.verifiers import VerificationContext
from app.models.agent import AgentEvidence, AgentRun
from app.models.audit import AuditEvent
from app.models.consult import ConsultMessage, ConsultSession
from app.models.domain import (
    ArtifactRevision,
    ArtifactRevisionPayload,
    DomainCommandCommit,
    GateResult,
    GraphRun,
    GraphRunStep,
    Observation,
    OutboxEvent,
    SafetyFactAssertion,
    SafetyProfile,
)
from app.models.question_contract import QuestionContractRecord, QuestionCoverageEventRecord
from app.models.review import DoctorReview, MedicalRecord
from app.models.safety import SafetyRuleRun
from app.schemas.domain import ArtifactRevisionSchema, GateResultSchema, ObservationSchema, SafetyProfileSchema
from app.schemas.question_contract import (
    QUESTION_CONTRACT_SCHEMA_VERSION,
    QUESTION_COVERAGE_EVENT_SCHEMA_VERSION,
    CoverageEventItem,
    QuestionAspect,
    QuestionContract,
    QuestionCoverageEvent,
)

DOMAIN_STATE_COMMITTED = "domain.state_committed.v1"
SAFETY_FACT_MANIFEST_VERSION = "intake-safety-fact-manifest.v1"
_INTAKE_SAFETY_SOURCE_KINDS = (
    "model_extraction",
    "deterministic_precheck",
    "deterministic_reply_binding",
)
_INTAKE_REPLY_BINDING_VERSION = "intake-reply-binding.v1"
_SAFETY_REPLY_DIMENSION_BY_FIELD = {
    "allergy": "safety.allergy_status",
    "pregnancy": "safety.pregnancy_status",
    "lactation": "safety.lactation_status",
    "medications": "safety.medication_status",
    "major_conditions": "safety.major_condition_status",
}
_EXPLICIT_NONE_REPLY = re.compile(
    r"^\s*(?:无|没有|否|不是|未有|none|no)\s*[。.!！]?\s*$",
    re.IGNORECASE,
)
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _canonical_json_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def artifact_payload_digest(payload_schema_version: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        {"payload_schema_version": payload_schema_version, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _contract_record_values(contract: QuestionContract) -> dict[str, object]:
    """Durable column values for one immutable question contract."""
    return {
        "id": contract.contract_id,
        "schema_version": contract.schema_version,
        "session_id": contract.session_id,
        "question_message_id": contract.question_message_id,
        "root_contract_id": contract.root_contract_id,
        "parent_contract_id": contract.parent_contract_id,
        "revision": contract.revision,
        "dimension": contract.dimension,
        "selection_kind": contract.selection_kind,
        "safety_critical": contract.safety_critical,
        "max_followups": contract.max_followups,
        "question_digest": contract.question_digest,
        "aspects": [aspect.model_dump(mode="json") for aspect in contract.aspects],
        "contract_digest": contract.contract_digest,
    }


def _coverage_event_record_values(event: QuestionCoverageEvent) -> dict[str, object]:
    """Durable column values for one append-only, quote-free coverage event."""
    return {
        "id": event.event_id,
        "schema_version": event.schema_version,
        "session_id": event.session_id,
        "contract_id": event.contract_id,
        "root_contract_id": event.root_contract_id,
        "answer_message_id": event.answer_message_id,
        "items": [item.model_dump(mode="json") for item in event.items],
        "event_digest": event.event_digest,
    }


def _contract_schema(row: QuestionContractRecord) -> QuestionContract:
    """Rebuild the immutable schema from a protected row, re-verifying the
    canonical digest so tampered storage fails closed."""
    if row.schema_version != QUESTION_CONTRACT_SCHEMA_VERSION:
        raise ValueError("question contract schema_version mismatch")
    return QuestionContract.model_validate(
        {
            "contract_id": row.id,
            "session_id": row.session_id,
            "question_message_id": row.question_message_id,
            "root_contract_id": row.root_contract_id,
            "parent_contract_id": row.parent_contract_id,
            "revision": row.revision,
            "dimension": row.dimension,
            "selection_kind": row.selection_kind,
            "safety_critical": row.safety_critical,
            "max_followups": row.max_followups,
            "question_digest": row.question_digest,
            "aspects": [QuestionAspect.model_validate(item) for item in row.aspects],
            "contract_digest": row.contract_digest,
        }
    )


def _coverage_event_schema(row: QuestionCoverageEventRecord) -> QuestionCoverageEvent:
    """Rebuild the immutable coverage event from a protected row, re-verifying
    the canonical digest so tampered storage fails closed."""
    if row.schema_version != QUESTION_COVERAGE_EVENT_SCHEMA_VERSION:
        raise ValueError("question coverage event schema_version mismatch")
    return QuestionCoverageEvent.model_validate(
        {
            "event_id": row.id,
            "session_id": row.session_id,
            "contract_id": row.contract_id,
            "root_contract_id": row.root_contract_id,
            "answer_message_id": row.answer_message_id,
            "items": [CoverageEventItem.model_validate(item) for item in row.items],
            "event_digest": row.event_digest,
        }
    )


class RepositoryErrorCode(StrEnum):
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    STATE_VERSION_CONFLICT = "STATE_VERSION_CONFLICT"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    ARTIFACT_PARENT_INVALID = "ARTIFACT_PARENT_INVALID"
    ARTIFACT_PAYLOAD_INVALID = "ARTIFACT_PAYLOAD_INVALID"
    UNSAFE_METADATA = "UNSAFE_METADATA"
    TRANSACTION_FAILED = "TRANSACTION_FAILED"


class RepositoryError(RuntimeError):
    """A payload-free repository failure with a stable code."""

    def __init__(self, code: RepositoryErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class OutboxErrorCode(StrEnum):
    PUBLISH_TIMEOUT = "PUBLISH_TIMEOUT"
    PUBLISH_UNAVAILABLE = "PUBLISH_UNAVAILABLE"
    PUBLISH_REJECTED = "PUBLISH_REJECTED"
    PUBLISH_UNKNOWN = "PUBLISH_UNKNOWN"


class CommitResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    commit_id: UUID
    session_id: UUID
    graph_run_id: UUID
    outbox_event_id: UUID
    input_state_version: int = Field(ge=1)
    output_state_version: int = Field(ge=1)
    changed: bool


class ReasoningAuthoritySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: UUID
    current_state_version: int = Field(ge=1)
    current_stage: str = Field(min_length=1, max_length=32)
    session_status: str = Field(min_length=1, max_length=32)
    agent_runtime: str = Field(min_length=1, max_length=16)
    domain_state: DomainState
    source_gate_id: UUID
    source_gate_state_version: int = Field(ge=1)
    triage_gate: GateResultSchema
    completeness_gate: GateResultSchema
    intake_graph_run_id: UUID
    advance_run_id: UUID | None = None

    @model_validator(mode="after")
    def authority_consistency(self) -> ReasoningAuthoritySnapshot:
        if self.domain_state.session_id != self.session_id:
            raise ValueError("domain_state session must match authority session")
        if self.domain_state.state_version != self.current_state_version:
            raise ValueError("domain_state version must match current authority version")
        if (
            self.triage_gate.input_state_version != self.source_gate_state_version
            or self.completeness_gate.input_state_version != self.source_gate_state_version
        ):
            raise ValueError("gate input versions must match source gate version")
        return self


class GraphStepSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    step_name: str = Field(min_length=1, max_length=64)
    status: str = Field(pattern=r"^(started|completed|failed|skipped)$")
    metadata: dict[str, object] | None = None


class ArtifactPayloadSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: UUID
    artifact_id: UUID
    revision: int = Field(ge=1)
    payload_schema_version: str = Field(min_length=1, max_length=64)
    payload: dict[str, object]
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def digest_matches_payload(self) -> ArtifactPayloadSpec:
        if self.content_digest != artifact_payload_digest(self.payload_schema_version, self.payload):
            raise ValueError("artifact payload digest mismatch")
        return self


class ArtifactPayloadRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: UUID
    artifact_revision_row_id: UUID
    session_id: UUID
    artifact_id: UUID
    artifact_type: str
    revision: int = Field(ge=1)
    input_state_version: int = Field(ge=1)
    status: str = Field(min_length=1, max_length=16)
    produced_by_run_id: UUID
    parent_revision_id: UUID | None = None
    parent_revision: int | None = None
    payload_schema_version: str = Field(min_length=1, max_length=64)
    payload: dict[str, object]
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def digest_matches_payload(self) -> ArtifactPayloadRecord:
        if self.content_digest != artifact_payload_digest(self.payload_schema_version, self.payload):
            raise ValueError("artifact payload digest mismatch")
        return self


class ConsultMessageSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: UUID
    role: str = Field(pattern=r"^(doctor|patient_proxy|agent|system)$")
    stage: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=5000)
    agent_name: str | None = Field(default=None, max_length=32)
    structured_delta: dict[str, object] | None = None
    trace_id: str | None = Field(default=None, max_length=64)


class SafetyFactEvidenceSpec(BaseModel):
    """Privacy-minimal, immutable evidence reference for a safety proposal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_message_id: UUID
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    quote_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reply_to_question_message_id: UUID | None = None
    reply_dimension: str | None = Field(
        default=None,
        pattern=(
            r"^safety\.(allergy_status|pregnancy_status|lactation_status|"
            r"medication_status|major_condition_status)$"
        ),
    )

    @model_validator(mode="after")
    def valid_range_and_reply_binding(self) -> SafetyFactEvidenceSpec:
        if self.end_char <= self.start_char:
            raise ValueError("safety evidence range must be non-empty")
        if (self.reply_to_question_message_id is None) != (self.reply_dimension is None):
            raise ValueError("safety evidence reply binding must be complete")
        return self


class SafetyFactAssertionSpec(BaseModel):
    """Atomic, content-addressed safety proposal persisted with a Domain commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assertion_id: UUID
    session_id: UUID
    field_name: str = Field(
        pattern=r"^(allergy|pregnancy|lactation|medications|major_conditions|contraindications|red_flag)$"
    )
    value: dict[str, object]
    value_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    assertion_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: str = Field(default="proposed", pattern=r"^proposed$")
    source_kind: str = Field(
        pattern=r"^(model_extraction|deterministic_precheck|deterministic_reply_binding)$"
    )
    source_message_id: UUID
    extraction_run_id: UUID
    template_version: str = Field(min_length=1, max_length=64)
    evidence_spans: tuple[SafetyFactEvidenceSpec, ...] = Field(min_length=1)
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_by_actor_type: str = Field(pattern=r"^(model|system)$")
    proposed_by_actor_id: None = None
    audit_event_id: UUID
    audit_event_type: str = Field(default="safety_fact.proposed", pattern=r"^safety_fact\.proposed$")
    audit_actor_type: str = Field(pattern=r"^(agent|system)$")
    audit_actor_id: None = None
    audit_trace_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def verify_content_addressed_identity(self) -> SafetyFactAssertionSpec:
        evidence_payload = [item.model_dump(mode="json", exclude_none=True) for item in self.evidence_spans]
        if self.value_digest != _canonical_json_digest(self.value):
            raise ValueError("safety assertion value digest mismatch")
        if self.evidence_digest != _canonical_json_digest(evidence_payload):
            raise ValueError("safety assertion evidence digest mismatch")
        expected_fingerprint = _canonical_json_digest(
            {
                "session_id": str(self.session_id),
                "field_name": self.field_name,
                "value_digest": self.value_digest,
                "source_kind": self.source_kind,
                "source_message_id": str(self.source_message_id),
                "extraction_run_id": str(self.extraction_run_id),
                "template_version": self.template_version,
                "evidence_digest": self.evidence_digest,
            }
        )
        if self.assertion_fingerprint != expected_fingerprint:
            raise ValueError("safety assertion fingerprint mismatch")
        if self.assertion_id != uuid5(
            NAMESPACE_URL,
            f"xuanhu:safety-assertion:{self.session_id}:{expected_fingerprint}",
        ):
            raise ValueError("safety assertion stable id mismatch")
        if self.audit_event_id != uuid5(self.assertion_id, "safety-fact-proposal-audit.v1"):
            raise ValueError("safety proposal audit stable id mismatch")
        expected_actor = "agent" if self.source_kind == "model_extraction" else "system"
        expected_proposer = "model" if self.source_kind == "model_extraction" else "system"
        if self.audit_actor_type != expected_actor or self.proposed_by_actor_type != expected_proposer:
            raise ValueError("safety proposal actor metadata does not match source kind")
        if any(item.source_message_id != self.source_message_id for item in self.evidence_spans):
            raise ValueError("safety evidence source message mismatch")
        bindings = tuple(
            (item.reply_to_question_message_id, item.reply_dimension)
            for item in self.evidence_spans
        )
        if self.source_kind == "deterministic_reply_binding":
            expected_dimension = _SAFETY_REPLY_DIMENSION_BY_FIELD.get(self.field_name)
            if expected_dimension is None or any(
                question_id is None or dimension is None
                for question_id, dimension in bindings
            ):
                raise ValueError("deterministic safety reply requires a bindable field and bound evidence")
            question_ids = {question_id for question_id, _ in bindings}
            dimensions = {dimension for _, dimension in bindings}
            if len(question_ids) != 1 or dimensions != {expected_dimension}:
                raise ValueError("deterministic safety reply binding does not match its field")
            if self.value.get("collection_status") != "explicitly_none":
                raise ValueError("deterministic safety reply may only represent an explicit negative")
        elif any(question_id is not None or dimension is not None for question_id, dimension in bindings):
            raise ValueError("only deterministic safety replies may carry question binding")
        if self.source_kind == "deterministic_precheck" and self.field_name != "red_flag":
            raise ValueError("deterministic triage precheck may only propose a red flag")
        return self


def _safety_fact_manifest_entry(
    *,
    assertion_id: UUID,
    assertion_fingerprint: str,
    value_digest: str,
    evidence_digest: str,
    field_name: str,
    source_kind: str,
    source_message_id: UUID,
    extraction_run_id: UUID | None,
    template_version: str,
) -> dict[str, object]:
    return {
        "assertion_id": str(assertion_id),
        "assertion_fingerprint": assertion_fingerprint,
        "value_digest": value_digest,
        "evidence_digest": evidence_digest,
        "field_name": field_name,
        "source_kind": source_kind,
        "source_message_id": str(source_message_id),
        "extraction_run_id": str(extraction_run_id) if extraction_run_id is not None else None,
        "template_version": template_version,
    }


def _safety_fact_manifest(entries: Sequence[dict[str, object]]) -> dict[str, object]:
    ordered = sorted(entries, key=lambda item: str(item["assertion_id"]))
    return {
        "version": SAFETY_FACT_MANIFEST_VERSION,
        "count": len(ordered),
        "assertion_ids": [str(item["assertion_id"]) for item in ordered],
        "content_digest": _canonical_json_digest(ordered),
    }


def _safety_fact_spec_manifest(specs: Sequence[SafetyFactAssertionSpec]) -> dict[str, object]:
    return _safety_fact_manifest(
        [
            _safety_fact_manifest_entry(
                assertion_id=item.assertion_id,
                assertion_fingerprint=item.assertion_fingerprint,
                value_digest=item.value_digest,
                evidence_digest=item.evidence_digest,
                field_name=item.field_name,
                source_kind=item.source_kind,
                source_message_id=item.source_message_id,
                extraction_run_id=item.extraction_run_id,
                template_version=item.template_version,
            )
            for item in specs
        ]
    )


class SafetyRuleRunSpec(BaseModel):
    """Atomic compatibility projection for one authoritative safety artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    safety_rule_run_id: UUID
    session_id: UUID
    agent_run_id: UUID | None = None
    formula_source: str = Field(pattern=r"^(agent_output|doctor_override)$")
    passed: bool
    issues: list[dict[str, object]]
    formula_snapshot: dict[str, object]
    normalized_formula: dict[str, object] | None
    patient_snapshot: dict[str, object]
    rule_version: str = Field(min_length=1, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)


class DoctorReviewSpec(BaseModel):
    """Atomic compatibility projection for a persisted review submission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_id: UUID
    session_id: UUID
    agent_run_id: UUID | None = None
    safety_rule_run_id: UUID
    action: str = Field(pattern=r"^(confirm|modify|reject|request_more_info)$")
    original_formula: dict[str, object] | None = None
    formula_override: dict[str, object] | None = None
    feedback: str | None = Field(default=None, max_length=2000)
    reviewed_by: str | None = Field(default=None, max_length=128)


class MedicalRecordSpec(BaseModel):
    """Atomic compatibility projection for one authoritative record artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID
    session_id: UUID
    version: int = Field(ge=1)
    record_text: str = Field(min_length=1, max_length=100_000)
    record_json: dict[str, object]
    doctor_review_id: UUID
    disclaimer: str = Field(min_length=1, max_length=2000)
    edited_by_doctor: bool = False
    diff_from_previous: dict[str, object] | None = None

    @model_validator(mode="after")
    def generated_record_is_not_doctor_edited(self) -> MedicalRecordSpec:
        if self.edited_by_doctor or self.diff_from_previous is not None:
            raise ValueError("generated record projection cannot claim a doctor edit")
        return self


class AuditEventSpec(BaseModel):
    """Atomic audit projection for product Domain transitions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    session_id: UUID
    event_type: str = Field(min_length=1, max_length=64)
    actor_type: str = Field(pattern=r"^(doctor|agent|system)$")
    actor_id: str | None = Field(default=None, max_length=128)
    payload: dict[str, object]
    trace_id: str | None = Field(default=None, max_length=64)


class AgentRunSpec(BaseModel):
    """Atomic compatibility projection for one agent run row.

    ``run_id`` 即 AgentRun.id（与 RunSpec.run_id 一致）——天然幂等：
    同一 run 重放时按 id 命中已存在行，值一致即静默通过。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    session_id: UUID
    agent_name: str = Field(min_length=1, max_length=32)
    stage: str = Field(min_length=1, max_length=32)
    input_snapshot: dict[str, object] | None = None
    output_snapshot: dict[str, object] | None = None
    prompt_version: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    retry_count: int = Field(default=0, ge=0)
    status: str = Field(pattern=r"^(success|failed|blocked)$")
    error_code: str | None = Field(default=None, max_length=64)
    latency_ms: int | None = Field(default=None, ge=0)
    trace_id: str | None = Field(default=None, max_length=64)


class AgentEvidenceSpec(BaseModel):
    """Atomic compatibility projection for one agent_evidences row.

    ``evidence_row_id`` 为确定性行 ID（uuid5(run_id, evidence_id)），
    保证幂等重放不产生重复行。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_row_id: UUID
    agent_run_id: UUID
    session_id: UUID
    evidence_id: str = Field(min_length=1, max_length=128)
    source_type: str = Field(pattern=r"^(formula|herb|acupoint|theory|case)$")
    source_id: UUID
    chunk_id: UUID | None = None
    title: str | None = Field(default=None, max_length=255)
    content_snippet: str | None = None
    score: float | None = Field(default=None, ge=0, le=1)
    rank: int | None = Field(default=None, ge=1)


class OutboxMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    event_type: str
    session_id: UUID
    # Nullable for async_command.* rows, which are not graph runs.
    graph_run_id: UUID | None
    state_version: int = Field(ge=1)
    trace_id: str
    payload: dict[str, object]
    status: str
    attempt_count: int = Field(ge=0)
    leased_by: str | None


class OutboxHealth(BaseModel):
    """Privacy-safe operational counters for the durable outbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backlog_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    leased_count: int = Field(ge=0)
    dead_letter_count: int = Field(ge=0)
    oldest_unpublished_age_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def backlog_matches_states(self) -> OutboxHealth:
        if self.backlog_count != self.pending_count + self.leased_count:
            raise ValueError("backlog_count must equal pending_count + leased_count")
        return self


class DomainRepository(Protocol):
    async def get_state(self, session_id: UUID) -> DomainState: ...

    async def get_gate_results(self, session_id: UUID, state_version: int) -> tuple[GateResultSchema, ...]: ...

    async def get_reasoning_authority(self, session_id: UUID, state_version: int) -> ReasoningAuthoritySnapshot | None: ...

    async def get_artifact_payload(
        self,
        session_id: UUID,
        *,
        artifact_type: str,
        artifact_id: UUID | None = None,
        revision: int | None = None,
        status: str | None = "current",
    ) -> ArtifactPayloadRecord | None: ...

    async def commit(
        self,
        delta: DomainDelta,
        context: VerificationContext,
        *,
        graph_version: str,
        gate_results: Sequence[GateResultSchema] = (),
        graph_steps: Sequence[GraphStepSpec] = (),
        artifact_payloads: Sequence[ArtifactPayloadSpec] = (),
        consult_messages: Sequence[ConsultMessageSpec] = (),
        safety_fact_assertions: Sequence[SafetyFactAssertionSpec] = (),
        safety_rule_runs: Sequence[SafetyRuleRunSpec] = (),
        doctor_reviews: Sequence[DoctorReviewSpec] = (),
        medical_records: Sequence[MedicalRecordSpec] = (),
        agent_runs: Sequence[AgentRunSpec] = (),
        agent_evidences: Sequence[AgentEvidenceSpec] = (),
        audit_events: Sequence[AuditEventSpec] = (),
        session_updates: dict[str, object] | None = None,
        outbox_event_type: str = DOMAIN_STATE_COMMITTED,
        outbox_payload: dict[str, object] | None = None,
    ) -> CommitResult: ...


class OutboxRepository(Protocol):
    async def claim(self, *, worker_id: str, limit: int, lease_seconds: int) -> tuple[OutboxMessage, ...]: ...

    async def acknowledge(self, event_id: UUID, *, worker_id: str) -> bool: ...

    async def release_failed(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        error_code: OutboxErrorCode,
        retry_after_seconds: int,
    ) -> bool: ...

    async def dead_letter(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        error_code: OutboxErrorCode,
    ) -> bool: ...

    async def get_outbox_health(self) -> OutboxHealth: ...


class PostgresDomainRepository(DomainRepository, OutboxRepository):
    """Async SQLAlchemy repository whose correctness relies on PostgreSQL locks."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_state(self, session_id: UUID) -> DomainState:
        async with self._session_factory() as session:
            session_row = await session.get(ConsultSession, session_id)
            if session_row is None:
                raise RepositoryError(RepositoryErrorCode.SESSION_NOT_FOUND)
            return await self._load_state(session, session_row)

    async def get_gate_results(self, session_id: UUID, state_version: int) -> tuple[GateResultSchema, ...]:
        try:
            async with self._session_factory() as session:
                session_row = await session.get(ConsultSession, session_id)
                if session_row is None:
                    raise RepositoryError(RepositoryErrorCode.SESSION_NOT_FOUND)
                rows = await self._gate_rows(session, session_id, state_version)
                if len({row.graph_run_id for row in rows}) > 1:
                    return ()
                return tuple(self._gate_schema(row) for row in self._ordered_gate_rows(rows))
        except RepositoryError:
            raise
        except SQLAlchemyError:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def get_reasoning_authority(self, session_id: UUID, state_version: int) -> ReasoningAuthoritySnapshot | None:
        try:
            async with self._session_factory() as session, session.begin():
                session_row = await session.get(ConsultSession, session_id, with_for_update=True)
                if session_row is None:
                    raise RepositoryError(RepositoryErrorCode.SESSION_NOT_FOUND)
                if (
                    session_row.state_version != state_version
                    or session_row.current_stage != "syndrome"
                    or session_row.status != "active"
                    or session_row.agent_runtime != "langgraph"
                    or session_row.recovery_status == "manual_required"
                ):
                    return None
                source = self._advance_source(session_row.state_snapshot, state_version)
                if source is None:
                    return None
                source_gate_id, source_gate_state_version = source
                domain_state = await self._load_state(session, session_row)
                if domain_state.state_version != state_version:
                    return None
                source_gate = await session.scalar(
                    select(GateResult)
                    .join(GraphRun, GateResult.graph_run_id == GraphRun.id)
                    .where(
                        GateResult.id == source_gate_id,
                        GateResult.session_id == session_id,
                        GateResult.gate_name == "completeness",
                        GateResult.input_state_version == source_gate_state_version,
                        GraphRun.session_id == session_id,
                        GraphRun.status == "completed",
                    )
                )
                if source_gate is None or not self._completion_gate_is_ready(source_gate):
                    return None
                rows = await self._source_gate_rows(
                    session,
                    session_id=session_id,
                    source_state_version=source_gate_state_version,
                    graph_run_id=source_gate.graph_run_id,
                )
                authority = self._authority_gate_rows(rows)
                if authority is None:
                    return None
                triage, completeness, graph_run_id = authority
                if completeness.id != source_gate_id or not self._triage_gate_is_continue(triage):
                    return None
                return ReasoningAuthoritySnapshot(
                    session_id=session_id,
                    current_state_version=domain_state.state_version,
                    current_stage=session_row.current_stage,
                    session_status=session_row.status,
                    agent_runtime=session_row.agent_runtime,
                    domain_state=domain_state,
                    source_gate_id=source_gate_id,
                    source_gate_state_version=source_gate_state_version,
                    triage_gate=self._gate_schema(triage),
                    completeness_gate=self._gate_schema(completeness),
                    intake_graph_run_id=graph_run_id,
                    advance_run_id=None,
                )
        except RepositoryError:
            raise
        except SQLAlchemyError:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def get_artifact_payload(
        self,
        session_id: UUID,
        *,
        artifact_type: str,
        artifact_id: UUID | None = None,
        revision: int | None = None,
        status: str | None = "current",
    ) -> ArtifactPayloadRecord | None:
        try:
            async with self._session_factory() as session:
                criteria = [
                    ArtifactRevision.session_id == session_id,
                    ArtifactRevision.artifact_type == artifact_type,
                ]
                if status is not None:
                    criteria.append(ArtifactRevision.status == status)
                if artifact_id is not None:
                    criteria.append(ArtifactRevision.artifact_id == artifact_id)
                if revision is not None:
                    criteria.append(ArtifactRevision.revision == revision)
                row = await session.execute(
                    select(ArtifactRevision, ArtifactRevisionPayload)
                    .join(
                        ArtifactRevisionPayload,
                        ArtifactRevisionPayload.artifact_revision_id == ArtifactRevision.id,
                    )
                    .where(*criteria)
                    .order_by(ArtifactRevision.revision.desc(), ArtifactRevision.created_at.desc())
                    .limit(1)
                )
                pair = row.one_or_none()
                if pair is None:
                    return None
                artifact, payload = pair
                record = ArtifactPayloadRecord(
                    row_id=payload.id,
                    artifact_revision_row_id=artifact.id,
                    session_id=artifact.session_id,
                    artifact_id=artifact.artifact_id,
                    artifact_type=artifact.artifact_type,
                    revision=artifact.revision,
                    input_state_version=artifact.input_state_version,
                    status=artifact.status,
                    produced_by_run_id=artifact.produced_by_run_id,
                    parent_revision_id=artifact.parent_revision_id,
                    parent_revision=artifact.parent_revision,
                    payload_schema_version=payload.payload_schema_version,
                    payload=dict(payload.payload),
                    content_digest=payload.content_digest,
                )
                if (
                    payload.session_id != artifact.session_id
                    or payload.artifact_id != artifact.artifact_id
                    or payload.revision != artifact.revision
                ):
                    raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
                return record
        except RepositoryError:
            raise
        except (SQLAlchemyError, ValueError, TypeError):
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def commit(
        self,
        delta: DomainDelta,
        context: VerificationContext,
        *,
        graph_version: str,
        gate_results: Sequence[GateResultSchema] = (),
        graph_steps: Sequence[GraphStepSpec] = (),
        artifact_payloads: Sequence[ArtifactPayloadSpec] = (),
        consult_messages: Sequence[ConsultMessageSpec] = (),
        safety_fact_assertions: Sequence[SafetyFactAssertionSpec] = (),
        safety_rule_runs: Sequence[SafetyRuleRunSpec] = (),
        doctor_reviews: Sequence[DoctorReviewSpec] = (),
        medical_records: Sequence[MedicalRecordSpec] = (),
        agent_runs: Sequence[AgentRunSpec] = (),
        agent_evidences: Sequence[AgentEvidenceSpec] = (),
        audit_events: Sequence[AuditEventSpec] = (),
        session_updates: dict[str, object] | None = None,
        outbox_event_type: str = DOMAIN_STATE_COMMITTED,
        outbox_payload: dict[str, object] | None = None,
    ) -> CommitResult:
        self._validate_metadata(context, graph_version)
        digest = domain_delta_digest(delta)
        idempotency_ref = self._stable_ref("command", context.run_spec.idempotency_key)
        try:
            async with self._session_factory() as session, session.begin():
                locked = await session.scalar(
                    select(ConsultSession).where(ConsultSession.id == delta.session_id).with_for_update()
                )
                if locked is None:
                    raise RepositoryError(RepositoryErrorCode.SESSION_NOT_FOUND)

                existing = await session.scalar(
                    select(DomainCommandCommit).where(
                        DomainCommandCommit.session_id == delta.session_id,
                        DomainCommandCommit.idempotency_key == idempotency_ref,
                        DomainCommandCommit.input_state_version == delta.expected_state_version,
                        DomainCommandCommit.agent_spec_version == context.agent_spec.version,
                    )
                )
                if existing is not None:
                    if existing.delta_digest != digest:
                        raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
                    await self._replay_safety_fact_assertions(
                        session,
                        delta,
                        existing_commit=existing,
                        safety_fact_assertions=safety_fact_assertions,
                    )
                    return self._commit_result(existing)

                if locked.state_version != delta.expected_state_version:
                    raise RepositoryError(RepositoryErrorCode.STATE_VERSION_CONFLICT)

                state = await self._load_state(session, locked)
                next_state = reduce_domain_state(state, delta, context)
                with session.no_autoflush:
                    graph_run = await session.get(GraphRun, delta.run_id)
                if graph_run is None:
                    graph_run = GraphRun(
                        id=delta.run_id,
                        session_id=delta.session_id,
                        graph_version=graph_version,
                        command_id=idempotency_ref,
                        input_state_version=delta.expected_state_version,
                        status="running",
                    )
                    session.add(graph_run)
                    await session.flush([graph_run])
                elif (
                    graph_run.session_id != delta.session_id
                    or graph_run.graph_version != graph_version
                    or graph_run.input_state_version != delta.expected_state_version
                ):
                    raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
                # R9: question contracts reference the asked question message
                # (FK question_message_id -> consult_messages), so the question
                # rows must enter the unit of work *before* _persist_state can
                # flush contract rows in its artifact phase.  Inserting the
                # messages first keeps the explicit-flush FK ordering sound.
                for message in consult_messages:
                    session.add(
                        ConsultMessage(
                            id=message.message_id,
                            session_id=delta.session_id,
                            role=message.role,
                            stage=message.stage,
                            agent_name=message.agent_name,
                            content=message.content,
                            structured_delta=message.structured_delta,
                            trace_id=message.trace_id,
                        )
                    )
                await self._persist_state(session, state, next_state, delta, artifact_payloads=artifact_payloads)
                await self._persist_product_projections(
                    session,
                    delta,
                    safety_rule_runs=safety_rule_runs,
                    doctor_reviews=doctor_reviews,
                    medical_records=medical_records,
                    agent_runs=agent_runs,
                    agent_evidences=agent_evidences,
                    audit_events=audit_events,
                )
                locked.state_version = next_state.state_version
                graph_run.status = "completed"
                graph_run.completed_at = func.now()
                session.add(
                    GraphRunStep(
                        id=uuid4(),
                        graph_run_id=delta.run_id,
                        step_index=0,
                        step_name="domain_commit",
                        status="completed",
                        step_metadata={
                            "state_version": next_state.state_version,
                            "safety_fact_assertion_manifest": _safety_fact_spec_manifest(
                                safety_fact_assertions
                            ),
                        },
                    )
                )
                for index, step in enumerate(graph_steps, start=1):
                    session.add(
                        GraphRunStep(
                            id=uuid4(),
                            graph_run_id=delta.run_id,
                            step_index=index,
                            step_name=step.step_name,
                            status=step.status,
                            step_metadata=step.metadata,
                        )
                    )
                session.add(
                    GateResult(
                        id=uuid4(),
                        session_id=delta.session_id,
                        graph_run_id=delta.run_id,
                        gate_name="canonical_verifier_chain",
                        policy_version="l2-4-v1",
                        input_state_version=delta.expected_state_version,
                        decision="passed",
                        details={"subject_digest": digest},
                    )
                )
                for gate in gate_results:
                    session.add(
                        GateResult(
                            id=uuid4(),
                            session_id=delta.session_id,
                            graph_run_id=delta.run_id,
                            gate_name=gate.gate_name,
                            policy_version=gate.policy_version,
                            input_state_version=gate.input_state_version,
                            decision=gate.decision.value,
                            details=gate.details,
                        )
                    )
                await self._persist_safety_fact_assertions(
                    session,
                    delta,
                    safety_fact_assertions=safety_fact_assertions,
                )
                if session_updates:
                    self._apply_session_updates(locked, session_updates)
                # Explicit flush phases make FK ordering unambiguous without
                # introducing ORM relationships into the persistence contract.
                # All phases remain inside this one transaction.
                await session.flush()

                outbox_id = uuid4()
                session.add(
                    OutboxEvent(
                        id=outbox_id,
                        event_type=outbox_event_type,
                        session_id=delta.session_id,
                        graph_run_id=delta.run_id,
                        state_version=next_state.state_version,
                        trace_id=self._stable_ref("trace", context.run_spec.trace_id),
                        payload=outbox_payload or self._event_payload(delta, next_state),
                        status="pending",
                        attempt_count=0,
                    )
                )
                await session.flush()
                commit_row = DomainCommandCommit(
                    id=uuid4(),
                    session_id=delta.session_id,
                    idempotency_key=idempotency_ref,
                    input_state_version=delta.expected_state_version,
                    agent_spec_version=context.agent_spec.version,
                    delta_digest=digest,
                    output_state_version=next_state.state_version,
                    changed=next_state.state_version != state.state_version,
                    graph_run_id=delta.run_id,
                    outbox_event_id=outbox_id,
                )
                session.add(commit_row)
                await session.flush()
                return self._commit_result(commit_row)
        except (RepositoryError, DomainReducerError):
            raise
        except SQLAlchemyError:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def get_outbox(self, event_id: UUID) -> OutboxMessage | None:
        async with self._session_factory() as session:
            row = await session.get(OutboxEvent, event_id)
            return None if row is None else self._outbox_message(row)

    async def claim(self, *, worker_id: str, limit: int, lease_seconds: int) -> tuple[OutboxMessage, ...]:
        self._validate_worker(worker_id)
        if limit < 1 or lease_seconds < 1:
            raise ValueError("limit and lease_seconds must be positive")
        try:
            async with self._session_factory() as session, session.begin():
                now = func.now()
                rows = (
                    await session.scalars(
                        select(OutboxEvent)
                        .where(
                            or_(
                                and_(OutboxEvent.status == "pending", OutboxEvent.available_at <= now),
                                and_(OutboxEvent.status == "leased", OutboxEvent.leased_until <= now),
                            )
                        )
                        .order_by(OutboxEvent.created_at, OutboxEvent.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for row in rows:
                    row.status = "leased"
                    row.leased_by = worker_id
                    row.leased_until = func.now() + timedelta(seconds=lease_seconds)
                    row.attempt_count += 1
                await session.flush()
                return tuple(self._outbox_message(row) for row in rows)
        except SQLAlchemyError:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def acknowledge(self, event_id: UUID, *, worker_id: str) -> bool:
        self._validate_worker(worker_id)
        try:
            async with self._session_factory() as session, session.begin():
                result = await session.execute(
                    update(OutboxEvent)
                    .where(
                        OutboxEvent.id == event_id,
                        OutboxEvent.status == "leased",
                        OutboxEvent.leased_by == worker_id,
                    )
                    .values(
                        status="published",
                        leased_by=None,
                        leased_until=None,
                        last_error_code=None,
                        published_at=func.now(),
                    )
                    .returning(OutboxEvent.id)
                )
                return result.scalar_one_or_none() is not None
        except SQLAlchemyError:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def release_failed(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        error_code: OutboxErrorCode,
        retry_after_seconds: int,
    ) -> bool:
        self._validate_worker(worker_id)
        if not isinstance(error_code, OutboxErrorCode):
            raise TypeError("error_code must be OutboxErrorCode")
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must not be negative")
        try:
            async with self._session_factory() as session, session.begin():
                result = await session.execute(
                    update(OutboxEvent)
                    .where(
                        OutboxEvent.id == event_id,
                        OutboxEvent.status == "leased",
                        OutboxEvent.leased_by == worker_id,
                    )
                    .values(
                        status="pending",
                        leased_by=None,
                        leased_until=None,
                        last_error_code=error_code.value,
                        available_at=func.now() + timedelta(seconds=retry_after_seconds),
                    )
                    .returning(OutboxEvent.id)
                )
                return result.scalar_one_or_none() is not None
        except SQLAlchemyError:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def dead_letter(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        error_code: OutboxErrorCode,
    ) -> bool:
        """Move a currently owned lease to the durable terminal DLQ state."""
        self._validate_worker(worker_id)
        if not isinstance(error_code, OutboxErrorCode):
            raise TypeError("error_code must be OutboxErrorCode")
        try:
            async with self._session_factory() as session, session.begin():
                result = await session.execute(
                    update(OutboxEvent)
                    .where(
                        OutboxEvent.id == event_id,
                        OutboxEvent.status == "leased",
                        OutboxEvent.leased_by == worker_id,
                    )
                    .values(
                        status="dead_letter",
                        leased_by=None,
                        leased_until=None,
                        last_error_code=error_code.value,
                        dead_lettered_at=func.now(),
                    )
                    .returning(OutboxEvent.id)
                )
                return result.scalar_one_or_none() is not None
        except SQLAlchemyError:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def get_outbox_health(self) -> OutboxHealth:
        """Return aggregate counters only; never return event payloads or identifiers."""
        try:
            async with self._session_factory() as session:
                pending_count = int(
                    await session.scalar(
                        select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == "pending")
                    )
                    or 0
                )
                leased_count = int(
                    await session.scalar(
                        select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == "leased")
                    )
                    or 0
                )
                dead_letter_count = int(
                    await session.scalar(
                        select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == "dead_letter")
                    )
                    or 0
                )
                oldest = await session.scalar(
                    select(func.min(OutboxEvent.created_at)).where(
                        OutboxEvent.status.in_(("pending", "leased"))
                    )
                )
            age = 0.0
            if oldest is not None:
                if oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=UTC)
                age = max(0.0, (datetime.now(UTC) - oldest).total_seconds())
            return OutboxHealth(
                backlog_count=pending_count + leased_count,
                pending_count=pending_count,
                leased_count=leased_count,
                dead_letter_count=dead_letter_count,
                oldest_unpublished_age_seconds=age,
            )
        except SQLAlchemyError:
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None

    async def _load_state(self, session: AsyncSession, session_row: ConsultSession) -> DomainState:
        observations = (
            await session.scalars(
                select(Observation)
                .where(Observation.session_id == session_row.id)
                .order_by(Observation.created_at, Observation.id)
            )
        ).all()
        safety = await session.scalar(select(SafetyProfile).where(SafetyProfile.session_id == session_row.id))
        artifacts = (
            await session.scalars(
                select(ArtifactRevision)
                .where(ArtifactRevision.session_id == session_row.id)
                .order_by(ArtifactRevision.artifact_id, ArtifactRevision.revision)
            )
        ).all()
        contracts = (
            await session.scalars(
                select(QuestionContractRecord)
                .where(QuestionContractRecord.session_id == session_row.id)
                .order_by(QuestionContractRecord.root_contract_id, QuestionContractRecord.revision)
            )
        ).all()
        events = (
            await session.scalars(
                select(QuestionCoverageEventRecord)
                .where(QuestionCoverageEventRecord.session_id == session_row.id)
                .order_by(
                    QuestionCoverageEventRecord.root_contract_id,
                    QuestionCoverageEventRecord.created_at,
                    QuestionCoverageEventRecord.id,
                )
            )
        ).all()
        try:
            contract_schemas = tuple(_contract_schema(row) for row in contracts)
            event_schemas = tuple(_coverage_event_schema(row) for row in events)
        except (ValueError, TypeError):
            raise RepositoryError(RepositoryErrorCode.TRANSACTION_FAILED) from None
        return DomainState(
            session_id=session_row.id,
            state_version=session_row.state_version,
            observations=tuple(self._observation_schema(row) for row in observations),
            safety_profile=None if safety is None else self._safety_schema(safety),
            artifacts=tuple(self._artifact_schema(row) for row in artifacts),
            question_contracts=contract_schemas,
            question_coverage_events=event_schemas,
        )

    async def _persist_state(
        self,
        session: AsyncSession,
        previous: DomainState,
        current: DomainState,
        delta: DomainDelta,
        *,
        artifact_payloads: Sequence[ArtifactPayloadSpec],
    ) -> None:
        previous_observations = {item.observation_id for item in previous.observations}
        for observation_item in current.observations:
            if observation_item.observation_id not in previous_observations:
                session.add(
                    Observation(
                        id=observation_item.observation_id,
                        session_id=observation_item.session_id,
                        fact_key=observation_item.fact_key,
                        value=observation_item.value,
                        normalized_value=observation_item.normalized_value,
                        source_message_id=observation_item.source_message_id,
                        status=observation_item.status.value,
                        confidence=observation_item.confidence,
                        supersedes_observation_id=observation_item.supersedes_observation_id,
                        created_at=observation_item.created_at,
                    )
                )

        previous_contract_ids = {item.contract_id for item in previous.question_contracts}
        for contract_item in current.question_contracts:
            if contract_item.contract_id not in previous_contract_ids:
                session.add(QuestionContractRecord(**_contract_record_values(contract_item)))
        previous_event_ids = {item.event_id for item in previous.question_coverage_events}
        for event_item in current.question_coverage_events:
            if event_item.event_id not in previous_event_ids:
                session.add(QuestionCoverageEventRecord(**_coverage_event_record_values(event_item)))

        if delta.safety_profile is not None:
            safety_row = await session.scalar(
                select(SafetyProfile).where(SafetyProfile.session_id == current.session_id)
            )
            values = self._safety_values(delta.safety_profile)
            if safety_row is None:
                session.add(SafetyProfile(id=uuid4(), session_id=current.session_id, **values))
            else:
                for name, value in values.items():
                    setattr(safety_row, name, value)

        rows = (
            await session.scalars(select(ArtifactRevision).where(ArtifactRevision.session_id == current.session_id))
        ).all()
        by_key = {(artifact_row.artifact_id, artifact_row.revision): artifact_row for artifact_row in rows}
        next_by_key = {
            (artifact_item.artifact_id, artifact_item.revision): artifact_item for artifact_item in current.artifacts
        }
        for key, artifact_row in by_key.items():
            artifact_row.status = next_by_key[key].status.value
        if by_key:
            await session.flush()

        incoming_keys = {
            (artifact_item.artifact_id, artifact_item.revision) for artifact_item in delta.artifact_revisions
        }
        payload_by_key = {
            (payload.artifact_id, payload.revision): payload for payload in artifact_payloads
        }
        if set(payload_by_key) - incoming_keys:
            raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
        for artifact_item in current.artifacts:
            key = (artifact_item.artifact_id, artifact_item.revision)
            if key not in incoming_keys or key in by_key:
                continue
            if artifact_item.revision > 1:
                parent = by_key.get((artifact_item.artifact_id, artifact_item.revision - 1))
                if (
                    parent is None
                    or artifact_item.parent_revision_id != parent.id
                    or parent.session_id != artifact_item.session_id
                ):
                    raise RepositoryError(RepositoryErrorCode.ARTIFACT_PARENT_INVALID)
            revision_row = ArtifactRevision(
                id=uuid4(),
                artifact_id=artifact_item.artifact_id,
                artifact_type=artifact_item.artifact_type,
                revision=artifact_item.revision,
                session_id=artifact_item.session_id,
                input_state_version=artifact_item.input_state_version,
                status=artifact_item.status.value,
                produced_by_run_id=artifact_item.produced_by_run_id,
                parent_revision_id=artifact_item.parent_revision_id,
                parent_revision=artifact_item.parent_revision,
                created_at=artifact_item.created_at,
            )
            session.add(revision_row)
            payload = payload_by_key.get(key)
            if payload is not None:
                if payload.session_id != artifact_item.session_id:
                    raise RepositoryError(RepositoryErrorCode.ARTIFACT_PAYLOAD_INVALID)
                session.add(
                    ArtifactRevisionPayload(
                        id=uuid4(),
                        artifact_revision_id=revision_row.id,
                        session_id=payload.session_id,
                        artifact_id=payload.artifact_id,
                        revision=payload.revision,
                        payload_schema_version=payload.payload_schema_version,
                        payload=dict(payload.payload),
                        content_digest=payload.content_digest,
                    )
                )

    @classmethod
    async def _replay_safety_fact_assertions(
        cls,
        session: AsyncSession,
        delta: DomainDelta,
        *,
        existing_commit: DomainCommandCommit,
        safety_fact_assertions: Sequence[SafetyFactAssertionSpec],
    ) -> None:
        """Seal legacy split commits once, then require an exact proposal manifest."""

        cls._validate_safety_fact_spec_scope(delta, safety_fact_assertions)
        source_message_ids = tuple(delta.source_message_ids)
        existing_rows = (
            await session.scalars(
                select(SafetyFactAssertion).where(
                    SafetyFactAssertion.session_id == delta.session_id,
                    SafetyFactAssertion.source_message_id.in_(source_message_ids),
                    SafetyFactAssertion.source_kind.in_(_INTAKE_SAFETY_SOURCE_KINDS),
                )
            )
        ).all()
        existing_manifest = _safety_fact_manifest(
            [
                _safety_fact_manifest_entry(
                    assertion_id=row.id,
                    assertion_fingerprint=row.assertion_fingerprint,
                    value_digest=row.value_digest,
                    evidence_digest=row.evidence_digest,
                    field_name=row.field_name,
                    source_kind=row.source_kind,
                    source_message_id=row.source_message_id,
                    extraction_run_id=row.extraction_run_id,
                    template_version=row.template_version,
                )
                for row in existing_rows
            ]
        )
        requested_manifest = _safety_fact_spec_manifest(safety_fact_assertions)
        domain_step = await session.scalar(
            select(GraphRunStep).where(
                GraphRunStep.graph_run_id == existing_commit.graph_run_id,
                GraphRunStep.step_index == 0,
                GraphRunStep.step_name == "domain_commit",
            )
        )
        if domain_step is None:
            raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
        metadata = dict(domain_step.step_metadata or {})
        sealed_manifest = metadata.get("safety_fact_assertion_manifest")
        if sealed_manifest is not None:
            if sealed_manifest != requested_manifest or existing_manifest != requested_manifest:
                raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
        elif existing_rows and existing_manifest != requested_manifest:
            # A historical split commit may be sealed only with the exact set
            # already persisted by its former post-commit safety transaction.
            raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)

        # With no sealed manifest and no rows this is the one compatibility
        # path for a historical Domain commit whose split safety write never
        # happened.  The transaction below fills (or seals empty) exactly once.
        await cls._persist_safety_fact_assertions(
            session,
            delta,
            safety_fact_assertions=safety_fact_assertions,
        )
        metadata["safety_fact_assertion_manifest"] = requested_manifest
        domain_step.step_metadata = metadata
        await session.flush()

    @staticmethod
    def _validate_safety_fact_spec_scope(
        delta: DomainDelta,
        safety_fact_assertions: Sequence[SafetyFactAssertionSpec],
    ) -> None:
        allowed_source_message_ids = frozenset(delta.source_message_ids)
        if (
            any(item.session_id != delta.session_id for item in safety_fact_assertions)
            or any(
                item.source_message_id not in allowed_source_message_ids
                for item in safety_fact_assertions
            )
        ):
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)

    @staticmethod
    async def _validate_safety_fact_provenance(
        session: AsyncSession,
        item: SafetyFactAssertionSpec,
        source: ConsultMessage,
    ) -> None:
        """Recheck reply provenance at the database trust boundary."""

        bindings = tuple(
            (evidence.reply_to_question_message_id, evidence.reply_dimension)
            for evidence in item.evidence_spans
        )
        if item.source_kind != "deterministic_reply_binding":
            if any(question_id is not None or dimension is not None for question_id, dimension in bindings):
                raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
            if item.source_kind == "deterministic_precheck" and item.field_name != "red_flag":
                raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
            return

        expected_dimension = _SAFETY_REPLY_DIMENSION_BY_FIELD.get(item.field_name)
        question_ids = {question_id for question_id, _ in bindings if question_id is not None}
        dimensions = {dimension for _, dimension in bindings if dimension is not None}
        if (
            expected_dimension is None
            or len(bindings) == 0
            or any(
                question_id is None or dimension is None
                for question_id, dimension in bindings
            )
            or len(question_ids) != 1
            or dimensions != {expected_dimension}
            or item.value.get("collection_status") != "explicitly_none"
            or _EXPLICIT_NONE_REPLY.fullmatch(source.content) is None
            or len(item.evidence_spans) != 1
            or item.evidence_spans[0].start_char != 0
            or item.evidence_spans[0].end_char != len(source.content)
        ):
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)

        structured = source.structured_delta if isinstance(source.structured_delta, dict) else {}
        raw_context = structured.get("reply_context")
        question_id = next(iter(question_ids))
        if (
            source.role not in {"doctor", "patient_proxy"}
            or source.stage != "inquiry"
            or structured.get("binding_version") != _INTAKE_REPLY_BINDING_VERSION
            or not isinstance(raw_context, dict)
            or str(raw_context.get("question_message_id")) != str(question_id)
            or raw_context.get("selected_dimension") != expected_dimension
        ):
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)

        question = await session.get(ConsultMessage, question_id)
        question_structured = question.structured_delta if question is not None else None
        if (
            question is None
            or question.session_id != item.session_id
            or question.role != "agent"
            or question.stage != "inquiry"
            or question.agent_name != "question_composer"
            or not isinstance(question_structured, dict)
            or question_structured.get("selected_dimension") != expected_dimension
        ):
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)

    @classmethod
    async def _persist_safety_fact_assertions(
        cls,
        session: AsyncSession,
        delta: DomainDelta,
        *,
        safety_fact_assertions: Sequence[SafetyFactAssertionSpec],
    ) -> None:
        """Persist safety proposals and their audits in the Domain transaction.

        Batch-loads source messages, assertion rows, and audit rows before
        iterating to avoid N+1 SELECT patterns (was 4+ session.get() calls per
        assertion item, now 4 total queries regardless of item count).
        """

        assertion_ids = {item.assertion_id for item in safety_fact_assertions}
        audit_ids = {item.audit_event_id for item in safety_fact_assertions}
        if len(assertion_ids) != len(safety_fact_assertions) or len(audit_ids) != len(safety_fact_assertions):
            raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
        cls._validate_safety_fact_spec_scope(delta, safety_fact_assertions)

        # ---- batch 1: load all source ConsultMessages ----
        source_message_ids = {item.source_message_id for item in safety_fact_assertions}
        source_messages: dict[UUID, ConsultMessage] = {}
        if source_message_ids:
            for row in (
                await session.scalars(
                    select(ConsultMessage).where(ConsultMessage.id.in_(source_message_ids))
                )
            ).all():
                source_messages[row.id] = row

        # ---- batch 2: find all referenced reply question IDs ----
        reply_ids: set[UUID] = set()
        for item in safety_fact_assertions:
            for evidence in item.evidence_spans:
                if evidence.reply_to_question_message_id is not None:
                    reply_ids.add(evidence.reply_to_question_message_id)
        reply_messages: dict[UUID, ConsultMessage] = {}
        if reply_ids:
            for row in (
                await session.scalars(
                    select(ConsultMessage).where(ConsultMessage.id.in_(reply_ids))
                )
            ).all():
                reply_messages[row.id] = row

        # ---- batch 3: load existing SafetyFactAssertion rows ----
        existing_assertions: dict[UUID, SafetyFactAssertion] = {}
        if assertion_ids:
            for assertion_row in (
                await session.scalars(
                    select(SafetyFactAssertion).where(SafetyFactAssertion.id.in_(assertion_ids))
                )
            ).all():
                existing_assertions[assertion_row.id] = assertion_row

        # ---- batch 4: load existing AuditEvent rows ----
        existing_audits: dict[UUID, AuditEvent] = {}
        if audit_ids:
            for audit_row in (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.id.in_(audit_ids))
                )
            ).all():
                existing_audits[audit_row.id] = audit_row

        for item in safety_fact_assertions:
            source = source_messages.get(item.source_message_id)
            if source is None or source.session_id != delta.session_id:
                raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
            await cls._validate_safety_fact_provenance(session, item, source)
            evidence_payload = [
                evidence.model_dump(mode="json", exclude_none=True)
                for evidence in item.evidence_spans
            ]
            for evidence in item.evidence_spans:
                if evidence.end_char > len(source.content):
                    raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
                quote = source.content[evidence.start_char : evidence.end_char]
                if hashlib.sha256(quote.encode("utf-8")).hexdigest() != evidence.quote_digest:
                    raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
                if evidence.reply_to_question_message_id is not None:
                    question = reply_messages.get(evidence.reply_to_question_message_id)
                    structured = question.structured_delta if question is not None else None
                    if (
                        question is None
                        or question.session_id != delta.session_id
                        or question.role != "agent"
                        or question.stage != "inquiry"
                        or question.agent_name != "question_composer"
                        or not isinstance(structured, dict)
                        or structured.get("selected_dimension") != evidence.reply_dimension
                    ):
                        raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)

            assertion_values: dict[str, object] = {
                "session_id": item.session_id,
                "field_name": item.field_name,
                "value": item.value,
                "value_digest": item.value_digest,
                "assertion_fingerprint": item.assertion_fingerprint,
                "source_kind": item.source_kind,
                "source_message_id": item.source_message_id,
                "extraction_run_id": item.extraction_run_id,
                "template_version": item.template_version,
                "evidence_spans": evidence_payload,
                "evidence_digest": item.evidence_digest,
                "proposed_by_actor_type": item.proposed_by_actor_type,
                "proposed_by_actor_id": item.proposed_by_actor_id,
            }
            existing_assertion = existing_assertions.get(item.assertion_id)
            if existing_assertion is None:
                session.add(
                    SafetyFactAssertion(
                        id=item.assertion_id,
                        status=item.status,
                        **assertion_values,
                    )
                )
            elif any(
                getattr(existing_assertion, name) != value
                for name, value in assertion_values.items()
            ):
                raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)

            audit_payload: dict[str, object] = {
                "assertion_id": str(item.assertion_id),
                "field_name": item.field_name,
                "status": item.status,
                "value_digest": item.value_digest,
                "evidence_digest": item.evidence_digest,
                "source_message_id": str(item.source_message_id),
                "extraction_run_id": str(item.extraction_run_id),
                "template_version": item.template_version,
            }
            audit_values: dict[str, object] = {
                "session_id": item.session_id,
                "event_type": item.audit_event_type,
                "actor_type": item.audit_actor_type,
                "actor_id": item.audit_actor_id,
                "payload": audit_payload,
                "trace_id": item.audit_trace_id,
            }
            existing_audit = existing_audits.get(item.audit_event_id)
            if existing_audit is None:
                session.add(AuditEvent(id=item.audit_event_id, **audit_values))
            elif any(getattr(existing_audit, name) != value for name, value in audit_values.items()):
                raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)

    @staticmethod
    async def _persist_product_projections(
        session: AsyncSession,
        delta: DomainDelta,
        *,
        safety_rule_runs: Sequence[SafetyRuleRunSpec],
        doctor_reviews: Sequence[DoctorReviewSpec],
        medical_records: Sequence[MedicalRecordSpec],
        agent_runs: Sequence[AgentRunSpec],
        agent_evidences: Sequence[AgentEvidenceSpec],
        audit_events: Sequence[AuditEventSpec],
    ) -> None:
        """Persist compatibility projections inside the Domain transaction."""

        safety_ids = {item.safety_rule_run_id for item in safety_rule_runs}
        if len(safety_ids) != len(safety_rule_runs):
            raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
        review_ids = {item.review_id for item in doctor_reviews}
        if len(review_ids) != len(doctor_reviews):
            raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
        record_ids = {item.record_id for item in medical_records}
        if len(record_ids) != len(medical_records):
            raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
        if (
            any(safety_item.session_id != delta.session_id for safety_item in safety_rule_runs)
            or any(review_item.session_id != delta.session_id for review_item in doctor_reviews)
            or any(record_item.session_id != delta.session_id for record_item in medical_records)
            or any(audit_item.session_id != delta.session_id for audit_item in audit_events)
        ):
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)

        # 批量预加载所有实体，避免在循环中逐条 session.get()
        safety_keys = {item.safety_rule_run_id for item in safety_rule_runs}
        existing_safety_map: dict[UUID, SafetyRuleRun] = {}
        if safety_keys:
            for row in (
                await session.scalars(
                    select(SafetyRuleRun).where(SafetyRuleRun.id.in_(safety_keys))
                )
            ).all():
                existing_safety_map[row.id] = row

        for safety_item in safety_rule_runs:
            existing_safety = existing_safety_map.get(safety_item.safety_rule_run_id)
            safety_values: dict[str, object] = {
                "session_id": safety_item.session_id,
                "agent_run_id": safety_item.agent_run_id,
                "formula_source": safety_item.formula_source,
                "passed": safety_item.passed,
                "issues": safety_item.issues,
                "formula_snapshot": safety_item.formula_snapshot,
                "normalized_formula": safety_item.normalized_formula,
                "patient_snapshot": safety_item.patient_snapshot,
                "rule_version": safety_item.rule_version,
                "trace_id": safety_item.trace_id,
            }
            if existing_safety is None:
                session.add(SafetyRuleRun(id=safety_item.safety_rule_run_id, **safety_values))
                continue
            if any(
                getattr(existing_safety, name) != value
                for name, value in safety_values.items()
            ):
                raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)

        event_ids = {item.event_id for item in audit_events}
        if len(event_ids) != len(audit_events):
            raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
        existing_audit_map: dict[UUID, AuditEvent] = {}
        if event_ids:
            for existing_audit_row in (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.id.in_(event_ids))
                )
            ).all():
                existing_audit_map[existing_audit_row.id] = existing_audit_row

        for audit_item in audit_events:
            existing_audit = existing_audit_map.get(audit_item.event_id)
            audit_values: dict[str, object] = {
                "session_id": audit_item.session_id,
                "event_type": audit_item.event_type,
                "actor_type": audit_item.actor_type,
                "actor_id": audit_item.actor_id,
                "payload": audit_item.payload,
                "trace_id": audit_item.trace_id,
            }
            if existing_audit is None:
                session.add(AuditEvent(id=audit_item.event_id, **audit_values))
                continue
            if any(
                getattr(existing_audit, name) != value
                for name, value in audit_values.items()
            ):
                raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)

        review_ids_pending = {item.review_id for item in doctor_reviews}
        existing_review_map: dict[UUID, DoctorReview] = {}
        if review_ids_pending:
            for review_row in (
                await session.scalars(
                    select(DoctorReview).where(DoctorReview.id.in_(review_ids_pending))
                )
            ).all():
                existing_review_map[review_row.id] = review_row

        for review_item in doctor_reviews:
            if (
                review_item.safety_rule_run_id not in safety_ids
                and review_item.safety_rule_run_id not in existing_safety_map
            ):
                # 不在本次批次内 → 单独查并缓存
                safety_exists = await session.get(SafetyRuleRun, review_item.safety_rule_run_id)
                if safety_exists is None or safety_exists.session_id != delta.session_id:
                    raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
                existing_safety_map[review_item.safety_rule_run_id] = safety_exists
            existing_review = existing_review_map.get(review_item.review_id)
            review_values: dict[str, object] = {
                "session_id": review_item.session_id,
                "agent_run_id": review_item.agent_run_id,
                "safety_rule_run_id": review_item.safety_rule_run_id,
                "action": review_item.action,
                "original_formula": review_item.original_formula,
                "formula_override": review_item.formula_override,
                "feedback": review_item.feedback,
                "reviewed_by": review_item.reviewed_by,
            }
            if existing_review is None:
                session.add(DoctorReview(id=review_item.review_id, **review_values))
                continue
            if any(
                getattr(existing_review, name) != value
                for name, value in review_values.items()
            ):
                raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)

        record_ids_pending = {item.record_id for item in medical_records}
        existing_record_map: dict[UUID, MedicalRecord] = {}
        if record_ids_pending:
            for record_row in (
                await session.scalars(
                    select(MedicalRecord).where(MedicalRecord.id.in_(record_ids_pending))
                )
            ).all():
                existing_record_map[record_row.id] = record_row
        # 预加载 medical_records 中引用的 DoctorReview（用于校验）
        review_ref_ids = {item.doctor_review_id for item in medical_records} - review_ids_pending
        if review_ref_ids:
            for review_ref_row in (
                await session.scalars(
                    select(DoctorReview).where(DoctorReview.id.in_(review_ref_ids))
                )
            ).all():
                existing_review_map[review_ref_row.id] = review_ref_row

        for record_item in medical_records:
            review = existing_review_map.get(record_item.doctor_review_id)
            if (
                review is None
                or review.session_id != delta.session_id
                or review.action not in {"confirm", "modify"}
            ):
                raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
            version_owner = await session.scalar(
                select(MedicalRecord).where(
                    MedicalRecord.session_id == record_item.session_id,
                    MedicalRecord.version == record_item.version,
                )
            )
            if version_owner is not None and version_owner.id != record_item.record_id:
                raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
            existing_record = existing_record_map.get(record_item.record_id)
            record_values: dict[str, object] = {
                "session_id": record_item.session_id,
                "version": record_item.version,
                "record_text": record_item.record_text,
                "record_json": record_item.record_json,
                "diff_from_previous": record_item.diff_from_previous,
                "doctor_review_id": record_item.doctor_review_id,
                "disclaimer": record_item.disclaimer,
                "edited_by_doctor": record_item.edited_by_doctor,
            }
            if existing_record is None:
                session.add(MedicalRecord(id=record_item.record_id, **record_values))
                continue
            if any(
                getattr(existing_record, name) != value
                for name, value in record_values.items()
            ):
                raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)

        run_ids_pending = {item.run_id for item in agent_runs}
        if len(run_ids_pending) != len(agent_runs):
            raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
        if any(run_item.session_id != delta.session_id for run_item in agent_runs):
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
        existing_run_map: dict[UUID, AgentRun] = {}
        if run_ids_pending:
            for run_row in (
                await session.scalars(
                    select(AgentRun).where(AgentRun.id.in_(run_ids_pending))
                )
            ).all():
                existing_run_map[run_row.id] = run_row

        for run_item in agent_runs:
            existing_run = existing_run_map.get(run_item.run_id)
            run_values: dict[str, object] = {
                "session_id": run_item.session_id,
                "agent_name": run_item.agent_name,
                "stage": run_item.stage,
                "input_snapshot": run_item.input_snapshot,
                "output_snapshot": run_item.output_snapshot,
                "prompt_version": run_item.prompt_version,
                "model": run_item.model,
                "retry_count": run_item.retry_count,
                "status": run_item.status,
                "error_code": run_item.error_code,
                "latency_ms": run_item.latency_ms,
                "trace_id": run_item.trace_id,
            }
            if existing_run is None:
                session.add(AgentRun(id=run_item.run_id, **run_values))
                continue
            if any(
                getattr(existing_run, name) != value
                for name, value in run_values.items()
            ):
                raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)

        evidence_row_ids = {item.evidence_row_id for item in agent_evidences}
        if len(evidence_row_ids) != len(agent_evidences):
            raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)
        if any(
            evidence_item.session_id != delta.session_id
            or evidence_item.agent_run_id not in run_ids_pending
            for evidence_item in agent_evidences
        ):
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
        existing_evidence_map: dict[UUID, AgentEvidence] = {}
        if evidence_row_ids:
            for evidence_row in (
                await session.scalars(
                    select(AgentEvidence).where(AgentEvidence.id.in_(evidence_row_ids))
                )
            ).all():
                existing_evidence_map[evidence_row.id] = evidence_row

        for evidence_item in agent_evidences:
            existing_evidence = existing_evidence_map.get(evidence_item.evidence_row_id)
            evidence_values: dict[str, object] = {
                "agent_run_id": evidence_item.agent_run_id,
                "session_id": evidence_item.session_id,
                "evidence_id": evidence_item.evidence_id,
                "source_type": evidence_item.source_type,
                "source_id": evidence_item.source_id,
                "chunk_id": evidence_item.chunk_id,
                "title": evidence_item.title,
                "content_snippet": evidence_item.content_snippet,
                "score": evidence_item.score,
                "rank": evidence_item.rank,
            }
            if existing_evidence is None:
                session.add(AgentEvidence(id=evidence_item.evidence_row_id, **evidence_values))
                continue
            if any(
                getattr(existing_evidence, name) != value
                for name, value in evidence_values.items()
            ):
                raise RepositoryError(RepositoryErrorCode.IDEMPOTENCY_KEY_REUSED)

    @staticmethod
    def _observation_schema(row: Observation) -> ObservationSchema:
        return ObservationSchema.model_validate(
            {
                "observation_id": row.id,
                "session_id": row.session_id,
                "fact_key": row.fact_key,
                "value": row.value,
                "normalized_value": row.normalized_value,
                "source_message_id": row.source_message_id,
                "status": row.status,
                "confidence": row.confidence,
                "supersedes_observation_id": row.supersedes_observation_id,
                "created_at": row.created_at,
            }
        )

    @staticmethod
    async def _gate_rows(session: AsyncSession, session_id: UUID, state_version: int) -> list[GateResult]:
        return list(
            (
                await session.scalars(
                    select(GateResult)
                    .join(GraphRun, GateResult.graph_run_id == GraphRun.id)
                    .where(
                        GateResult.session_id == session_id,
                        GateResult.input_state_version == state_version,
                        GateResult.gate_name.in_(("triage", "completeness")),
                        GraphRun.session_id == session_id,
                        GraphRun.status == "completed",
                    )
                    .order_by(GateResult.created_at, GateResult.id)
                )
            ).all()
        )

    @staticmethod
    async def _source_gate_rows(
        session: AsyncSession,
        *,
        session_id: UUID,
        source_state_version: int,
        graph_run_id: UUID | None,
    ) -> list[GateResult]:
        if graph_run_id is None:
            return []
        return list(
            (
                await session.scalars(
                    select(GateResult)
                    .join(GraphRun, GateResult.graph_run_id == GraphRun.id)
                    .where(
                        GateResult.session_id == session_id,
                        GateResult.input_state_version == source_state_version,
                        GateResult.graph_run_id == graph_run_id,
                        GateResult.gate_name.in_(("triage", "completeness")),
                        GraphRun.session_id == session_id,
                        GraphRun.status == "completed",
                    )
                    .order_by(GateResult.created_at, GateResult.id)
                )
            ).all()
        )

    @staticmethod
    def _advance_source(snapshot: dict[str, Any] | None, current_state_version: int) -> tuple[UUID, int] | None:
        if not isinstance(snapshot, dict):
            return None
        advance = snapshot.get("advance")
        if not isinstance(advance, dict):
            return None
        raw_gate_id = advance.get("source_gate_id")
        raw_state_version = advance.get("source_gate_state_version")
        if not isinstance(raw_gate_id, str) or not isinstance(raw_state_version, int) or isinstance(raw_state_version, bool):
            return None
        try:
            source_gate_id = UUID(raw_gate_id)
        except (TypeError, ValueError):
            return None
        source_state_version = raw_state_version
        if source_state_version < 1 or source_state_version >= current_state_version:
            return None
        return source_gate_id, source_state_version

    @staticmethod
    def _ordered_gate_rows(rows: Sequence[GateResult]) -> tuple[GateResult, ...]:
        priority = {"triage": 0, "completeness": 1}
        return tuple(sorted(rows, key=lambda row: (priority.get(row.gate_name, 99), row.created_at, str(row.id))))

    @classmethod
    def _authority_gate_rows(cls, rows: Sequence[GateResult]) -> tuple[GateResult, GateResult, UUID] | None:
        ordered = cls._ordered_gate_rows(rows)
        if len(ordered) != 2 or ordered[0].gate_name != "triage" or ordered[1].gate_name != "completeness":
            return None
        graph_run_ids = {row.graph_run_id for row in ordered}
        if len(graph_run_ids) != 1:
            return None
        graph_run_id = next(iter(graph_run_ids))
        if graph_run_id is None:
            return None
        return ordered[0], ordered[1], graph_run_id

    @staticmethod
    def _completion_gate_is_ready(row: GateResult) -> bool:
        details = row.details
        return (
            row.gate_name == "completeness"
            and row.policy_version == "completeness-policy.v1"
            and row.decision == "passed"
            and isinstance(details, dict)
            and details.get("disposition") == "ready"
        )

    @staticmethod
    def _triage_gate_is_continue(row: GateResult) -> bool:
        details = row.details
        return (
            row.gate_name == "triage"
            and row.policy_version == "triage-red-flag.v1"
            and row.decision == "passed"
            and isinstance(details, dict)
            and details.get("disposition") == "continue"
            and details.get("candidate_count") == 0
        )

    @staticmethod
    def _gate_schema(row: GateResult) -> GateResultSchema:
        return GateResultSchema.model_validate(
            {
                "gate_name": row.gate_name,
                "policy_version": row.policy_version,
                "input_state_version": row.input_state_version,
                "decision": row.decision,
                "details": row.details,
            }
        )

    @staticmethod
    def _safety_schema(row: SafetyProfile) -> SafetyProfileSchema:
        return SafetyProfileSchema.model_validate(
            {"session_id": row.session_id, **PostgresDomainRepository._safety_values(row)}
        )

    @staticmethod
    def _safety_values(profile: SafetyProfileSchema | SafetyProfile) -> dict[str, object]:
        names = (
            "allergy_collection_status",
            "allergens",
            "pregnancy_collection_status",
            "pregnancy_value",
            "lactation_collection_status",
            "lactation_value",
            "medications_collection_status",
            "medications",
            "major_conditions_collection_status",
            "major_conditions",
            "contraindications_collection_status",
            "contraindications",
        )
        return {
            name: value.value if isinstance((value := getattr(profile, name)), StrEnum) else value for name in names
        }

    @staticmethod
    def _artifact_schema(row: ArtifactRevision) -> ArtifactRevisionSchema:
        return ArtifactRevisionSchema.model_validate(
            {
                "artifact_id": row.artifact_id,
                "artifact_type": row.artifact_type,
                "revision": row.revision,
                "session_id": row.session_id,
                "input_state_version": row.input_state_version,
                "status": row.status,
                "produced_by_run_id": row.produced_by_run_id,
                "parent_revision_id": row.parent_revision_id,
                "parent_revision": row.parent_revision,
                "created_at": row.created_at,
            }
        )

    @staticmethod
    def _event_payload(delta: DomainDelta, state: DomainState) -> dict[str, object]:
        return {
            "session_id": str(delta.session_id),
            "input_state_version": delta.expected_state_version,
            "output_state_version": state.state_version,
            "observation_ids": [str(item.observation_id) for item in delta.observations],
            "artifact_ids": sorted({str(item.artifact_id) for item in delta.artifact_revisions}),
        }

    @staticmethod
    def _apply_session_updates(session_row: ConsultSession, updates: dict[str, object]) -> None:
        allowed = {
            "current_stage",
            "status",
            "pending_review",
            "recovery_status",
            "blocked_reason",
            "blocked_at",
            "state_snapshot",
        }
        if set(updates) - allowed:
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
        for name, value in updates.items():
            setattr(session_row, name, value)

    @staticmethod
    def _commit_result(row: DomainCommandCommit) -> CommitResult:
        return CommitResult(
            commit_id=row.id,
            session_id=row.session_id,
            graph_run_id=row.graph_run_id,
            outbox_event_id=row.outbox_event_id,
            input_state_version=row.input_state_version,
            output_state_version=row.output_state_version,
            changed=row.changed,
        )

    @staticmethod
    def _outbox_message(row: OutboxEvent) -> OutboxMessage:
        return OutboxMessage(
            event_id=row.id,
            event_type=row.event_type,
            session_id=row.session_id,
            graph_run_id=row.graph_run_id,
            state_version=row.state_version,
            trace_id=row.trace_id,
            payload=dict(row.payload),
            status=row.status,
            attempt_count=row.attempt_count,
            leased_by=row.leased_by,
        )

    @staticmethod
    def _validate_metadata(context: VerificationContext, graph_version: str) -> None:
        refs: Sequence[tuple[str, int]] = (
            (graph_version, 64),
            (context.agent_spec.version, 100),
        )
        if any(len(value) > maximum or _SAFE_REF.fullmatch(value) is None for value, maximum in refs):
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)

    @staticmethod
    def _stable_ref(kind: str, value: str) -> str:
        return f"{kind}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _validate_worker(worker_id: str) -> None:
        if len(worker_id) > 128 or _SAFE_REF.fullmatch(worker_id) is None:
            raise RepositoryError(RepositoryErrorCode.UNSAFE_METADATA)
