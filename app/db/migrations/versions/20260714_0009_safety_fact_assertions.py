"""Add the durable safety-fact confirmation ledger.

Revision ID: 20260714_0009
Revises: 20260713_0008, 20260714_0008
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260714_0009"
down_revision = ("20260713_0008", "20260714_0008")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safety_fact_assertions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("consult_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_name", sa.String(32), nullable=False),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column("value_digest", sa.String(64), nullable=False),
        sa.Column("assertion_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column(
            "source_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("consult_messages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("extraction_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("template_version", sa.String(64), nullable=False),
        sa.Column("evidence_spans", postgresql.JSONB, nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("proposed_by_actor_type", sa.String(16), nullable=False),
        sa.Column("proposed_by_actor_id", sa.String(128), nullable=True),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("confirmed_by_actor_type", sa.String(16), nullable=True),
        sa.Column("confirmed_by_actor_id", sa.String(128), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by_actor_type", sa.String(16), nullable=True),
        sa.Column("rejected_by_actor_id", sa.String(128), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retracted_by_actor_type", sa.String(16), nullable=True),
        sa.Column("retracted_by_actor_id", sa.String(128), nullable=True),
        sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "supersedes_assertion_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("safety_fact_assertions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "field_name IN ('allergy','pregnancy','lactation','medications','major_conditions',"
            "'contraindications','red_flag')",
            name="chk_safety_fact_assertions_field",
        ),
        sa.CheckConstraint(
            "status IN ('proposed','confirmed','rejected','superseded','retracted')",
            name="chk_safety_fact_assertions_status",
        ),
        sa.CheckConstraint(
            "source_kind IN ('model_extraction','deterministic_precheck','structured_form')",
            name="chk_safety_fact_assertions_source_kind",
        ),
        sa.CheckConstraint(
            "proposed_by_actor_type IN ('model','doctor','system')",
            name="chk_safety_fact_assertions_proposer_type",
        ),
        sa.CheckConstraint(
            "confirmed_by_actor_type IS NULL OR confirmed_by_actor_type IN ('doctor','system')",
            name="chk_safety_fact_assertions_confirmer_type",
        ),
        sa.CheckConstraint(
            "rejected_by_actor_type IS NULL OR rejected_by_actor_type IN ('doctor','system')",
            name="chk_safety_fact_assertions_rejecter_type",
        ),
        sa.CheckConstraint(
            "retracted_by_actor_type IS NULL OR retracted_by_actor_type IN ('doctor','system')",
            name="chk_safety_fact_assertions_retractor_type",
        ),
        sa.CheckConstraint("jsonb_typeof(value) = 'object'", name="chk_safety_fact_assertions_value_object"),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_spans) = 'array'", name="chk_safety_fact_assertions_evidence_array"
        ),
        sa.CheckConstraint("value_digest ~ '^[0-9a-f]{64}$'", name="chk_safety_fact_assertions_value_digest"),
        sa.CheckConstraint(
            "assertion_fingerprint ~ '^[0-9a-f]{64}$'", name="chk_safety_fact_assertions_fingerprint"
        ),
        sa.CheckConstraint(
            "evidence_digest ~ '^[0-9a-f]{64}$'", name="chk_safety_fact_assertions_evidence_digest"
        ),
        sa.CheckConstraint(
            "supersedes_assertion_id IS NULL OR supersedes_assertion_id <> id",
            name="chk_safety_fact_assertions_no_self_supersede",
        ),
        sa.CheckConstraint(
            "(status = 'proposed' AND confirmed_at IS NULL AND rejected_at IS NULL AND retracted_at IS NULL "
            "AND superseded_at IS NULL) OR "
            "(status = 'confirmed' AND confirmed_at IS NOT NULL AND confirmed_by_actor_type IS NOT NULL "
            "AND rejected_at IS NULL AND retracted_at IS NULL "
            "AND superseded_at IS NULL) OR "
            "(status = 'rejected' AND confirmed_at IS NULL AND rejected_at IS NOT NULL "
            "AND rejected_by_actor_type IS NOT NULL AND retracted_at IS NULL "
            "AND superseded_at IS NULL) OR "
            "(status = 'superseded' AND confirmed_at IS NOT NULL AND confirmed_by_actor_type IS NOT NULL "
            "AND rejected_at IS NULL AND retracted_at IS NULL "
            "AND superseded_at IS NOT NULL) OR "
            "(status = 'retracted' AND confirmed_at IS NOT NULL AND confirmed_by_actor_type IS NOT NULL "
            "AND rejected_at IS NULL AND retracted_at IS NOT NULL AND retracted_by_actor_type IS NOT NULL "
            "AND superseded_at IS NULL)",
            name="chk_safety_fact_assertions_transition_state",
        ),
        sa.UniqueConstraint(
            "session_id", "assertion_fingerprint", name="uq_safety_fact_assertions_fingerprint"
        ),
    )
    op.create_index(
        "idx_safety_fact_assertions_session_status",
        "safety_fact_assertions",
        ["session_id", "status", "proposed_at"],
    )
    op.create_index(
        "idx_safety_fact_assertions_source",
        "safety_fact_assertions",
        ["source_message_id", "extraction_run_id"],
    )
    op.create_index(
        "uq_safety_fact_assertions_one_confirmed_field",
        "safety_fact_assertions",
        ["session_id", "field_name"],
        unique=True,
        postgresql_where=sa.text("status = 'confirmed' AND field_name <> 'red_flag'"),
    )

    op.create_table(
        "safety_fact_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("consult_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assertion_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("safety_fact_assertions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("resulting_status", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("action IN ('confirm','reject','retract')", name="chk_safety_fact_transitions_action"),
        sa.CheckConstraint(
            "resulting_status IN ('confirmed','rejected','retracted')",
            name="chk_safety_fact_transitions_result",
        ),
        sa.CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'", name="chk_safety_fact_transitions_request_digest"
        ),
        sa.CheckConstraint(
            "idempotency_key_digest ~ '^[0-9a-f]{64}$'",
            name="chk_safety_fact_transitions_idempotency_digest",
        ),
        sa.UniqueConstraint(
            "session_id", "idempotency_key_digest", name="uq_safety_fact_transitions_idempotency"
        ),
    )
    op.create_index(
        "idx_safety_fact_transitions_assertion",
        "safety_fact_transitions",
        ["assertion_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("safety_fact_transitions")
    op.drop_table("safety_fact_assertions")
