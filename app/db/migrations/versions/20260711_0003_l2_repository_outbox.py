"""Add L2 repository idempotency and transactional outbox tables.

Revision ID: 20260711_0003
Revises: 20260710_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260711_0003"
down_revision: str | None = "20260710_0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("consult_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "graph_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("graph_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state_version", sa.Integer, nullable=False),
        sa.Column("trace_id", sa.String(200), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("leased_by", sa.String(128), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("char_length(event_type) > 0", name="chk_outbox_events_type_nonempty"),
        sa.CheckConstraint("state_version >= 1", name="chk_outbox_events_state_version"),
        sa.CheckConstraint("attempt_count >= 0", name="chk_outbox_events_attempt_count"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="chk_outbox_events_payload_object"),
        sa.CheckConstraint("status IN ('pending','leased','published')", name="chk_outbox_events_status"),
        sa.CheckConstraint(
            "(status = 'leased' AND leased_by IS NOT NULL AND leased_until IS NOT NULL) OR (status <> 'leased' AND leased_by IS NULL AND leased_until IS NULL)",
            name="chk_outbox_events_lease_relation",
        ),
        sa.CheckConstraint(
            "(status = 'published' AND published_at IS NOT NULL) OR (status <> 'published' AND published_at IS NULL)",
            name="chk_outbox_events_published_relation",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR last_error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'", name="chk_outbox_events_error_code"
        ),
    )
    op.create_index(
        "idx_outbox_events_claim", "outbox_events", ["status", "available_at", "leased_until", "created_at"]
    )
    op.create_index("idx_outbox_events_session_version", "outbox_events", ["session_id", "state_version"])
    op.create_table(
        "domain_command_commits",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("consult_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("input_state_version", sa.Integer, nullable=False),
        sa.Column("agent_spec_version", sa.String(100), nullable=False),
        sa.Column("delta_digest", sa.String(64), nullable=False),
        sa.Column("output_state_version", sa.Integer, nullable=False),
        sa.Column("changed", sa.Boolean, nullable=False),
        sa.Column(
            "graph_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("graph_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "outbox_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("outbox_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("graph_run_id"),
        sa.UniqueConstraint("outbox_event_id"),
        sa.UniqueConstraint(
            "session_id",
            "idempotency_key",
            "input_state_version",
            "agent_spec_version",
            name="uq_domain_command_commits_idempotency",
        ),
        sa.CheckConstraint("input_state_version >= 1", name="chk_domain_command_commits_input_version"),
        sa.CheckConstraint(
            "output_state_version IN (input_state_version, input_state_version + 1)",
            name="chk_domain_command_commits_output_version",
        ),
        sa.CheckConstraint("delta_digest ~ '^[0-9a-f]{64}$'", name="chk_domain_command_commits_digest"),
    )
    op.create_index(
        "idx_domain_command_commits_session_created", "domain_command_commits", ["session_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("domain_command_commits")
    op.drop_table("outbox_events")
