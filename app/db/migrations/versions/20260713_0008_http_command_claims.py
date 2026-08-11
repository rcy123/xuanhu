"""Add durable public HTTP command claims.

Revision ID: 20260713_0008
Revises: 20260712_0007
Create Date: 2026-07-13
"""

from __future__ import annotations

from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260713_0008"
down_revision = "20260712_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "http_command_claims",
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("scope_key", sa.String(length=160), nullable=False),
        sa.Column("concurrency_scope", sa.String(length=160), nullable=True),
        sa.Column("idempotency_mode", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("owner_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("response_payload", cast(Any, postgresql.JSONB)(astext_type=sa.Text()), nullable=True),
        sa.Column("error_payload", cast(Any, postgresql.JSONB)(astext_type=sa.Text()), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "idempotency_mode IN ('public','non_idempotent')",
            name="chk_http_command_claims_idempotency_mode",
        ),
        sa.CheckConstraint(
            "idempotency_key_digest ~ '^[0-9a-f]{64}$'",
            name="chk_http_command_claims_key_digest",
        ),
        sa.CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="chk_http_command_claims_request_digest",
        ),
        sa.CheckConstraint(
            "status IN ('running','completed','failed','ambiguous')",
            name="chk_http_command_claims_status",
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR (http_status >= 100 AND http_status <= 599)",
            name="chk_http_command_claims_http_status",
        ),
        sa.CheckConstraint(
            "response_payload IS NULL OR jsonb_typeof(response_payload) = 'object'",
            name="chk_http_command_claims_response_object",
        ),
        sa.CheckConstraint(
            "error_payload IS NULL OR jsonb_typeof(error_payload) = 'object'",
            name="chk_http_command_claims_error_object",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND response_payload IS NOT NULL AND http_status IS NOT NULL) "
            "OR status <> 'completed'",
            name="chk_http_command_claims_completed_payload",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_http_command_claims"),
        sa.UniqueConstraint(
            "operation",
            "scope_key",
            "idempotency_key_digest",
            name="uq_http_command_claims_logical_command",
        ),
    )
    op.create_index(
        "uq_http_command_claims_inflight_scope",
        "http_command_claims",
        ["concurrency_scope"],
        unique=True,
        postgresql_where=sa.text("status = 'running' AND concurrency_scope IS NOT NULL"),
    )
    op.create_index(
        "idx_http_command_claims_status_lease",
        "http_command_claims",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "idx_http_command_claims_scope_created",
        "http_command_claims",
        ["scope_key", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_http_command_claims_scope_created", table_name="http_command_claims")
    op.drop_index("idx_http_command_claims_status_lease", table_name="http_command_claims")
    op.drop_index("uq_http_command_claims_inflight_scope", table_name="http_command_claims")
    op.drop_table("http_command_claims")
