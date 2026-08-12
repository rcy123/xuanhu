"""Add doctors table for stage-1 JWT authentication.

Revision ID: 20260813_0018
Revises: 20260811_0017
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260813_0018"
down_revision = "20260811_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "doctors",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "enabled IN (true, false)",
            name="chk_doctors_enabled",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_doctors"),
    )


def downgrade() -> None:
    op.drop_table("doctors")
