"""Add L4-4 artifact revision payloads.

Revision ID: 20260712_0007
Revises: 20260712_0006
Create Date: 2026-07-12
"""

from __future__ import annotations

from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260712_0007"
down_revision = "20260712_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifact_revision_payloads",
        sa.Column("artifact_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload_schema_version", sa.String(length=64), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("payload", cast(Any, postgresql.JSONB)(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="chk_artifact_revision_payloads_revision"),
        sa.CheckConstraint(
            "char_length(payload_schema_version) > 0",
            name="chk_artifact_revision_payloads_schema_nonempty",
        ),
        sa.CheckConstraint(
            "content_digest ~ '^[0-9a-f]{64}$'",
            name="chk_artifact_revision_payloads_digest",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="chk_artifact_revision_payloads_payload_object",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_revision_id"],
            ["artifact_revisions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id", "revision"],
            ["artifact_revisions.artifact_id", "artifact_revisions.revision"],
            name="fk_artifact_revision_payloads_artifact_revision",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["consult_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_revision_id"),
        sa.UniqueConstraint(
            "session_id",
            "artifact_id",
            "revision",
            name="uq_artifact_revision_payloads_revision",
        ),
    )
    op.create_index(
        "idx_artifact_revision_payloads_session_artifact",
        "artifact_revision_payloads",
        ["session_id", "artifact_id", "revision"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_artifact_revision_payloads_session_artifact",
        table_name="artifact_revision_payloads",
    )
    op.drop_table("artifact_revision_payloads")
