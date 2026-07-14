"""Add minimal durable model-run audits.

Revision ID: 20260715_0010
Revises: 20260714_0009
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260715_0010"
down_revision = "20260714_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_run_audits",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("consult_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_name", sa.String(100), nullable=False),
        sa.Column("stage", sa.String(100), nullable=False),
        sa.Column("agent_spec_version", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("output_schema_id", sa.String(255), nullable=False),
        sa.Column("model_requested", sa.String(200), nullable=False),
        sa.Column("model_actual", sa.String(200), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_digest", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("run_id"),
        sa.CheckConstraint(
            "status IN ('started','succeeded','failed','cancelled')",
            name="chk_model_run_audits_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="chk_model_run_audits_attempts"),
        sa.CheckConstraint("latency_ms >= 0", name="chk_model_run_audits_latency"),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0",
            name="chk_model_run_audits_token_usage",
        ),
        sa.CheckConstraint(
            "output_digest IS NULL OR output_digest ~ '^[0-9a-f]{64}$'",
            name="chk_model_run_audits_output_digest",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND output_digest IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND output_digest IS NULL AND error_code IS NOT NULL) OR "
            "(status IN ('started','cancelled') AND output_digest IS NULL AND error_code IS NULL)",
            name="chk_model_run_audits_terminal_payload",
        ),
    )
    op.create_index(
        "idx_model_run_audits_session_created",
        "model_run_audits",
        ["session_id", "created_at"],
    )
    op.create_index("idx_model_run_audits_trace", "model_run_audits", ["trace_id"])
    op.create_index(
        "idx_model_run_audits_status_updated",
        "model_run_audits",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_table("model_run_audits")
