"""Add L3-5 intake command claims.

Revision ID: 20260711_0005
Revises: 20260711_0004
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260711_0005"
down_revision = "20260711_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intake_command_claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("consult_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("input_state_version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "patient_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("consult_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "question_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("consult_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("output_state_version", sa.Integer, nullable=True),
        sa.Column("response_payload", postgresql.JSONB, nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "idempotency_key", name="uq_intake_command_claims_idempotency"),
        sa.CheckConstraint("input_state_version >= 1", name="chk_intake_command_claims_input_version"),
        sa.CheckConstraint(
            "output_state_version IS NULL OR output_state_version >= input_state_version",
            name="chk_intake_command_claims_output_version",
        ),
        sa.CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="chk_intake_command_claims_digest"),
        sa.CheckConstraint("status IN ('running','completed','failed')", name="chk_intake_command_claims_status"),
        sa.CheckConstraint(
            "response_payload IS NULL OR jsonb_typeof(response_payload) = 'object'",
            name="chk_intake_command_claims_response_object",
        ),
    )
    op.create_index(
        "idx_intake_command_claims_session_status",
        "intake_command_claims",
        ["session_id", "status", "created_at"],
    )
    op.create_index("idx_intake_command_claims_run", "intake_command_claims", ["run_id"])


def downgrade() -> None:
    op.drop_index("idx_intake_command_claims_run", table_name="intake_command_claims")
    op.drop_index("idx_intake_command_claims_session_status", table_name="intake_command_claims")
    op.drop_table("intake_command_claims")
