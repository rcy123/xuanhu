"""Add durable async command substrate (R6-A).

Revision ID: 20260729_0016
Revises: 20260729_0015
Create Date: 2026-07-29
"""

from __future__ import annotations

from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260729_0016"
down_revision = "20260729_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # async-command Outbox rows are not graph runs. Relax the existing
    # outbox_events.graph_run_id FK so async_command.* lifecycle rows may carry
    # a NULL run reference while existing synchronous rows keep theirs. A check
    # constraint keeps the two populations disjoint.
    op.alter_column("outbox_events", "graph_run_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.create_check_constraint(
        "chk_outbox_events_graph_run_boundary",
        "outbox_events",
        "((event_type LIKE 'async_command.%' AND graph_run_id IS NULL) OR "
        "(event_type NOT LIKE 'async_command.%' AND graph_run_id IS NOT NULL))",
    )

    op.create_table(
        "async_commands",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("request_payload", cast(Any, postgresql.JSONB)(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_http_status", sa.Integer(), nullable=True),
        sa.Column("result_payload", cast(Any, postgresql.JSONB)(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_payload", cast(Any, postgresql.JSONB)(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "operation IN ('intake.message', 'prescription.review', 'session.advance')",
            name="chk_async_commands_operation_allowlist",
        ),
        sa.CheckConstraint(
            "idempotency_key_digest ~ '^[0-9a-f]{64}$'",
            name="chk_async_commands_key_digest",
        ),
        sa.CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="chk_async_commands_request_digest",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed')",
            name="chk_async_commands_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(request_payload) = 'object'",
            name="chk_async_commands_request_object",
        ),
        sa.CheckConstraint(
            "result_payload IS NULL OR jsonb_typeof(result_payload) = 'object'",
            name="chk_async_commands_result_object",
        ),
        sa.CheckConstraint(
            "error_payload IS NULL OR jsonb_typeof(error_payload) = 'object'",
            name="chk_async_commands_error_object",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="chk_async_commands_attempt_count"),
        sa.CheckConstraint(
            "result_http_status IS NULL OR (result_http_status >= 100 AND result_http_status <= 599)",
            name="chk_async_commands_http_status",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'",
            name="chk_async_commands_error_code",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'running' AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="chk_async_commands_lease_relation",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND result_http_status IS NOT NULL AND result_payload IS NOT NULL "
            "AND error_code IS NULL AND error_payload IS NULL) "
            "OR (status = 'failed' AND error_code IS NOT NULL AND error_payload IS NOT NULL "
            "AND result_http_status IS NULL AND result_payload IS NULL) "
            "OR status IN ('queued','running')",
            name="chk_async_commands_terminal_payload",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded','failed') AND completed_at IS NOT NULL) "
            "OR (status IN ('queued','running') AND completed_at IS NULL)",
            name="chk_async_commands_completed_relation",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_async_commands"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["consult_sessions.id"],
            ondelete="CASCADE",
            name="fk_async_commands_session_id",
        ),
        sa.UniqueConstraint(
            "session_id",
            "operation",
            "idempotency_key_digest",
            name="uq_async_commands_logical_command",
        ),
    )
    op.create_index(
        "uq_async_commands_active_session",
        "async_commands",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.create_index(
        "idx_async_commands_claim",
        "async_commands",
        ["status", "available_at", "lease_expires_at"],
    )
    op.create_index(
        "idx_async_commands_session_created",
        "async_commands",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    # async_command.* Outbox rows carry a NULL graph_run_id. Remove them before
    # restoring graph_run_id NOT NULL, so the downgrade works with real lifecycle
    # data present (not only an empty table).
    op.execute("DELETE FROM outbox_events WHERE event_type LIKE 'async_command.%'")
    op.drop_index("idx_async_commands_session_created", table_name="async_commands")
    op.drop_index("idx_async_commands_claim", table_name="async_commands")
    op.drop_index("uq_async_commands_active_session", table_name="async_commands")
    op.drop_table("async_commands")
    op.drop_constraint("chk_outbox_events_graph_run_boundary", "outbox_events", type_="check")
    op.alter_column("outbox_events", "graph_run_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
