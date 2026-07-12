"""L2 domain-state persistence models.

These tables are the durable clinical-fact ledger.  They deliberately do not
store prompts, raw model responses, or an authoritative safety conclusion.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.consult import ConsultMessage


class Observation(Base, UUIDPrimaryKeyMixin):
    """A sourced fact and, where applicable, its correction/retraction link."""

    __tablename__ = "observations"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    fact_key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    normalized_value: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    source_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_messages.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    confidence: Mapped[float | None] = mapped_column(DOUBLE_PRECISION, nullable=True)
    supersedes_observation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("observations.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    source_message: Mapped[ConsultMessage] = relationship("ConsultMessage")
    supersedes: Mapped[Observation | None] = relationship("Observation", remote_side="Observation.id")

    __table_args__ = (
        CheckConstraint("status IN ('active','corrected','retracted')", name="chk_observations_status"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="chk_observations_confidence_range"
        ),
        CheckConstraint(
            "(status = 'active' AND supersedes_observation_id IS NULL) OR (status IN ('corrected','retracted') AND supersedes_observation_id IS NOT NULL)",
            name="chk_observations_status_relation",
        ),
        CheckConstraint(
            "supersedes_observation_id IS NULL OR supersedes_observation_id <> id",
            name="chk_observations_no_self_supersede",
        ),
        Index("idx_observations_session_fact_created", "session_id", "fact_key", "created_at"),
        Index("idx_observations_source_message", "source_message_id"),
    )


class SafetyProfile(Base, UUIDPrimaryKeyMixin):
    """Collected safety facts, with unknown distinct from explicit absence."""

    __tablename__ = "safety_profiles"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    allergy_collection_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    allergens: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    pregnancy_collection_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    pregnancy_value: Mapped[str | None] = mapped_column(String(16), nullable=True)
    lactation_collection_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    lactation_value: Mapped[str | None] = mapped_column(String(16), nullable=True)
    medications_collection_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    medications: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    major_conditions_collection_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    major_conditions: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    contraindications_collection_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    contraindications: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), server_onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        *(
            CheckConstraint(
                f"{field} IN ('unknown','explicitly_none','collected')", name=f"chk_safety_profiles_{field}"
            )
            for field in (
                "allergy_collection_status",
                "pregnancy_collection_status",
                "lactation_collection_status",
                "medications_collection_status",
                "major_conditions_collection_status",
                "contraindications_collection_status",
            )
        ),
        CheckConstraint(
            "pregnancy_value IS NULL OR pregnancy_value IN ('pregnant','not_pregnant','possible')",
            name="chk_safety_profiles_pregnancy_value",
        ),
        CheckConstraint(
            "lactation_value IS NULL OR lactation_value IN ('lactating','not_lactating')",
            name="chk_safety_profiles_lactation_value",
        ),
        *(
            CheckConstraint(
                f"({status} = 'unknown' AND {value} IS NULL) OR ({status} = 'explicitly_none' AND {value} IS NULL) OR ({status} = 'collected' AND jsonb_typeof({value}) = 'array' AND jsonb_array_length({value}) > 0)",
                name=f"chk_safety_profiles_{value}_collection",
            )
            for status, value in (
                ("allergy_collection_status", "allergens"),
                ("medications_collection_status", "medications"),
                ("major_conditions_collection_status", "major_conditions"),
                ("contraindications_collection_status", "contraindications"),
            )
        ),
        CheckConstraint(
            "(pregnancy_collection_status = 'unknown' AND pregnancy_value IS NULL) OR (pregnancy_collection_status = 'explicitly_none' AND pregnancy_value IS NULL) OR (pregnancy_collection_status = 'collected' AND pregnancy_value IS NOT NULL)",
            name="chk_safety_profiles_pregnancy_collection",
        ),
        CheckConstraint(
            "(lactation_collection_status = 'unknown' AND lactation_value IS NULL) OR (lactation_collection_status = 'explicitly_none' AND lactation_value IS NULL) OR (lactation_collection_status = 'collected' AND lactation_value IS NOT NULL)",
            name="chk_safety_profiles_lactation_collection",
        ),
    )


class GraphRun(Base, UUIDPrimaryKeyMixin):
    """Minimal graph-execution metadata; never a source of clinical facts."""

    __tablename__ = "graph_runs"
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    graph_version: Mapped[str] = mapped_column(String(64), nullable=False)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    input_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("input_state_version >= 1", name="chk_graph_runs_input_state_version"),
        CheckConstraint("status IN ('running','completed','failed','cancelled')", name="chk_graph_runs_status"),
        Index("idx_graph_runs_session_created", "session_id", "created_at"),
        Index("idx_graph_runs_session_command", "session_id", "command_id"),
    )


class GraphRunStep(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "graph_run_steps"
    graph_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("graph_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    step_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("step_index >= 0", name="chk_graph_run_steps_index"),
        CheckConstraint("status IN ('started','completed','failed','skipped')", name="chk_graph_run_steps_status"),
        CheckConstraint(
            "metadata IS NULL OR jsonb_typeof(metadata) = 'object'", name="chk_graph_run_steps_metadata_object"
        ),
        Index("uq_graph_run_steps_run_index", "graph_run_id", "step_index", unique=True),
    )


class ArtifactRevision(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "artifact_revisions"
    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    input_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    produced_by_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("graph_runs.id", ondelete="RESTRICT"), nullable=False
    )
    parent_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    parent_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("revision >= 1", name="chk_artifact_revisions_revision"),
        CheckConstraint("input_state_version >= 1", name="chk_artifact_revisions_input_state_version"),
        CheckConstraint("status IN ('current','superseded','stale')", name="chk_artifact_revisions_status"),
        CheckConstraint("char_length(artifact_type) > 0", name="chk_artifact_revisions_type_nonempty"),
        CheckConstraint(
            "(revision = 1 AND parent_revision_id IS NULL AND parent_revision IS NULL) OR "
            "(revision > 1 AND parent_revision_id IS NOT NULL AND parent_revision = revision - 1)",
            name="chk_artifact_revisions_parent_relation",
        ),
        Index("uq_artifact_revisions_artifact_revision", "artifact_id", "revision", unique=True),
        Index(
            "uq_artifact_revisions_parent_target",
            "id",
            "artifact_id",
            "session_id",
            "revision",
            unique=True,
        ),
        ForeignKeyConstraint(
            ["parent_revision_id", "artifact_id", "session_id", "parent_revision"],
            [
                "artifact_revisions.id",
                "artifact_revisions.artifact_id",
                "artifact_revisions.session_id",
                "artifact_revisions.revision",
            ],
            name="fk_artifact_revisions_parent_same_artifact_session",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_artifact_revisions_one_current",
            "artifact_id",
            unique=True,
            postgresql_where=(status == "current"),
        ),
        Index("idx_artifact_revisions_session_type_status", "session_id", "artifact_type", "status"),
    )


class ArtifactRevisionPayload(Base, UUIDPrimaryKeyMixin):
    """Structured artifact payload bound to one artifact revision.

    Payload rows are deliberately separate from ``artifact_revisions`` so the
    DomainState ledger can keep returning metadata-only artifact references.
    """

    __tablename__ = "artifact_revision_payloads"
    artifact_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifact_revisions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("revision >= 1", name="chk_artifact_revision_payloads_revision"),
        CheckConstraint(
            "char_length(payload_schema_version) > 0",
            name="chk_artifact_revision_payloads_schema_nonempty",
        ),
        CheckConstraint(
            "content_digest ~ '^[0-9a-f]{64}$'",
            name="chk_artifact_revision_payloads_digest",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="chk_artifact_revision_payloads_payload_object",
        ),
        UniqueConstraint(
            "session_id",
            "artifact_id",
            "revision",
            name="uq_artifact_revision_payloads_revision",
        ),
        ForeignKeyConstraint(
            ["artifact_id", "revision"],
            ["artifact_revisions.artifact_id", "artifact_revisions.revision"],
            name="fk_artifact_revision_payloads_artifact_revision",
            ondelete="CASCADE",
        ),
        Index("idx_artifact_revision_payloads_session_artifact", "session_id", "artifact_id", "revision"),
    )


class GateResult(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "gate_results"
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    graph_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("graph_runs.id", ondelete="SET NULL"), nullable=True
    )
    gate_name: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("input_state_version >= 1", name="chk_gate_results_input_state_version"),
        CheckConstraint("decision IN ('passed','failed','blocked')", name="chk_gate_results_decision"),
        CheckConstraint("char_length(gate_name) > 0", name="chk_gate_results_name_nonempty"),
        CheckConstraint("details IS NULL OR jsonb_typeof(details) = 'object'", name="chk_gate_results_details_object"),
        Index("idx_gate_results_session_created", "session_id", "created_at"),
        Index("idx_gate_results_run", "graph_run_id"),
    )


class OutboxEvent(Base, UUIDPrimaryKeyMixin):
    """A durable, privacy-minimal event awaiting an external publisher."""

    __tablename__ = "outbox_events"
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    graph_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("graph_runs.id", ondelete="CASCADE"), nullable=False
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    leased_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("char_length(event_type) > 0", name="chk_outbox_events_type_nonempty"),
        CheckConstraint("state_version >= 1", name="chk_outbox_events_state_version"),
        CheckConstraint("attempt_count >= 0", name="chk_outbox_events_attempt_count"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="chk_outbox_events_payload_object"),
        CheckConstraint("status IN ('pending','leased','published')", name="chk_outbox_events_status"),
        CheckConstraint(
            "(status = 'leased' AND leased_by IS NOT NULL AND leased_until IS NOT NULL) OR "
            "(status <> 'leased' AND leased_by IS NULL AND leased_until IS NULL)",
            name="chk_outbox_events_lease_relation",
        ),
        CheckConstraint(
            "(status = 'published' AND published_at IS NOT NULL) OR (status <> 'published' AND published_at IS NULL)",
            name="chk_outbox_events_published_relation",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR last_error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'",
            name="chk_outbox_events_error_code",
        ),
        Index("idx_outbox_events_claim", "status", "available_at", "leased_until", "created_at"),
        Index("idx_outbox_events_session_version", "session_id", "state_version"),
    )


class DomainCommandCommit(Base, UUIDPrimaryKeyMixin):
    """Database-level idempotency record and stable commit result."""

    __tablename__ = "domain_command_commits"
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    input_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_spec_version: Mapped[str] = mapped_column(String(100), nullable=False)
    delta_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    output_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    changed: Mapped[bool] = mapped_column(nullable=False)
    graph_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("graph_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    outbox_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outbox_events.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("input_state_version >= 1", name="chk_domain_command_commits_input_version"),
        CheckConstraint(
            "output_state_version IN (input_state_version, input_state_version + 1)",
            name="chk_domain_command_commits_output_version",
        ),
        CheckConstraint("delta_digest ~ '^[0-9a-f]{64}$'", name="chk_domain_command_commits_digest"),
        UniqueConstraint(
            "session_id",
            "idempotency_key",
            "input_state_version",
            "agent_spec_version",
            name="uq_domain_command_commits_idempotency",
        ),
        Index("idx_domain_command_commits_session_created", "session_id", "created_at"),
    )


class IntakeCommandClaim(Base, UUIDPrimaryKeyMixin):
    """Durable L3-5 command claim and replayable response."""

    __tablename__ = "intake_command_claims"
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_sessions.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    patient_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_messages.id", ondelete="SET NULL"), nullable=True
    )
    question_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consult_messages.id", ondelete="SET NULL"), nullable=True
    )
    output_state_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intermediate_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), server_onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("session_id", "idempotency_key", name="uq_intake_command_claims_idempotency"),
        CheckConstraint("input_state_version >= 1", name="chk_intake_command_claims_input_version"),
        CheckConstraint(
            "output_state_version IS NULL OR output_state_version >= input_state_version",
            name="chk_intake_command_claims_output_version",
        ),
        CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="chk_intake_command_claims_digest"),
        CheckConstraint("status IN ('running','completed','failed')", name="chk_intake_command_claims_status"),
        CheckConstraint(
            "intermediate_payload IS NULL OR jsonb_typeof(intermediate_payload) = 'object'",
            name="chk_intake_command_claims_intermediate_object",
        ),
        CheckConstraint(
            "response_payload IS NULL OR jsonb_typeof(response_payload) = 'object'",
            name="chk_intake_command_claims_response_object",
        ),
        Index("idx_intake_command_claims_session_status", "session_id", "status", "created_at"),
        Index("idx_intake_command_claims_run", "run_id"),
    )
